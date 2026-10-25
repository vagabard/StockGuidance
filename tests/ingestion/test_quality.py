from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stockguidance.market_data.quality import HistoryMode, ObservedBar, QualityFlag, QualityPolicy, QualityRecord, interpolate
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import ZScoreInput, ZScoreObservation
from stockguidance.zscores.specification import ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.results import ZScoreStatus
from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.zscores import ZScoreInputQuery
from stockguidance.database.repositories.mysql_zscores import MySQLZScoreRepository
from stockguidance.database.update_schema import initialize_zscore_schema
from stockguidance.zscores.storage import ZScoreSeriesDefinition
from stockguidance.market_data.identity import source_key
from support.ingestion import FakeConnection, FakeCursor
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.workflows.daily_maintenance_source import export_archive, archive_identity
from pathlib import Path
import re
import json


NOW = datetime(2024, 7, 10, 23, tzinfo=timezone.utc)


def observed(sequence, price, *, ticker="AAA", volume=100):
	return ObservedBar(ticker, date(2024, 7, 1) + timedelta(days=sequence), sequence, (price, price, price, price, volume, None, 0), NOW - timedelta(days=10 - sequence))


@pytest.mark.parametrize("gap", [1, 2, 5])
def test_interpolation_preserves_originals_and_classifies_gap(gap):
	inputs = (observed(0, 10), *(observed(index, 0) for index in range(1, gap + 1)), observed(gap + 1, 10 + gap + 1))
	records = interpolate(inputs, NOW)
	assert len(records) == gap
	for index, record in enumerate(records, 1):
		assert record.observation.values[0] == 0
		assert record.effective[0] == Decimal(10 + index)
		assert record.effective[4] == 100
		assert record.gap_length == gap
		assert record.flags == QualityFlag.BAD | (QualityFlag.INTERPOLATED_SINGLE if gap == 1 else QualityFlag.INTERPOLATED_GAP)
		assert QualityRecord.from_payload(record.payload()).revision == record.revision


def test_missing_endpoint_is_unresolved_and_valid_zero_quantity_is_preserved():
	record = interpolate((observed(0, 0, volume=0), observed(1, 10)), NOW)[0]
	assert record.flags == QualityFlag.BAD
	assert record.effective is None
	assert interpolate((observed(0, 10, volume=0),), NOW) == ()


def test_availability_and_retrospective_leakage():
	record = interpolate((observed(0, 10), observed(1, 0), observed(2, 12)), NOW)[0]
	cutoff = NOW - timedelta(days=1)
	assert record.usable(QualityPolicy(HistoryMode.INTERPOLATED), cutoff)[0] is None
	values, flags = record.usable(QualityPolicy(HistoryMode.INTERPOLATED, True), cutoff)
	assert values is not None
	assert flags & QualityFlag.FUTURE_LEAKAGE
	assert record.usable(QualityPolicy(), NOW)[0] is None


def test_zscore_window_retains_quality_and_clean_history_recovers(calendar):
	sessions = calendar.sessions(date(2024, 7, 1), date(2024, 7, 10))
	values = [ZScoreObservation.ready(10 + index) for index in range(len(sessions))]
	values[1] = replace(values[1], quality=QualityFlag.BAD | QualityFlag.INTERPOLATED_GAP, gap_length=2, provenance=("repair-revision",))
	inputs = ZScoreInput.from_values(ZScoreFamily.PRICE, sessions, {1: tuple(values)})
	strict = calculate_zscores(ZScoreSpecification(ZScoreFamily.PRICE, (3,)), inputs)
	assert strict.row(3, sessions[2], 1).status is ZScoreStatus.INSUFFICIENT_HISTORY
	assert strict.row(3, sessions[4], 1).status is ZScoreStatus.READY
	estimated = calculate_zscores(ZScoreSpecification(ZScoreFamily.PRICE, (3,), quality_policy=QualityPolicy(HistoryMode.INTERPOLATED)), inputs)
	row = estimated.row(3, sessions[2], 1)
	assert row.status is ZScoreStatus.READY
	assert row.quality & QualityFlag.INTERPOLATED_GAP
	assert row.gap_length == 2
	assert row.provenance == ("repair-revision",)


