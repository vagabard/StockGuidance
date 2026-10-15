from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.update_schema import initialize_update_schema
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState
from stockguidance.validation.coverage import CoverageState
from stockguidance.workflows.failures import StageExecutionError


def test_atomic_failure_preserves_previous_response_and_manifest(harness, calendar):
	first = harness.run()
	manifest = first.manifests[0]
	store = ManifestStore(harness.source.data_root)
	before = store.path_for(harness.source.identity, manifest.download.session).read_bytes()
	download = DownloadedSession(manifest.download.session, harness.response("14"), harness.clock.now())
	with patch("stockguidance.market_data.atomic.os.replace", side_effect=OSError("fixture interrupted write")):
		with pytest.raises(OSError):
			store.save_download(harness.source.identity, download)
	assert store.path_for(harness.source.identity, manifest.download.session).read_bytes() == before
	assert store.load(harness.source.identity, manifest.download.session) == manifest


def test_manifest_atomic_failure_after_content_write_recovers(harness):
	harness.database.fail_on = "INSERT INTO raw_stock_data"
	with patch("stockguidance.market_data.manifests.ManifestStore.save", side_effect=OSError("fixture manifest interruption")):
		result = harness.run()
	assert not result.successful
	assert list((harness.source.data_root / "market_data").rglob("responses/*.json"))
	harness.database.fail_on = None
	assert harness.run().successful


def test_unknown_boundary_fails_before_database_or_provider(harness):
	harness.boundary = None
	harness.database.statements.clear()
	with pytest.raises(StageExecutionError):
		harness.run()
	assert harness.database.statements == [] and harness.provider.calls == []


def test_queries_and_fetches_are_bounded_and_defer_unpublished_session(harness):
	harness.clock.value = datetime(2024, 7, 8, 19, 30, tzinfo=timezone.utc)
	settings = replace(harness.settings, start=date(2024, 6, 20), end=date(2024, 7, 12))
	result = harness.run(settings)
	assert result.successful
	assert result.plan.excluded_before_history and result.plan.excluded_after_cutoff
	assert result.coverage.sessions[-1].state is CoverageState.NOT_YET_AVAILABLE
	assert all(date(2024, 7, 1) <= session.session_date <= date(2024, 7, 5) for source, session in harness.provider.calls)
	for statement, parameters in harness.database.statements:
		assert "MAX(" not in statement
		for value in parameters:
			if isinstance(value, date) and not isinstance(value, datetime):
				assert date(2024, 7, 1) <= value <= date(2024, 7, 5)


def test_archived_verification_avoids_full_download_until_explicit_recheck(harness):
	policy = replace(harness.settings.corrections, recent_window=timedelta(days=1))
	settings = replace(harness.settings, corrections=policy)
	harness.run(settings)
	harness.provider.calls.clear()
	harness.clock.value += timedelta(hours=2)
	assert harness.run(settings).successful and harness.provider.calls == []
	harness.clock.value += timedelta(days=8)
	assert harness.run(settings).successful and len(harness.provider.calls) == 5


def test_nonconsecutive_nonmonotonic_ids_do_not_change_calendar_dependencies(harness):
	harness.run()
	for old, new in ((1, 901), (2, 11), (3, 450), (4, 120), (5, 65)):
		harness.database.sql("UPDATE transaction_dates SET id=? WHERE id=?", (new, old))
		harness.database.sql("UPDATE raw_stock_data SET date_id=? WHERE date_id=?", (new, old))
		harness.database.sql("UPDATE daily_delta_stock_data SET date_id=? WHERE date_id=?", (new, old))
	result = harness.run()
	assert result.successful and result.downloaded_sessions == 0


def test_future_receipt_is_not_used_to_claim_historical_availability(harness):
	harness.run()
	harness.clock.value -= timedelta(days=1)
	result = harness.run()
	assert not result.successful
	assert all(item.state is not CoverageState.COMPLETE for item in result.coverage.sessions)


def test_bound_database_scope_rejects_different_adjustment(harness):
	from stockguidance.config.source import PriceAdjustment
	other = replace(harness.source, identity=replace(harness.source.identity, adjustment=PriceAdjustment.UNADJUSTED))
	with pytest.raises(ValueError):
		initialize_update_schema(other, harness.database)
	assert harness.database.rollbacks


def test_database_inspection_is_read_only_and_closes_connections(harness, calendar):
	harness.run()
	query = SessionQuery(harness.boundary, calendar.sessions(harness.settings.start, harness.settings.end), harness.clock.now())
	commits, closed = harness.database.commits, harness.database.closed
	harness.database.statements.clear()
	assert len(harness.repository.inspect(query)) == 5
	assert harness.database.commits == commits and harness.database.closed == closed + 1
	assert all(statement.startswith(("SELECT", "START TRANSACTION")) for statement, parameters in harness.database.statements)


def test_nontransactional_tables_fail_before_fetching_or_writing(harness):
	harness.database.engine = "MyISAM"
	harness.database.statements.clear()
	with pytest.raises(StageExecutionError):
		harness.run()
	assert harness.provider.calls == []
	assert all(statement.startswith(("SELECT", "START TRANSACTION")) for statement, parameters in harness.database.statements)


def test_policy_provenance_and_revision_are_persisted_without_configuration_secrets(harness):
	import json

	result = harness.run()
	paths = list(harness.source.data_root.rglob("runs/*.json"))
	assert len(paths) == 1
	payload = json.loads(paths[0].read_text())
	assert payload["data_revision"] == result.data_revision
	assert payload["derived_spec"] == "legacy-open-to-close-v1"
	assert payload["publication_policy"]["after_close_seconds"] == 900
	assert payload["correction_policy"]["provenance"] == "Fixture-only corrections"
	assert "credential" not in paths[0].read_text()


def test_a_session_published_during_the_run_prevents_premature_success(harness):
	harness.clock.value = datetime(2024, 7, 8, 20, 10, tzinfo=timezone.utc)
	provider = harness.provider
	original = provider.fetch

	def fetch(source, session):
		response = original(source, session)
		harness.clock.value = datetime(2024, 7, 8, 20, 20, tzinfo=timezone.utc)
		return response

	provider.fetch = fetch
	result = harness.run()
	assert not result.successful
	assert result.coverage.sessions[-1].session.session_date == date(2024, 7, 8)
	assert result.coverage.sessions[-1].state is CoverageState.UNVERIFIED


def test_run_metadata_write_failure_cannot_report_success(harness):
	with patch("stockguidance.workflows.update_metadata.atomic_write", side_effect=OSError("fixture disk failure")):
		with pytest.raises(StageExecutionError):
			harness.run()
	assert harness.run().successful
