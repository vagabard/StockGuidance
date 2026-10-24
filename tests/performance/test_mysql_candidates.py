import json

import pytest

from stockguidance.database.table_names import load_table_names
from stockguidance.market_data.identity import content_id
from stockguidance.performance.candidates import aggregate_candidate_reports
from stockguidance.performance.mysql_candidates import mysql_schema_statements, mysql_statement, validate_schema


@pytest.mark.parametrize("schema", ["stocks", "mysql", "stockguidance_disposable_x;DROP", "stockguidance_disposable_"])
def test_mysql_guard_rejects_non_disposable_names(schema):
	with pytest.raises(ValueError, match="distinct"):
		validate_schema(schema, "stocks")


def test_mysql_guard_rejects_production_name_even_with_disposable_prefix():
	with pytest.raises(ValueError, match="distinct"):
		validate_schema("stockguidance_disposable_test", "STOCKGUIDANCE_DISPOSABLE_TEST")


def test_mysql_ddl_uses_corrected_precision_and_transactional_engine(tmp_path):
	names = load_table_names()
	precision = tmp_path / "precision.json"
	columns = {
		"open": "DECIMAL(28,8)", "high": "DECIMAL(28,8)", "low": "DECIMAL(28,8)",
		"close": "DECIMAL(28,8)", "volume": "DECIMAL(20,8)", "vwap": "DECIMAL(42,22)"}
	precision.write_text(json.dumps({"schema_selection": {"schema_hash": content_id(columns), "columns": columns, "ddl": columns}}), encoding="utf-8")
	manifest = {"authoritative_registry": {"tables": names.tables(), "zscore_templates": names.templates()},
		"corrected_schema_hash": content_id(columns), "inputs": {"precision_report": str(precision)}}
	statements = mysql_schema_statements(manifest)
	assert all("ENGINE=InnoDB" in statement for statement in statements if statement.startswith("CREATE TABLE"))
	assert any("volume DECIMAL(20,8)" in statement and "vwap DECIMAL(42,22)" in statement for statement in statements)
	assert any("label VARCHAR(16) COLLATE utf8mb4_bin" in statement for statement in statements)
	assert all("zscore FLOAT" in statement for statement in statements if "(date_id BIGINT NOT NULL, ticker_id BIGINT NOT NULL, zscore" in statement)
	manifest["corrected_schema_hash"] = "stale"
	with pytest.raises(ValueError, match="precision"):
		mysql_schema_statements(manifest)


def test_mysql_upsert_preserves_rows_instead_of_replace_deletion():
	statement = mysql_statement("INSERT OR REPLACE INTO raw(ticker_id, date_id, close) VALUES (?,?,?)")
	assert "REPLACE" not in statement
	assert "ON DUPLICATE KEY UPDATE" in statement
	assert "%s" in statement


def test_sqlite_reports_cannot_complete_mysql_comparison(tmp_path):
	report = tmp_path / "sqlite.json"
	report.write_text(json.dumps({"schema_version": 1, "backend": "python", "manifest": {"id": "same"},
		"workloads": {workload: [{"end_to_end_seconds": 1}] for workload in ("raw", "derived", "zscores")}}), encoding="utf-8")
	aggregate = aggregate_candidate_reports((report,), tmp_path / "study.json")
	assert aggregate["comparison_eligible"] is False
	assert aggregate["status"] == "incomplete"
	assert all(row["status"] == "offline_sqlite_only" for row in aggregate["comparison_table"] if row["backend"] == "python")