def test_bad_source_rows_are_persisted_repaired_and_audited(harness, calendar):
	bad_date = date(2024, 7, 2)
	harness.provider.responses[bad_date] = ProviderResponse(ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 1),), bad_records=(("AAA", (0, 0, 0, 0, 0, None, 0)),))
	result = harness.run()
	assert result.successful
	rows = harness.database.sql("SELECT flags,payload FROM stockguidance_row_quality")
	assert len(rows) == 1
	assert rows[0][0] == int(QualityFlag.BAD | QualityFlag.INTERPOLATED_SINGLE)
	assert json.loads(rows[0][1])["original"] == ["0", "0", "0", "0", "0", None, "0"]
	assert harness.database.sql("SELECT close FROM raw_stock_data r JOIN transaction_dates s ON s.id=r.date_id WHERE s.transaction_date=?", (bad_date.isoformat(),)) == [(0,)]
	query = SessionQuery(harness.boundary, calendar.sessions(date(2024, 7, 1), date(2024, 7, 8)), harness.clock.now())
	snapshot = next(item for item in harness.repository.inspect(query) if item.session.session_date == bad_date)
	assert snapshot.records[0].close == Decimal(11)
	assert snapshot.raw_verified
	audit_count = harness.database.sql("SELECT COUNT(*) FROM stockguidance_quality_revisions")
	assert harness.run().successful
	assert harness.database.sql("SELECT COUNT(*) FROM stockguidance_quality_revisions") == audit_count
	assert harness.database.sql("SELECT COUNT(*) FROM stockguidance_output_quality WHERE series_key='legacy-derived'")[0][0] > 0


def test_completed_large_scope_reads_metadata_without_response_decoding(harness, calendar, monkeypatch):
	harness.clock.value = datetime(2024, 9, 30, 23, tzinfo=timezone.utc)
	settings = replace(harness.settings, end=date(2024, 9, 30), corrections=replace(harness.settings.corrections, recent_recheck=timedelta(days=365), archive_recheck=timedelta(days=365)))
	assert harness.run(settings_override=settings).successful
	harness.database.statements.clear()
	monkeypatch.setattr("stockguidance.market_data.manifests.decode_response", lambda value: pytest.fail("Completed scope decoded a full response"))
	assert harness.run(settings_override=settings).successful
	queries = [operation for operation, _ in harness.database.statements]
	assert not any("open, high, low, close" in operation or "GROUP BY date_id" in operation or "SELECT date_id, ticker_id FROM" in operation for operation in queries)


def test_unchanged_scheduled_downloads_do_not_reload_historical_records(harness, monkeypatch):
	harness.clock.value = datetime(2024, 9, 30, 23, tzinfo=timezone.utc)
	settings = replace(harness.settings, end=date(2024, 9, 30))
	first = harness.run(settings_override=settings)
	assert first.successful
	harness.clock.value += timedelta(hours=2)
	harness.database.statements.clear()
	monkeypatch.setattr("stockguidance.market_data.manifests.decode_response", lambda value: pytest.fail("Refresh planning decoded an old response"))
	second = harness.run(settings_override=settings)
	assert second.successful
	assert second.downloaded_sessions > 0
	assert second.data_revision == first.data_revision
	assert not any("open, high, low, close" in operation or "GROUP BY date_id" in operation for operation, _ in harness.database.statements)
	assert harness.run(settings_override=settings).downloaded_sessions == 0


def test_quality_scan_resume_is_idempotent_and_checkpoints_are_temporary(harness, calendar):
	assert harness.run().successful
	settings = replace(harness.settings, scan_quality=True)
	assert harness.run(settings_override=settings).successful
	assert harness.database.sql("SELECT COUNT(*) FROM stockguidance_quality_scans") == [(0,)]
	assert harness.run(settings_override=settings).successful


