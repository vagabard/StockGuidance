from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.maintain_daily import maintenance_range, main, parser
from stockguidance.config.source import DataConfiguration, SourceConfig
from stockguidance.database.table_names import load_table_names
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.update_schema import initialize_update_schema, initialize_zscore_schema
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.contracts import DownloadedSession, ProviderResponse, ResponseState
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.records import DailyBar, decode_bar, records_id
from stockguidance.legacy_migration.precision import ExactArchiveBar, exact_records_id
from stockguidance.workflows import daily_maintenance as workflow


def test_cli_defaults_and_overrides():
	arguments = parser().parse_args([])
	assert arguments.adjustment is None
	assert arguments.publication_delay_minutes == 15
	assert arguments.recent_refresh_hours == 0
	assert arguments.archive_refresh_days == 30
	assert not hasattr(arguments, "java_manifest")
	assert parser().parse_args(["--archive-refresh-days", "7"]).archive_refresh_days == 7
	assert maintenance_range(date(2024, 2, 29)) == (date(2022, 2, 28), date(2024, 2, 29))


@pytest.fixture
def calendar():
	return ExchangeCalendar("XNYS", date(2023, 1, 1), date(2025, 1, 31))


def test_missed_days_holidays_and_publication_are_inferred(calendar):
	policy = DailyPublicationPolicy(timedelta(minutes=15), "test")
	raw, targets = workflow.session_workload(calendar, date(2024, 7, 1), date(2024, 7, 7), datetime(2024, 7, 5, 20, 10, tzinfo=timezone.utc), policy)
	assert [session.session_date for session in targets] == [date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3)]
	assert raw[0] == calendar.shift(targets[0], -61)
	assert raw[-1] == targets[-1]
	assert len(raw) == 64


def test_historical_range_loads_following_context_without_adding_training_targets(calendar):
	policy = DailyPublicationPolicy(timedelta(minutes=15), "test")
	raw, targets = workflow.session_workload(calendar, date(2024, 7, 1), date(2024, 7, 2), datetime(2024, 7, 10, 22, tzinfo=timezone.utc), policy)
	assert targets[-1].session_date == date(2024, 7, 2)
	assert raw[-1] == calendar.shift(targets[-1], 3)


def source_fixture(tmp_path):
	configuration = DataConfiguration("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, True, False)
	return SourceConfig(configuration.to_source_identity(), tmp_path / "data", tmp_path / "dbinfo.id", "test", _configuration=configuration)


def response_fixture(session, close="11"):
	bar = DailyBar("ABC", Decimal("10.0000"), Decimal("12.0000"), Decimal("9.0000"), Decimal(close).quantize(Decimal("0.00000001")), Decimal("123.4"), Decimal("10.1234567890123456789012"), 2)
	timestamp = int(session.opens_at.timestamp() * 1000)
	return ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1),), False, source_timestamps=(("ABC", timestamp),))


def test_operational_precision_and_identity_match_migration():
	bar = decode_bar({"T": "ABC", "o": "10.12345678", "h": "12", "l": "9", "c": "11.12345678", "v": "123.00000001", "vw": "10.1234567890123456789012", "n": 2})
	assert bar.open == Decimal("10.12345678")
	assert bar.vwap == Decimal("10.1234567890123456789012")
	exact = ExactArchiveBar(bar.ticker, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)
	assert records_id((bar,)) == exact_records_id((exact,))


