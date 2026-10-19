from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

from stage3.conftest import FakeDatabase

from stockguidance.cli import validation_setup
from stockguidance.config.source import SourceConfig
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.repositories.closes import CloseQuery
from stockguidance.database.repositories.market_data import DerivedUpdate
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.repositories.mysql_sessions import MySQLSessionRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.table_names import TableRole, load_table_names, quote_identifier
from stockguidance.database.update_schema import initialize_update_schema
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.contracts import DownloadedSession, ProviderResponse, ResponseState
from stockguidance.market_data.records import DailyBar
from stockguidance.market_data.update_planning import SnapshotIndex


def _custom_registry(tmp_path: Path):
	base = load_table_names()
	custom = replace(
		base,
		source="fixture_source_custom",
		sessions="fixture_sessions_custom",
		tickers="fixture_tickers_custom",
		prices="fixture_prices_custom",
		deltas="fixture_deltas_custom",
		imports="fixture_imports_custom",
	)
	path = tmp_path / "table-names.json"
	path.write_text(json.dumps({"schema_version": 1, "tables": custom.tables(), "zscore_templates": custom.templates()}), encoding="utf-8")
	return load_table_names(path)


def test_all_python_sql_consumers_use_a_custom_registry(source, boundary, calendar, clock, tmp_path):
	registry = _custom_registry(tmp_path)
	database_config = tmp_path / "dbinfo.id"
	database_config.write_text(json.dumps({"host": "fixture", "database": "fixture", "user": "fixture", "password": ""}), encoding="utf-8")
	source_config = SourceConfig(source, tmp_path / "data", database_config, "fixture-key-reference")
	database = FakeDatabase(tmp_path / "custom.sqlite")
	initialize_update_schema(source_config, database, registry)
	session = calendar.session(boundary.history_start)
	query = SessionQuery(boundary, (session,), clock.now())
	bar = DailyBar("AAA", Decimal("10.0000"), Decimal("11.0000"), Decimal("9.0000"), Decimal("10.0000"), 100, Decimal("10.0000"), 5)
	response = ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1),))
	download = DownloadedSession(session, response, session.closes_at + timedelta(minutes=15))
	repository = MySQLMarketDataRepository(source_config, database, registry)
	repository.import_session(query, download)
	snapshot = repository.inspect(query)[0]
	dependency = SnapshotIndex((snapshot,)).dependency(snapshot)
	update = DerivedUpdate(snapshot, (LegacyDerivedRow("AAA", (None,) * len(DERIVED_COLUMNS)),), dependency)
	repository.replace_derived(query, update)

	assert MySQLSessionRepository(source_config, database, registry).inventory(calendar.start, calendar.end, calendar.calendar_id).observations
	validation = MySQLValidationRepository(source_config, database, registry)
	inspection = validation.inspect(query)
	assert not inspection.findings
	manifest_store = ManifestStore(source_config.data_root)
	manifest = manifest_store.save_download(source, download)
	manifest_store.mark_complete(manifest)
	close = VerifiedCloseRepository(source_config, validation).read(CloseQuery(query, (1,)))
	assert close.observations[0].close == 10.0

	prices = quote_identifier(registry.table(TableRole.PRICES))
	database.sql(f"INSERT INTO {prices} (ticker_id, date_id, open, high, low, close, volume, vwap, transactions) VALUES (999, 999, 10, 10, 10, 10, 1, 10, 1)")
	orphaned = validation.inspect(query)
	assert any("fixture_prices_custom" in finding.detail for finding in orphaned.findings)
	assert all(registry.table(role) in " ".join(operation for operation, _ in database.statements) for role in (TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES, TableRole.DELTAS, TableRole.IMPORTS))


def test_selected_registry_failure_happens_before_validation_connection(tmp_path, monkeypatch):
	dbinfo = tmp_path / "dbinfo.id"
	dbinfo.write_text(json.dumps({"host": "fixture", "database": "fixture", "user": "fixture", "table_names_file": "missing.json"}), encoding="utf-8")
	arguments = SimpleNamespace(
		data_root=str(tmp_path / "data"), dbinfo_file=str(dbinfo), adjustment="adjusted", ticker_ids=None,
		lookback_sessions=None, signal_mode="previous_close", as_of=None, start_date=None, end_date=None,
		history_start=None, history_evidence=None, publication_delay_minutes=None, publication_evidence=None,
	)
	connection_calls = []
	monkeypatch.setattr(validation_setup, "MySQLConnectionFactory", lambda: connection_calls.append(True))

	try:
		validation_setup.assemble_and_validate(arguments)
	except ValueError as error:
		assert "table-names" in str(error) or "database" in str(error)
	else:
		raise AssertionError("Invalid selected registry was accepted")
	assert connection_calls == []


def test_database_configuration_keeps_selected_registry_explicit(tmp_path):
	registry = _custom_registry(tmp_path)
	names_path = tmp_path / "table-names.json"
	config_path = tmp_path / "dbinfo.id"
	config_path.write_text(json.dumps({"host": "fixture", "database": "fixture", "user": "fixture", "table_names_file": names_path.name}), encoding="utf-8")
	configuration = load_database_configuration(config_path)
	assert configuration.load_table_names() == registry