def test_source_correction_cleans_current_quality_and_preserves_audit(harness):
	bad_date = date(2024, 7, 2)
	harness.provider.responses[bad_date] = ProviderResponse(ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 1),), bad_records=(("AAA", (0, 0, 0, 0, 0, None, 0)),))
	assert harness.run().successful
	assert harness.database.sql("SELECT COUNT(*) FROM stockguidance_row_quality") == [(1,)]
	harness.provider.responses[bad_date] = harness.response()
	harness.clock.value += timedelta(hours=2)
	assert harness.run().successful
	assert harness.database.sql("SELECT COUNT(*) FROM stockguidance_row_quality") == [(0,)]
	assert harness.database.sql("SELECT COUNT(*) FROM stockguidance_quality_revisions")[0][0] >= 1


class LockingCursor(FakeCursor):
	def execute(self, operation, parameters):
		return super().execute(re.sub(r" FOR UPDATE(?=\s|$)", "", operation), parameters)


class LockingConnection(FakeConnection):
	def cursor(self):
		return LockingCursor(self.connection.cursor(), self.database)


def test_published_zscores_round_trip_sticky_quality(harness, calendar):
	bad_date = date(2024, 7, 2)
	harness.provider.responses[bad_date] = ProviderResponse(ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 1),), bad_records=(("AAA", (0, 0, 0, 0, 0, None, 0)),))
	assert harness.run().successful
	policy = QualityPolicy(HistoryMode.INTERPOLATED)
	repository = MySQLZScoreRepository(harness.source, lambda configuration: LockingConnection(harness.database), quality_policy=policy)
	sessions = calendar.sessions(date(2024, 7, 1), date(2024, 7, 8))
	query = ZScoreInputQuery(SessionQuery(harness.boundary, sessions, harness.clock.now()), ZScoreFamily.PRICE, (1,))
	loaded = repository.load_inputs(query)
	specification = ZScoreSpecification(ZScoreFamily.PRICE, (2,), quality_policy=policy)
	result = calculate_zscores(specification, loaded.input)
	definition = ZScoreSeriesDefinition.from_specification(source_key(harness.source.identity), calendar.calendar_id, specification, 2)
	initialize_zscore_schema(harness.source, harness.database, (2,))
	repository.publish(definition, result, loaded.dependencies, harness.clock.now())
	read = repository.read_verified(definition, loaded.dependencies[1])
	assert read.rows[0].quality == QualityFlag.BAD | QualityFlag.INTERPOLATED_SINGLE
	assert read.rows[0].gap_length == 1
	assert read.rows[0].provenance


def test_source_export_preserves_bad_rows_and_timestamps(harness, calendar):
	session = calendar.session(date(2024, 7, 2))
	timestamp = int(session.opens_at.timestamp() * 1000)
	response = ProviderResponse(ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 1),), source_timestamps=(("TTE", timestamp),), bad_records=(("TTE", (0, 0, 0, 0, 0, None, 0)),))
	entry = export_archive(harness.source, DownloadedSession(session, response, harness.clock.now()))
	path = Path(entry["path"])
	content = path.read_bytes()
	row = json.loads(content)
	assert row == {"T": "TTE", "o": 0, "h": 0, "l": 0, "c": 0, "v": 0, "vw": None, "n": 0, "t": timestamp}
	assert archive_identity(path) == (entry["file_sha256"], entry["records_id"])
	assert path.read_bytes() == content


def test_quality_overlay_survives_database_rounding_of_invalid_counts(harness, calendar):
	bad_date = date(2024, 7, 2)
	harness.provider.responses[bad_date] = ProviderResponse(ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 1),), bad_records=(("AAA", (10, 15, 9, 11, 100, 10, 1.5)),))
	assert harness.run().successful
	date_id = harness.database.sql("SELECT id FROM transaction_dates WHERE transaction_date=?", (bad_date.isoformat(),))[0][0]
	harness.database.sql("UPDATE raw_stock_data SET transactions=1 WHERE date_id=?", (date_id,))
	query = SessionQuery(harness.boundary, calendar.sessions(date(2024, 7, 1), date(2024, 7, 8)), harness.clock.now())
	snapshot = next(item for item in harness.repository.inspect(query) if item.session.session_date == bad_date)
	assert snapshot.raw_verified
	assert snapshot.records[0].transactions == 5