def test_workload_contains_all_dependencies_and_changes_when_source_changes(tmp_path, calendar):
	source = source_fixture(tmp_path)
	store = ManifestStore(source.data_root)
	cutoff = datetime(2024, 7, 5, 20, 10, tzinfo=timezone.utc)
	raw, targets = workflow.session_workload(calendar, date(2024, 7, 1), date(2024, 7, 3), cutoff, DailyPublicationPolicy(timedelta(minutes=15), "test"))
	files, availability = {}, {}
	for session in raw:
		manifest = store.save_download(source.identity, DownloadedSession(session, response_fixture(session), cutoff))
		files[session.session_date] = workflow.export_archive(source, manifest)
		availability[session.session_date] = cutoff.isoformat()
	workload = workflow.build_workload(source, load_table_names(), calendar, raw, targets, files, availability)
	assert workload["kind"] == "daily-maintenance"
	assert workload["calendar_id"] == calendar.calendar_id
	assert workload["zscore_sessions"] == [session.session_date.isoformat() for session in targets]
	assert len(workload["targets"][-1]["zscore_dependencies"]) == 61
	assert workload["targets"][-1]["derived_dependencies"][-1] == "2024-07-05"
	assert workload["files"][-1]["valid"] is False
	identity = workload.pop("manifest_id")
	assert content_id(workload) == identity
	changed = store.save_download(source.identity, DownloadedSession(raw[-1], response_fixture(raw[-1], "11.5"), cutoff))
	files[raw[-1].session_date] = workflow.export_archive(source, changed)
	assert workflow.build_workload(source, load_table_names(), calendar, raw, targets, files, availability)["manifest_id"] != identity


class FakeCursor:
	def execute(self, query, parameters):
		self.query = query

	def fetchall(self):
		if "GET_LOCK" in self.query or "IS_USED_LOCK" in self.query:
			return [(1,)]
		if "information_schema.TABLES" in self.query:
			return [("InnoDB",)]
		if "SELECT source_family" in self.query:
			return [("polygon_massive", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, 1, 0, None)]
		if "SELECT session_date FROM" in self.query or "SELECT s.transaction_date" in self.query:
			return []
		return []

	def close(self):
		pass


class FakeConnection:
	def cursor(self):
		return FakeCursor()

	def rollback(self):
		pass

	def commit(self):
		pass

	def close(self):
		pass


def test_workflow_resumes_downloads_before_validation(tmp_path, monkeypatch):
	source = source_fixture(tmp_path)
	source.database_config.write_text(json.dumps({"database": "test", "user": "test"}))
	arguments = parser().parse_args(["--data-root", str(source.data_root), "--dbinfo-file", str(source.database_config),
		"--start-date", "2024-07-01", "--end-date", "2024-07-03", "--java-checkpoint-directory", str(tmp_path / "checkpoints")])
	cutoff = datetime(2024, 7, 5, 20, 10, tzinfo=timezone.utc)
	monkeypatch.setattr(workflow, "SystemClock", lambda: SimpleNamespace(now=lambda: cutoff))
	monkeypatch.setattr(workflow, "java_preflight", lambda _: [])
	monkeypatch.setattr(workflow, "MySQLConnectionFactory", lambda: lambda _: FakeConnection())
	for name in ("assert_source", "assert_transactional_tables", "assert_zscore_tables"):
		monkeypatch.setattr(workflow, name, lambda *args, **kwargs: None)
	fetched = []
	fail_java = [True]

	def fetch(source, session):
		fetched.append(session.session_date)
		return response_fixture(session)

	def execute(arguments, command, workload, checkpoint):
		if fail_java[0]:
			raise RuntimeError("interrupted after download")
		assert json.loads(workload.read_text())["kind"] == "daily-maintenance"
		return {"status": "complete"}

	def validate(source, names, workload, report, directory):
		assert not fail_java[0]
		assert arguments.adjustment == "adjusted"
		assert arguments.start_date == date(2024, 7, 1)
		assert arguments.end_date == date(2024, 7, 3)
		return {"successful": True}

	monkeypatch.setattr(workflow, "ConfiguredPolygonProvider", lambda _: SimpleNamespace(fetch=fetch))
	monkeypatch.setattr(workflow, "execute_java", execute)
	monkeypatch.setattr(workflow, "validate_materialized_zscores", validate)
	with pytest.raises(RuntimeError, match="interrupted"):
		workflow.run_daily_maintenance(arguments)
	first_count = len(fetched)
	fail_java[0] = False
	result = workflow.run_daily_maintenance(arguments)
	assert result["successful"]
	assert result["training_outcomes_through"] == "2024-07-02"
	assert result["forecast_session"] == "2024-07-03"
	assert len(fetched) - first_count < first_count


def test_cli_failure_is_nonzero_and_machine_readable(capsys):
	assert main([], run=lambda _: {"successful": False}) == 2
	assert json.loads(capsys.readouterr().out)["successful"] is False


