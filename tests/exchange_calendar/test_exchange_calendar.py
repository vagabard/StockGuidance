from datetime import date, datetime, timedelta, timezone

import pytest

from stockguidance.calendar.clock import decision_cutoff
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.calendar.resolution import latest_finalized_session, resolve_decision


def test_holiday_and_early_close_come_from_exchange_calendar(calendar):
	early, following = calendar.sessions(date(2024, 7, 3), date(2024, 7, 5))
	assert early.closes_at == datetime(2024, 7, 3, 17, tzinfo=timezone.utc)
	assert following.closes_at == datetime(2024, 7, 5, 20, tzinfo=timezone.utc)
	assert calendar.shift(early, 1) == following
	assert following.sequence == early.sequence + 1
	with pytest.raises(ValueError, match="not a XNYS trading session"):
		calendar.session(date(2024, 7, 4))
	assert calendar.sessions(date(2024, 7, 4), date(2024, 7, 4)) == ()


def test_dst_changes_actual_utc_times_without_changing_session_adjacency(calendar):
	before = calendar.session(date(2024, 3, 8))
	after = calendar.shift(before, 1)
	assert after.session_date == date(2024, 3, 11)
	assert (before.opens_at.hour, before.closes_at.hour) == (14, 21)
	assert (after.opens_at.hour, after.closes_at.hour) == (13, 20)
	assert before.closes_at.astimezone(calendar.timezone).hour == after.closes_at.astimezone(calendar.timezone).hour == 16
	assert calendar.session(date(2024, 11, 29)).closes_at.hour == 18


def test_session_ordinals_are_stable_across_loaded_ranges(calendar):
	other_range = ExchangeCalendar("XNYS", date(2024, 1, 1), date(2025, 12, 31))
	assert calendar.session(date(2024, 7, 3)) == other_range.session(date(2024, 7, 3))
	with pytest.raises(ValueError, match="outside"):
		other_range.session(date(2023, 12, 29))


def test_explicit_entry_uses_injected_clock_and_calendar(calendar, clock):
	timing = resolve_decision(calendar, date(2024, 7, 8), clock)
	assert timing.previous_session.session_date == date(2024, 7, 5)
	assert timing.exit_session.session_date == date(2024, 7, 9)
	assert timing.decision_cutoff == clock.now()
	assert timing.entry_time > timing.decision_cutoff
	with pytest.raises(ValueError, match="not a XNYS"):
		resolve_decision(calendar, date(2024, 7, 7), clock)


def test_historical_cutoff_never_resolves_against_current_clock(calendar, clock):
	cutoff = datetime(2024, 7, 3, 16, 30, tzinfo=timezone.utc)
	timing = resolve_decision(calendar, date(2024, 7, 3), clock, as_of=cutoff)
	assert timing.decision_cutoff == cutoff
	assert timing.entry_time == datetime(2024, 7, 3, 17, tzinfo=timezone.utc)
	assert timing.exit_session.session_date == date(2024, 7, 5)
	with pytest.raises(ValueError, match="injected clock"):
		decision_cutoff(clock, clock.now() + timedelta(seconds=1))
	with pytest.raises(ValueError, match="timezone-aware"):
		decision_cutoff(clock, cutoff.replace(tzinfo=None))


def test_latest_finalized_is_close_based_even_before_provider_publication(calendar):
	cutoff = datetime(2024, 7, 3, 17, 5, tzinfo=timezone.utc)
	assert latest_finalized_session(calendar, cutoff, not_before=date(2024, 7, 1)).session_date == date(2024, 7, 3)
	assert latest_finalized_session(calendar, cutoff - timedelta(minutes=6), not_before=date(2024, 7, 1)).session_date == date(2024, 7, 2)
	assert latest_finalized_session(calendar, cutoff, not_before=date(2024, 7, 5)) is None
