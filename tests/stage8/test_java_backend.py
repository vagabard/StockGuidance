from pathlib import Path
import subprocess

import pytest

from stockguidance.validation.java_backend import JavaValidationBackendError, compare_validation_reports, normalize_validation_report, run_java_validator


def java_report(exit_code=0):
	return subprocess.CompletedProcess([], exit_code, '{"schema_version": 1, "backend": "java", "complete": true, "failed": false, "exit_code": 0, "sessions": [], "findings": []}', "")


def test_java_boundary_passes_only_explicit_paths_and_accepts_incomplete_reports(tmp_path):
	seen = {}

	def runner(command, **keywords):
		seen["command"] = command
		seen["keywords"] = keywords
		name_index = command.index("--table-names-file") + 1
		seen["table_names_exists_during_run"] = Path(command[name_index]).is_file()
		return java_report(2)

	payload = run_java_validator(tmp_path / "db.json", tmp_path / "request.json", classpath=Path("target/classes"), run=runner)

	assert payload["backend"] == "java"
	assert "secret" not in " ".join(seen["command"])
	assert seen["command"][-4:] == ("--config", str(tmp_path / "db.json"), "--request", str(tmp_path / "request.json"))
	assert seen["table_names_exists_during_run"] is True
	assert not Path(seen["command"][seen["command"].index("--table-names-file") + 1]).exists()
	assert seen["keywords"]["check"] is False


def test_java_boundary_passes_configuration_relative_external_registry(tmp_path):
	registry = tmp_path / "table_names.json"
	registry.write_text(
		'{"schema_version":1,"tables":{"source":"custom_source","sessions":"custom_sessions",'
		'"tickers":"custom_tickers","prices":"custom_prices","deltas":"custom_deltas",'
		'"imports":"custom_imports","zscore_series":"custom_series",'
		'"zscore_sessions":"custom_session_meta"},"zscore_templates":'
		'{"price":"custom_price_{window}","price_delta":"custom_delta_{window}"}}',
		encoding="utf-8",
	)
	database_config = tmp_path / "db.json"
	database_config.write_text('{"database":"fixture","user":"fixture-user","table_names_file":"table_names.json"}', encoding="utf-8")
	seen = {}

	def runner(command, **keywords):
		seen["command"] = command
		return java_report(0)

	run_java_validator(database_config, tmp_path / "request.json", classpath=Path("target/classes"), run=runner)

	name_index = seen["command"].index("--table-names-file") + 1
	assert Path(seen["command"][name_index]) == registry


def test_java_boundary_rejects_missing_selected_registry_before_subprocess(tmp_path):
	database_config = tmp_path / "db.json"
	database_config.write_text('{"database":"fixture","user":"fixture-user","table_names_file":"missing.json"}', encoding="utf-8")

	with pytest.raises(JavaValidationBackendError, match="table-names"):
		run_java_validator(database_config, tmp_path / "request.json", classpath=Path("target/classes"), run=lambda *args, **kwargs: pytest.fail("Java must not start"))


def test_java_backend_failure_is_not_mistaken_for_a_validation_result(tmp_path):
	def runner(command, **keywords):
		return subprocess.CompletedProcess(command, 1, '{"schema_version": 1, "backend": "java", "complete": false, "failed": true, "exit_code": 1, "error": "Java validation backend failed"}', "")

	with pytest.raises(JavaValidationBackendError, match="backend failed"):
		run_java_validator(tmp_path / "db.json", tmp_path / "request.json", classpath=Path("target/classes"), run=runner)


def test_normalized_contract_sorts_storage_order_without_changing_scope():
	payload = {
		"complete": True,
		"failed": False,
		"exit_code": 0,
		"source": {"universe": "fixture"},
		"calendar": {"version": "fixture-1"},
		"scope": {"start": "2024-07-01", "end": "2024-07-01", "as_of": "2024-07-02T00:00:00Z"},
		"history": {"history_start": "2024-07-01"},
		"sessions": [{"date": "2024-07-01", "state": "complete", "source_keys": ["BBB", "AAA"], "raw_valid_keys": ["BBB", "AAA"], "derived_valid_keys": [], "checks": [], "source_count": 2, "raw_count": 2, "derived_count": 0}],
		"findings": [],
		"repair_plan": None,
	}

	normalized = normalize_validation_report(payload)
	assert normalized["sessions"][0]["source_keys"] == ["AAA", "BBB"]
	assert normalized["scope"]["start"] == "2024-07-01"


def test_normalized_contract_rejects_invalid_session_collections():
	with pytest.raises(ValueError, match="sessions must be a list"):
		normalize_validation_report({"sessions": None})
	with pytest.raises(ValueError, match="sessions must be a list"):
		normalize_validation_report({"session_results": None})


def test_python_and_java_selected_operation_reports_have_equal_normalized_contracts():
	common = {
		"complete": True,
		"failed": False,
		"exit_code": 0,
		"source": {"universe": "fixture"},
		"calendar": {"id": "XNYS", "version": "fixture-1"},
		"scope": {"start": "2024-07-01", "end": "2024-07-01", "as_of": "2024-07-02T00:00:00Z"},
		"history": {"history_start": "2024-07-01", "history_provenance": "fixture"},
		"sessions": [{"date": "2024-07-01", "state": "complete", "checks": ["session"], "source_keys": ["AAA"], "raw_valid_keys": ["AAA"], "derived_valid_keys": ["AAA"], "source_count": 1, "raw_count": 1, "derived_count": 1, "source_state": "complete", "available_at": "2024-07-01T21:00:00Z"}],
		"findings": [],
		"repair_plan": None,
	}
	python_report = dict(common, start="2024-07-01", end="2024-07-01", as_of="2024-07-02T00:00:00Z", calendar_version="fixture-1")
	java_report_payload = dict(common, backend="java")

	compare_validation_reports(python_report, java_report_payload)
