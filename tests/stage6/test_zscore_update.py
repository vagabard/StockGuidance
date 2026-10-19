from datetime import date, datetime, timezone
from pathlib import Path

import pytest

pytest_plugins = ("stage3.conftest",)

from stage5.test_zscore_storage import LockingDatabase
from stockguidance.cli.update_zscores import parse_windows
from stockguidance.database.repositories.mysql_zscores import MySQLZScoreRepository
from stockguidance.database.update_schema import initialize_zscore_schema
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.workflows.update_zscores import ZScoreUpdateSettings, update_zscores


def test_windows_parser_requires_distinct_integer_values():
	assert parse_windows("02,20") == (2, 20)
	for value in ("", "1", "2,2", "2,not-a-window"):
		with pytest.raises(Exception):
			parse_windows(value)


def _file_snapshot(root: Path) -> dict[str, bytes]:
	return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_local_update_warms_exchange_sessions_writes_both_families_and_is_idempotent(harness, calendar):
	assert harness.run().successful
	initialize_zscore_schema(harness.source, harness.database, (2,))
	repository = MySQLZScoreRepository(harness.source, LockingDatabase(harness.database))
	settings = ZScoreUpdateSettings(
		date(2024, 7, 3), date(2024, 7, 8), (2,),
		DailyPublicationPolicy(harness.settings.publication.after_close, "Fixture-only publication"),
	)
	before_files = _file_snapshot(harness.source.data_root)
	provider_calls = len(harness.provider.calls)
	first = update_zscores(harness.source, settings, harness.boundary, calendar, repository, harness.clock, initialized_schema=True)

	assert first.exit_code == 0
	assert [item.session_date for item in first.effective_sessions] == [date(2024, 7, 3), date(2024, 7, 5), date(2024, 7, 8)]
	assert [item.session_date for item in first.warmup_sessions] == [date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3), date(2024, 7, 5), date(2024, 7, 8)]
	assert len(first.rebuilt_results) == 6
	assert first.ready_count == 0
	assert first.unavailable_count == 6
	assert len(first.status_counts) == 2
	assert _file_snapshot(harness.source.data_root) == before_files
	assert len(harness.provider.calls) == provider_calls

	second = update_zscores(harness.source, settings, harness.boundary, calendar, repository, harness.clock)
	assert second.exit_code == 0
	assert not second.written_sessions
	assert len(second.skipped_results) == 6
	assert not second.rebuilt_results
	assert _file_snapshot(harness.source.data_root) == before_files


def test_update_does_not_write_results_before_publication_cutoff(harness, calendar):
	assert harness.run().successful
	initialize_zscore_schema(harness.source, harness.database, (2,))
	settings = ZScoreUpdateSettings(
		date(2024, 7, 8), date(2024, 7, 8), (2,),
		DailyPublicationPolicy(harness.settings.publication.after_close, "Fixture-only publication"),
	)
	harness.clock.value = datetime(2024, 7, 8, 19, 0, tzinfo=timezone.utc)
	result = update_zscores(harness.source, settings, harness.boundary, calendar, MySQLZScoreRepository(harness.source, LockingDatabase(harness.database)), harness.clock)

	assert result.exit_code == 2
	assert not result.effective_sessions
	assert result.skipped_sessions[0].reason == "not_yet_available_at_cutoff"
	assert not result.written_sessions


def test_publication_failure_is_reported_as_storage_failure(harness, calendar, monkeypatch):
	assert harness.run().successful
	initialize_zscore_schema(harness.source, harness.database, (2,))
	repository = MySQLZScoreRepository(harness.source, LockingDatabase(harness.database))
	def fail_publication(*args, **kwargs):
		raise RuntimeError("fixture storage failure")
	monkeypatch.setattr(repository, "publish", fail_publication)
	settings = ZScoreUpdateSettings(
		date(2024, 7, 3), date(2024, 7, 3), (2,),
		DailyPublicationPolicy(harness.settings.publication.after_close, "Fixture-only publication"),
	)
	result = update_zscores(harness.source, settings, harness.boundary, calendar, repository, harness.clock)

	assert result.exit_code == 1
	assert result.failures == ("price z-score window 2 publication failed", "price_delta z-score window 2 publication failed")
	assert not result.rebuilt_results
