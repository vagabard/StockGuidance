from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from stockguidance.database.repositories.sessions import StoredSession
from stockguidance.market_data.coverage import UpdateAction
from stockguidance.market_data.coverage_planning import plan_coverage
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.validation.coverage import CoverageState


def complete_row(calendar, publication, day, date_id):
	session = calendar.session(date(2024, 7, day))
	return StoredSession(date_id, session.session_date, CoverageState.COMPLETE, "Fixture source verification", publication.expected_at(session))


def test_missing_earliest_row_stays_visible_after_later_success(source_config, data_request, boundary, calendar, publication, repository, clock):
	repository.rows = tuple(complete_row(calendar, publication, day, date_id) for day, date_id in ((2, 91), (3, 5), (5, 800)))
	plan = plan_coverage(source_config, data_request, boundary, calendar, publication, repository, clock)
	assert [session.session_date for session in plan.fetch_sessions] == [boundary.history_start]
	assert plan.observed_first_in_scope == date(2024, 7, 2)
	assert plan.boundary.history_start == date(2024, 7, 1)
	assert plan.sessions[0].stored_date_id is None
	assert plan.sessions[-1].state is CoverageState.NOT_YET_AVAILABLE
	assert [observation.session.session_date.day for observation in plan.available_observations] == [2, 3, 5]


def test_interior_missing_and_partial_sessions_are_not_hidden_by_latest_row(source_config, data_request, boundary, calendar, publication, repository, clock):
	repository.rows = (
		complete_row(calendar, publication, 1, 700),
		StoredSession(31, date(2024, 7, 3), CoverageState.INCOMPLETE, "Fixture interrupted import"),
		complete_row(calendar, publication, 5, 4),
	)
	plan = plan_coverage(source_config, data_request, boundary, calendar, publication, repository, clock)
	assert [session.session_date.day for session in plan.fetch_sessions] == [2]
	assert [session.session_date.day for session in plan.repair_sessions] == [3]
	assert plan.sessions[2].stored_date_id == 31
	assert plan.sessions[3].state is CoverageState.COMPLETE


def test_persisted_history_bootstraps_empty_storage_without_preexisting_date_ids(source_config, data_request, boundary, calendar, publication, repository, clock):
	store = JsonHistoryStore(source_config.data_root / "metadata" / "history")
	store.save(boundary)
	plan = plan_coverage(source_config, data_request, store.load(source_config.identity), calendar, publication, repository, clock)
	assert [session.session_date.day for session in plan.fetch_sessions] == [1, 2, 3, 5]
	assert all(item.stored_date_id is None for item in plan.sessions)
	assert plan.observed_first_in_scope is None
	assert not plan.coverage.assessed_sessions_complete
	repository.rows = tuple(complete_row(calendar, publication, day, date_id) for day, date_id in ((1, 90), (2, 7), (3, 150), (5, 3)))
	refreshed = plan_coverage(source_config, data_request, store.load(source_config.identity), calendar, publication, repository, clock)
	assert refreshed.fetch_sessions == ()
	assert [item.stored_date_id for item in refreshed.sessions[:-1]] == [90, 7, 150, 3]


def test_no_query_or_action_crosses_history_or_historical_cutoff(source_config, data_request, boundary, calendar, publication, repository, clock):
	requested = replace(data_request, start=date(2024, 6, 1), end=date(2024, 8, 1), as_of=datetime(2024, 7, 3, 17, 5, tzinfo=timezone.utc))
	plan = plan_coverage(source_config, requested, boundary, calendar, publication, repository, clock)
	assert plan.excluded_before_history and plan.excluded_after_cutoff
	assert plan.latest_finalized.session_date == date(2024, 7, 3)
	assert plan.latest_expected_available.session_date == date(2024, 7, 2)
	assert plan.sessions[-1].state is CoverageState.NOT_YET_AVAILABLE
	assert [session.session_date.day for session in plan.query.sessions] == [1, 2]
	assert all(boundary.history_start <= item.session.session_date <= requested.as_of.date() for item in plan.sessions)
	assert all(item.expected_available_at <= requested.as_of for item in plan.sessions if item.action is not UpdateAction.NONE)
	assert all(session.closes_at <= requested.as_of for session in repository.queries[0].sessions)


def test_unknown_publication_policy_does_not_authorize_requests(source_config, data_request, boundary, calendar, repository, clock):
	publication = DailyPublicationPolicy(None, "No verified source policy")
	plan = plan_coverage(source_config, data_request, boundary, calendar, publication, repository, clock)
	assert plan.fetch_sessions == ()
	assert plan.query is None
	assert repository.queries == []
	assert plan.sessions[0].state is CoverageState.UNVERIFIED
	assert plan.sessions[-1].state is CoverageState.NOT_YET_AVAILABLE


def test_present_but_unverified_rows_require_verification(source_config, data_request, boundary, calendar, publication, repository, clock):
	repository.rows = (StoredSession(17, date(2024, 7, 1), CoverageState.UNVERIFIED, "No source manifest"),)
	plan = plan_coverage(source_config, data_request, boundary, calendar, publication, repository, clock)
	assert plan.sessions[0].action is UpdateAction.VERIFY
	assert plan.available_observations == ()


def test_actual_late_publication_is_not_available_at_historical_cutoff(source_config, data_request, boundary, calendar, publication, repository, clock):
	row = complete_row(calendar, publication, 5, 17)
	repository.rows = (replace(row, available_at=clock.now() + timedelta(minutes=1)),)
	plan = plan_coverage(source_config, data_request, boundary, calendar, publication, repository, clock)
	assert plan.sessions[3].state is CoverageState.NOT_YET_AVAILABLE
	assert plan.sessions[3].action is UpdateAction.NONE
	assert plan.available_observations == ()


def test_source_mismatch_future_cutoff_and_wrong_boundary_fail_before_io(source_config, data_request, boundary, calendar, publication, repository, clock):
	for request, history in (
		(replace(data_request, source=replace(data_request.source, universe="other")), boundary),
		(replace(data_request, as_of=clock.now() + timedelta(days=1)), boundary),
		(data_request, replace(boundary, calendar_id="other-calendar")),
	):
		with pytest.raises(ValueError):
			plan_coverage(source_config, request, history, calendar, publication, repository, clock)
	assert repository.queries == []


def test_before_supported_history_has_no_database_or_provider_work(source_config, data_request, boundary, calendar, publication, repository, clock):
	requested = replace(data_request, end=date(2024, 6, 28), start=date(2024, 6, 1))
	plan = plan_coverage(source_config, requested, boundary, calendar, publication, repository, clock)
	assert plan.sessions == ()
	assert plan.query is None
	assert repository.queries == []
	assert not plan.coverage.assessed_sessions_complete