@pytest.mark.skipif(os.environ.get("STOCKGUIDANCE_MYSQL_INTEGRATION") != "1", reason="Explicit local MySQL integration opt-in")
def test_real_java_mysql_update_resume_correction_and_vectorized_validation(tmp_path, monkeypatch):
	from pathlib import Path
	base = load_database_configuration(Path("dbinfo.id"))
	admin = MySQLConnectionFactory()(Path("dbinfo.id"))
	cursor = admin.cursor()
	schema = "stockguidance_maintenance_test_" + uuid4().hex[:16]
	assert re.fullmatch(r"stockguidance_maintenance_test_[0-9a-f]{16}", schema)
	assert schema != base.database
	cursor.execute(f"CREATE DATABASE `{schema}`", ())
	try:
		config_path = tmp_path / "dbinfo.id"
		payload = {"host": base.host, "database": schema, "port": base.port, "user": base.user, "password": base.password}
		if base.table_names_file is not None:
			payload["table_names_file"] = str(base.table_names_file)
		config_path.write_text(json.dumps(payload))
		source = source_fixture(tmp_path)
		initialize_update_schema(source, MySQLConnectionFactory(), base.load_table_names())
		initialize_zscore_schema(source, MySQLConnectionFactory(), (20, 60), base.load_table_names())
		connection = MySQLConnectionFactory()(config_path)
		test_cursor = connection.cursor()
		test_cursor.execute("CREATE TABLE stockguidance_migration_completion (workload VARCHAR(32) NOT NULL, session_date DATE NOT NULL, series_key VARCHAR(64) NOT NULL, content_digest CHAR(64) NOT NULL, committed_at VARCHAR(40) NOT NULL, PRIMARY KEY(workload,session_date,series_key)) ENGINE=InnoDB", ())
		connection.commit()
		test_cursor.close()
		connection.close()
		arguments = parser().parse_args(["--data-root", str(source.data_root), "--dbinfo-file", str(config_path),
			"--start-date", "2024-07-01", "--end-date", "2024-07-03", "--java-checkpoint-directory", str(tmp_path / "checkpoints"), "--batch-sessions", "2"])
		cutoff = datetime(2024, 7, 5, 20, 10, tzinfo=timezone.utc)
		monkeypatch.setattr(workflow, "SystemClock", lambda: SimpleNamespace(now=lambda: cutoff))
		corrected = [False]

		def fetch(source, session):
			close = "11.5" if corrected[0] and session.session_date == date(2024, 7, 2) else str(10 + session.sequence % 15 / 10)
			return response_fixture(session, close)

		monkeypatch.setattr(workflow, "ConfiguredPolygonProvider", lambda _: SimpleNamespace(fetch=fetch))
		first = workflow.run_daily_maintenance(arguments)
		assert first["successful"], first["python_validation"]
		second = workflow.run_daily_maintenance(arguments)
		assert second["successful"], second["python_validation"]
		assert sum(report["raw_changed"] for report in second["java_updates"]) == 0
		assert sum(report["derived_changed"] for report in second["java_updates"]) == 0
		connection = MySQLConnectionFactory()(config_path)
		test_cursor = connection.cursor()
		test_cursor.execute("UPDATE zscores_price_20 z JOIN transaction_dates d ON d.id = z.date_id SET z.zscore = 99 WHERE d.transaction_date = %s", (date(2024, 7, 1),))
		connection.commit()
		test_cursor.close()
		connection.close()
		corrected[0] = True
		original_execute = workflow.execute_java
		def interrupt_after_commit(*args):
			original_execute(*args)
			raise RuntimeError("coordinator interrupted after committed Java batch")
		monkeypatch.setattr(workflow, "execute_java", interrupt_after_commit)
		with pytest.raises(RuntimeError, match="coordinator interrupted"):
			workflow.run_daily_maintenance(arguments)
		monkeypatch.setattr(workflow, "execute_java", original_execute)
		third = workflow.run_daily_maintenance(arguments)
		assert third["successful"], third["python_validation"]
		assert sum(report["raw_changed"] for report in third["java_updates"]) == 0
		assert sum(report["derived_changed"] for report in third["java_updates"]) == 0
	finally:
		cursor.execute(f"DROP DATABASE `{schema}`", ())
		cursor.close()
		admin.close()
