from datetime import date

import pytest

from stockguidance.validation.findings import FindingCategory, ValidationCheck
from stockguidance.validation.reporting import report_payload


def checks(result, check):
	return [finding for finding in result.findings if finding.check is check]


@pytest.mark.parametrize("table", ["raw_stock_data", "daily_delta_stock_data"])
def test_duplicate_records_are_not_hidden_by_validating_unique_snapshots(harness, validator, table):
	harness.run()
	harness.database.sql(f"ALTER TABLE {table} RENAME TO original_rows")
	harness.database.sql(f"CREATE TABLE {table} AS SELECT * FROM original_rows")
	harness.database.sql(f"INSERT INTO {table} SELECT * FROM original_rows WHERE date_id = 1")
	result = validator.run()
	assert not result.complete and not result.failed
	assert checks(result, ValidationCheck.DUPLICATE)[0].tickers == ("AAA",)
	payload = report_payload(result)
	assert payload["sessions"][0]["raw_count" if table == "raw_stock_data" else "derived_count"] == 2


def test_duplicate_date_mapping_is_a_typed_integrity_finding(harness, validator):
	harness.run()
	harness.database.sql("ALTER TABLE transaction_dates RENAME TO old_dates")
	harness.database.sql("CREATE TABLE transaction_dates AS SELECT * FROM old_dates")
	harness.database.sql("INSERT INTO transaction_dates VALUES (900, '2024-07-01')")
	result = validator.run()
	assert not result.complete and not result.failed
	assert any(finding.session_date == date(2024, 7, 1) for finding in checks(result, ValidationCheck.DUPLICATE))


@pytest.mark.parametrize("table", ["raw_stock_data", "daily_delta_stock_data"])
@pytest.mark.parametrize("mapping", ["ticker", "date"])
def test_orphan_records_survive_left_join_inspection(harness, validator, table, mapping):
	harness.run()
	if mapping == "ticker":
		harness.database.sql(f"UPDATE {table} SET ticker_id = 999 WHERE date_id = 1")
	else:
		harness.database.sql(f"UPDATE {table} SET date_id = 999 WHERE date_id = 1")
	result = validator.run()
	assert checks(result, ValidationCheck.ORPHAN)
	assert not result.complete
	assert any(999 in finding.record_ids for finding in checks(result, ValidationCheck.ORPHAN))


def test_derived_orphan_raw_key_is_reported_without_assuming_ticker_lifetimes(harness, validator):
	harness.run()
	harness.database.sql("INSERT INTO ticker_symbol (id, label) VALUES (900, 'ORPHAN')")
	harness.database.sql("INSERT INTO daily_delta_stock_data (ticker_id, date_id) VALUES (900, 1)")
	result = validator.run()
	assert any(finding.tickers == ("ORPHAN",) for finding in checks(result, ValidationCheck.ORPHAN))


@pytest.mark.parametrize("damage", ["engine", "binding", "missing_metadata", "missing_column"])
def test_schema_is_reported_without_initializing_or_converting(harness, validator, damage):
	harness.run()
	if damage == "engine":
		harness.database.engine = "MyISAM"
	elif damage == "binding":
		harness.database.sql("UPDATE stockguidance_source SET source_key = 'wrong'")
	elif damage == "missing_metadata":
		harness.database.sql("DROP TABLE stockguidance_source")
	else:
		harness.database.sql("ALTER TABLE raw_stock_data DROP COLUMN vwap")
	commits = harness.database.commits
	harness.database.statements.clear()
	result = validator.run()
	assert not result.complete
	assert checks(result, ValidationCheck.SCHEMA)
	assert result.exit_code == (1 if damage == "missing_column" else 2)
	assert harness.database.commits == commits
	assert all(statement.startswith(("SELECT", "START TRANSACTION")) for statement, parameters in harness.database.statements)


@pytest.mark.parametrize("receipt", ["bad-time", "before-close", "bad-revision"])
def test_invalid_receipts_have_specific_findings(harness, validator, receipt):
	harness.run()
	if receipt == "bad-revision":
		harness.database.sql("UPDATE stockguidance_imports SET raw_id = 'invalid' WHERE session_date = '2024-07-01'")
	else:
		value = "broken" if receipt == "bad-time" else "2024-07-01T10:00:00+00:00"
		harness.database.sql("UPDATE stockguidance_imports SET available_at = ? WHERE session_date = '2024-07-01'", (value,))
	result = validator.run()
	assert not result.failed
	assert checks(result, ValidationCheck.IMPORT)
	assert any(finding.category is FindingCategory.INVALID for finding in checks(result, ValidationCheck.IMPORT))


@pytest.mark.parametrize("column", ["date_id", "ticker_id"])
def test_invalid_mapping_ids_are_findings_not_aborted_inspections(harness, validator, column):
	harness.run()
	harness.database.sql(f"UPDATE raw_stock_data SET {column} = 0 WHERE date_id = 1")
	result = validator.run()
	assert not result.failed and not result.complete
	assert any(0 in finding.record_ids for finding in checks(result, ValidationCheck.ORPHAN))
