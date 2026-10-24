from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from stockguidance.calendar.resolution import resolve_decision
from stockguidance.features.history_window import WindowState, plan_feature_history
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.targets.training_window import plan_training_history


def observations_between(calendar, start, end, publication):
	return tuple(ObservationTiming(session, session.closes_at, publication.expected_at(session)) for session in calendar.sessions(start, end))


def test_first_supported_entry_has_insufficient_lookback_without_earlier_data(boundary, calendar, publication, clock):
	timing = resolve_decision(calendar, boundary.history_start, clock, as_of=datetime(2024, 7, 1, 19, 30, tzinfo=timezone.utc))
	window = plan_feature_history(calendar, boundary, FeatureSpec((7, 3), 2), timing, publication)
	assert window.state is WindowState.INSUFFICIENT_HISTORY
	assert window.sessions == ()
	assert window.first_usable_entry.session_date == date(2024, 7, 3)


def test_first_usable_feature_window_uses_exact_previous_sessions(boundary, calendar, publication, clock):
	timing = resolve_decision(calendar, date(2024, 7, 3), clock, as_of=datetime(2024, 7, 3, 16, 30, tzinfo=timezone.utc))
	window = plan_feature_history(calendar, boundary, FeatureSpec((7, 3), 2), timing, publication)
	assert window.state is WindowState.EXPECTED_AVAILABLE
	assert [session.session_date for session in window.sessions] == [date(2024, 7, 1), date(2024, 7, 2)]
	assert all(session.session_date >= boundary.history_start for session in window.sessions)


def test_close_completion_does_not_imply_prior_bar_publication(boundary, calendar, publication, clock):
	timing = resolve_decision(calendar, date(2024, 7, 5), clock, as_of=datetime(2024, 7, 3, 17, 5, tzinfo=timezone.utc))
	window = plan_feature_history(calendar, boundary, FeatureSpec((7,), 2), timing, publication)
	assert window.state is WindowState.NOT_YET_AVAILABLE


def test_historical_training_window_ignores_current_database_latest(boundary, calendar, publication, clock):
	history = replace(boundary, history_start=date(2024, 6, 24))
	timing = resolve_decision(calendar, date(2024, 7, 3), clock, as_of=datetime(2024, 7, 3, 16, 30, tzinfo=timezone.utc))
	all_observations = observations_between(calendar, history.history_start, date(2024, 7, 12), publication)
	window = plan_training_history(calendar, history, FeatureSpec((7,), 2), timing, publication, all_observations, count=2)
	assert [session.session_date for session in window.entry_sessions] == [date(2024, 6, 28), date(2024, 7, 1)]
	assert window.mature_entries == window.entry_sessions
	assert [session.session_date for session in window.not_yet_available_entries] == [date(2024, 7, 2)]
	assert window.missing_expected_prices == ()


def test_missing_mature_outcomes_are_visible_and_do_not_shift_window(boundary, calendar, publication, clock):
	history = replace(boundary, history_start=date(2024, 6, 24))
	timing = resolve_decision(calendar, date(2024, 7, 3), clock, as_of=datetime(2024, 7, 3, 16, 30, tzinfo=timezone.utc))
	available = tuple(observation for observation in observations_between(calendar, history.history_start, date(2024, 7, 5), publication) if observation.session.session_date != date(2024, 7, 1))
	window = plan_training_history(calendar, history, FeatureSpec((7,), 2), timing, publication, available, count=2)
	assert [session.session_date for session in window.entry_sessions] == [date(2024, 6, 28), date(2024, 7, 1)]
	assert window.mature_entries == ()
	assert [session.session_date for session in window.missing_expected_prices] == [date(2024, 7, 1)]


def test_known_late_exit_publication_is_not_a_mature_training_label(boundary, calendar, publication, clock):
	timing = resolve_decision(calendar, date(2024, 7, 8), clock)
	observations = observations_between(calendar, boundary.history_start, date(2024, 7, 8), publication)
	observations = tuple(replace(observation, available_at=clock.now() + timedelta(seconds=1)) if observation.session.session_date == date(2024, 7, 5) else observation for observation in observations)
	window = plan_training_history(calendar, boundary, FeatureSpec((7,), 2), timing, publication, observations, count=1)
	assert [session.session_date for session in window.entry_sessions] == [date(2024, 7, 3)]
	assert window.mature_entries == ()
	assert {session.session_date for session in window.not_yet_available_entries} == {date(2024, 7, 3), date(2024, 7, 5)}
