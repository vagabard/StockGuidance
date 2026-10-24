from dataclasses import replace
from datetime import date
import json

import pytest

from zscores.test_storage import LockingDatabase
from support.ingestion import FakeDatabase
from stockguidance.cli.validate_database import parser
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.mysql_zscores import MySQLZScoreRepository
from stockguidance.database.table_names import load_table_names
from stockguidance.database.update_schema import initialize_update_schema, initialize_zscore_schema
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.validation.findings import FindingCategory, ValidationCheck
from stockguidance.validation.reporting import report_payload
from stockguidance.workflows.update_zscores import ZScoreUpdateSettings, update_zscores
from stockguidance.workflows.validate_database import ValidationSettings, validate_database


def _populate_zscores(harness, calendar):
	for day, close in ((1, "100"), (2, "110"), (3, "121"), (5, "133"), (8, "146")):
		session = calendar.session(date(2024, 7, day))
		harness.provider.responses[session.session_date] = harness.response(close=close)
	assert harness.run().successful
	initialize_zscore_schema(harness.source, harness.database, (2,))
	settings = ZScoreUpdateSettings(
		date(2024, 7, 1), date(2024, 7, 8), (2,),
		DailyPublicationPolicy(harness.settings.publication.after_close, "Fixture-only publication"),
	)
	result = update_zscores(harness.source, settings, harness.boundary, calendar, MySQLZScoreRepository(harness.source, LockingDatabase(harness.database)), harness.clock, initialized_schema=True)
	assert not result.failures, result


def _validate(harness, calendar, *, windows=(2,)):
	settings = ValidationSettings(
		harness.settings.start,
		harness.settings.end,
		harness.settings.publication,
		zscore_windows=windows,
	)
	return validate_database(
		harness.source,
		settings,
		harness.boundary,
		calendar,
		MySQLValidationRepository(harness.source, harness.database),
		harness.clock,
		zscore_repository=MySQLZScoreRepository(harness.source, LockingDatabase(harness.database)),
	)


def _date_id(harness, session_date):
	return harness.database.sql("SELECT id FROM transaction_dates WHERE transaction_date = ?", (session_date,))[0][0]


def test_validate_cli_parses_distinct_zscore_windows():
	assert parser().parse_args(["--zscore-windows", "02,20"]).zscore_windows == (2, 20)
	with pytest.raises(SystemExit):
		parser().parse_args(["--zscore-windows", "2,2"])


