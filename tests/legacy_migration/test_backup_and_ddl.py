from pathlib import Path

from stockguidance.database.table_names import load_table_names
from stockguidance.performance.stage12 import _dump_statements, _migration_ddl, _stable_raw_results


def test_backup_statement_stream_skips_target_switches_and_handles_routines(tmp_path: Path):
	path = tmp_path / "dump.sql"
	path.write_text(
		"-- header\n"
		"CREATE DATABASE `stockforecasting`;\n"
		"USE `stockforecasting`;\n"
		"CREATE TABLE `sample` (`id` INT);\n"
		"DELIMITER ;;\n"
		"CREATE PROCEDURE `sample_proc`()\n"
		"BEGIN\n"
		"  SELECT 1;\n"
		"END ;;\n"
		"DELIMITER ;\n"
		"INSERT INTO `sample` VALUES (1);\n"
		"-- Dump completed on 2026-10-22\n",
		encoding="utf-8",
	)

	assert list(_dump_statements(path)) == [
		"CREATE TABLE `sample` (`id` INT)",
		"CREATE PROCEDURE `sample_proc`()\nBEGIN\n  SELECT 1;\nEND",
		"INSERT INTO `sample` VALUES (1)",
	]


def test_stage12_ddl_uses_corrected_precision_and_keeps_registry_index_names():
	names = load_table_names()
	statements = _migration_ddl(names, {names.sessions: "InnoDB"})

	assert "DECIMAL(28,8)" in statements[0]
	assert "DECIMAL(20,8)" in statements[0]
	assert "DECIMAL(42,22)" in statements[0]
	assert "utf8mb4_bin" in statements[1]
	assert all("DROP INDEX" not in statement for statement in statements)


def test_second_run_comparison_ignores_expected_first_run_change_counts():
	first = {"raw": {"results": [{"session": "2026-04-29", "record_count": 2, "records_id": "a", "content_digest": "b", "receipt_adopted_at": "c", "changed_rows": 2, "new_tickers": 1}]}}
	second = {"raw": {"results": [{"session": "2026-04-29", "record_count": 2, "records_id": "a", "content_digest": "b", "receipt_adopted_at": "c", "changed_rows": 0, "new_tickers": 0}]}}

	assert _stable_raw_results(first) == _stable_raw_results(second)
