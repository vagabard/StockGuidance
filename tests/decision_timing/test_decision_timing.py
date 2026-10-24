from dataclasses import replace
from datetime import timedelta

import pytest

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.sessions import TradingSession
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.portfolio.availability import SaleProceedsTiming
from stockguidance.targets.availability import LabelTiming


def daily_close(session: TradingSession, delay_minutes: int = 15) -> ObservationTiming:
	return ObservationTiming(session, session.closes_at, session.closes_at + timedelta(minutes=delay_minutes))


def test_decision_and_fill_times_are_distinct(timing: DecisionTiming) -> None:
	assert timing.previous_session.closes_at < timing.decision_cutoff < timing.entry_time < timing.exit_time
	assert timing.entry_time == timing.entry_session.closes_at
	assert timing.exit_time == timing.exit_session.closes_at


@pytest.mark.parametrize("offset", [0, 1])
def test_at_or_after_entry_close_is_too_late(timing: DecisionTiming, offset: int) -> None:
	with pytest.raises(ValueError, match="Decision cutoff"):
		replace(timing, decision_cutoff=timing.entry_time + timedelta(seconds=offset))


def test_naive_timestamps_and_premature_decisions(timing: DecisionTiming) -> None:
	with pytest.raises(ValueError, match="timezone-aware"):
		replace(timing, decision_cutoff=timing.decision_cutoff.replace(tzinfo=None))
	with pytest.raises(ValueError, match="Decision cutoff"):
		replace(timing, decision_cutoff=timing.previous_session.closes_at - timedelta(seconds=1))
	with pytest.raises(ValueError, match="timezone-aware"):
		replace(timing.entry_session, closes_at=timing.entry_time.replace(tzinfo=None))
	with pytest.raises(ValueError, match="opening"):
		replace(timing.entry_session, opens_at=timing.entry_time)


def test_sessions_cross_weekend_using_calendar_ordinals(sessions: tuple[TradingSession, ...]) -> None:
	weekend_timing = DecisionTiming(sessions[0], sessions[1], sessions[2], sessions[1].opens_at)
	assert (weekend_timing.entry_session.session_date - weekend_timing.previous_session.session_date).days == 3


def test_nonconsecutive_or_other_calendar_sessions_rejected(timing: DecisionTiming) -> None:
	with pytest.raises(ValueError, match="next trading session"):
		replace(timing, exit_session=replace(timing.exit_session, sequence=90))
	with pytest.raises(ValueError, match="same calendar"):
		replace(timing, exit_session=replace(timing.exit_session, calendar_id="other-calendar"))
	with pytest.raises(ValueError, match="dates and timestamps"):
		replace(timing, exit_session=replace(timing.exit_session, session_date=timing.entry_session.session_date))


def test_previous_close_checks_publication_and_excludes_entry_data(timing: DecisionTiming) -> None:
	features = FeatureSpec((7, 3), 2)
	features.validate_observation(daily_close(timing.previous_session), timing)
	with pytest.raises(ValueError, match="not available"):
		features.validate_observation(replace(daily_close(timing.previous_session), available_at=timing.entry_time), timing)
	with pytest.raises(ValueError, match="entry-session"):
		features.validate_observation(daily_close(timing.entry_session), timing)
	with pytest.raises(ValueError, match="lookback"):
		features.validate_observation(daily_close(replace(timing.previous_session, sequence=-10)), timing)


def test_observation_is_not_publication(timing: DecisionTiming) -> None:
	observation = daily_close(timing.previous_session)
	assert not observation.is_available_at(observation.observed_at)
	assert observation.is_available_at(observation.available_at)
	with pytest.raises(ValueError, match="before it is observed"):
		replace(observation, available_at=observation.observed_at - timedelta(seconds=1))
	with pytest.raises(ValueError, match="session close"):
		replace(observation, observed_at=timing.previous_session.opens_at)


def test_label_for_previous_entry_is_immature_before_todays_close(timing: DecisionTiming) -> None:
	label = LabelTiming(daily_close(timing.previous_session), daily_close(timing.entry_session))
	assert not label.is_mature_at(timing.decision_cutoff)
	assert not label.is_mature_at(timing.entry_time)
	assert label.is_mature_at(label.exit_close.available_at)
	assert not replace(label, entry_close=replace(label.entry_close, available_at=timing.exit_time)).is_mature_at(label.exit_close.available_at)


def test_cash_from_sale_cannot_fund_same_close_purchase(timing: DecisionTiming) -> None:
	proceeds = SaleProceedsTiming(timing.previous_session, timing.entry_session)
	assert not proceeds.is_available_for(timing.previous_session)
	assert proceeds.is_available_for(timing.entry_session)
	assert proceeds.is_available_for(timing.exit_session)
	with pytest.raises(ValueError, match="next trading session"):
		SaleProceedsTiming(timing.entry_session, timing.entry_session)
	with pytest.raises(ValueError, match="next trading session"):
		SaleProceedsTiming(timing.previous_session, timing.exit_session)