def test_zscore_validation_recomputes_both_families_and_is_read_only(harness, calendar):
	_populate_zscores(harness, calendar)
	commits = harness.database.commits
	files = {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	harness.database.statements.clear()

	result = _validate(harness, calendar)

	zscore_findings = [finding for finding in result.findings if finding.check is ValidationCheck.ZSCORE]
	assert not zscore_findings
	payload = report_payload(result)
	assert payload["zscore_validation"]["windows"] == [2]
	assert payload["zscore_validation"]["families"] == ["price", "price_delta"]
	assert harness.database.commits == commits
	assert files == {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	row_queries = [statement for statement, _ in harness.database.statements if statement.startswith("SELECT") and "zscores_" in statement]
	assert len(row_queries) == 2


@pytest.mark.parametrize("damage, expected_category", [("score", FindingCategory.INVALID), ("status", FindingCategory.INVALID), ("missing", FindingCategory.MISSING), ("stale", FindingCategory.UNVERIFIED)])
def test_zscore_validation_reports_tampering_staleness_and_missing_rows(harness, calendar, damage, expected_category):
	_populate_zscores(harness, calendar)
	date_id = _date_id(harness, date(2024, 7, 3))
	if damage == "score":
		harness.database.sql("UPDATE zscores_price_2 SET zscore = zscore + 1 WHERE date_id = ? AND ticker_id = 1", (date_id,))
	elif damage == "status":
		harness.database.sql("UPDATE zscores_price_2 SET zscore = NULL, status = 5 WHERE date_id = ? AND ticker_id = 1", (date_id,))
	elif damage == "missing":
		harness.database.sql("DELETE FROM zscores_price_2 WHERE date_id = ? AND ticker_id = 1", (date_id,))
	else:
		harness.database.sql("UPDATE raw_stock_data SET close = close + 1 WHERE date_id = ? AND ticker_id = 1", (date_id,))

	commits = harness.database.commits
	files = {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	result = _validate(harness, calendar)

	findings = [finding for finding in result.findings if finding.check is ValidationCheck.ZSCORE]
	assert findings
	assert any(finding.category is expected_category for finding in findings)
	assert any(finding.session_date == date(2024, 7, 3) for finding in findings)
	assert harness.database.commits == commits
	assert files == {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	if damage == "score":
		assert any(record.stored_score is not None and record.detail.startswith("stored score") for record in result.zscore_audit)
	payload = json.dumps(report_payload(result))
	assert "z-score" in payload.lower()


def test_ordinary_validation_does_not_require_optional_zscore_tables(harness, calendar):
	settings = ValidationSettings(harness.settings.start, harness.settings.end, harness.settings.publication)
	result = validate_database(
		harness.source,
		settings,
		harness.boundary,
		calendar,
		MySQLValidationRepository(harness.source, harness.database),
		harness.clock,
	)

	assert not [finding for finding in result.findings if finding.check is ValidationCheck.ZSCORE]
	assert report_payload(result)["zscore_validation"] is None


def test_requested_zscore_audit_reports_missing_optional_schema_without_writes(harness, calendar):
	assert harness.run().successful
	commits = harness.database.commits
	settings = ValidationSettings(date(2024, 7, 3), date(2024, 7, 8), harness.settings.publication, zscore_windows=(2,))
	result = validate_database(
		harness.source,
		settings,
		harness.boundary,
		calendar,
		MySQLValidationRepository(harness.source, harness.database),
		harness.clock,
		zscore_repository=MySQLZScoreRepository(harness.source, LockingDatabase(harness.database)),
	)

	assert any(finding.check is ValidationCheck.ZSCORE and finding.category is FindingCategory.FAILED for finding in result.findings)
	assert harness.database.commits == commits


def test_cross_stage_custom_registry_reaches_zscore_validation(harness, calendar, tmp_path):
	base = load_table_names()
	registry = replace(
		base,
		source="custom_source",
		sessions="custom_sessions",
		tickers="custom_tickers",
		prices="custom_prices",
		deltas="custom_deltas",
		imports="custom_imports",
		zscore_series="custom_zscore_series",
		zscore_sessions="custom_zscore_sessions",
		price_template="custom_price_{window}",
		price_delta_template="custom_delta_{window}",
	)
	names_path = tmp_path / "registry.json"
	names_path.write_text(json.dumps({"schema_version": 1, "tables": registry.tables(), "zscore_templates": registry.templates()}), encoding="utf-8")
	dbinfo = tmp_path / "custom-db.json"
	dbinfo.write_text(json.dumps({"host": "fixture", "database": "fixture", "user": "fixture", "table_names_file": names_path.name}), encoding="utf-8")
	source = replace(harness.source, database_config=dbinfo)
	database = FakeDatabase(tmp_path / "custom.sqlite")
	for day, close in ((1, "100"), (2, "110"), (3, "121"), (5, "133"), (8, "146")):
		session = calendar.session(date(2024, 7, day))
		harness.provider.responses[session.session_date] = harness.response(close=close)
	initialize_update_schema(source, database, registry)
	from stockguidance.workflows.update_market_data import update_market_data
	assert update_market_data(source, harness.settings, harness.boundary, calendar, harness.provider, MySQLMarketDataRepository(source, database, registry), harness.clock).successful
	initialize_zscore_schema(source, database, (2,), registry)
	settings = ZScoreUpdateSettings(date(2024, 7, 3), date(2024, 7, 8), (2,), harness.settings.publication)
	assert not update_zscores(source, settings, harness.boundary, calendar, MySQLZScoreRepository(source, LockingDatabase(database), registry), harness.clock).failures
	validation_settings = ValidationSettings(date(2024, 7, 3), date(2024, 7, 8), harness.settings.publication, zscore_windows=(2,))
	result = validate_database(
		source,
		validation_settings,
		harness.boundary,
		calendar,
		MySQLValidationRepository(source, database, registry),
		harness.clock,
		zscore_repository=MySQLZScoreRepository(source, LockingDatabase(database), registry),
	)

	assert not [finding for finding in result.findings if finding.check is ValidationCheck.ZSCORE]
	assert {"custom_price_2", "custom_delta_2"} <= {row[0] for row in database.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
