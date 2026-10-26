import json
from pathlib import Path

import pytest

from stockguidance.cli import preview_schema
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.table_names import TableNames, load_table_names, quote_identifier
from stockguidance.database.update_schema import update_schema_statements


CANONICAL = {
	"schema_version": 1,
	"tables": {
		"source": "stockguidance_source",
		"sessions": "transaction_dates",
		"tickers": "ticker_symbol",
		"prices": "raw_stock_data",
		"deltas": "daily_delta_stock_data",
		"imports": "stockguidance_imports",
		"zscore_series": "stockguidance_zscore_series",
		"zscore_sessions": "stockguidance_zscore_sessions",
	},
	"zscore_templates": {"price": "zscores_price_{window}", "price_delta": "zscores_price_delta_{window}"},
}


def write_payload(path: Path, payload: object) -> Path:
	path.write_text(json.dumps(payload), encoding="utf-8")
	return path


def test_packaged_registry_is_complete_and_resolves_windows(tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	registry = load_table_names()
	assert registry.tables() == CANONICAL["tables"]
	assert registry.templates() == CANONICAL["zscore_templates"]
	assert registry.resolve_zscore_tables((2, 20))[20].price_delta == "zscores_price_delta_20"


@pytest.mark.parametrize(
	"change",
	[
		lambda payload: payload.pop("tables"),
		lambda payload: payload["tables"].pop("source"),
		lambda payload: payload["tables"].update({"unexpected": "other"}),
		lambda payload: payload["zscore_templates"].pop("price"),
		lambda payload: payload["zscore_templates"].update({"unexpected": "other"}),
	],
)
def test_missing_and_unknown_fields_are_rejected(tmp_path, change):
	payload = json.loads(json.dumps(CANONICAL))
	change(payload)
	with pytest.raises(ValueError):
		load_table_names(write_payload(tmp_path / "names.json", payload))


def test_duplicate_json_fields_are_rejected(tmp_path):
	path = tmp_path / "duplicate.json"
	path.write_text('{"schema_version": 1, "schema_version": 1, "tables": {}, "zscore_templates": {}}', encoding="utf-8")
	with pytest.raises(ValueError):
		load_table_names(path)


@pytest.mark.parametrize(
	"payload",
	[
		{"schema_version": 2, "tables": CANONICAL["tables"], "zscore_templates": CANONICAL["zscore_templates"]},
		{"schema_version": True, "tables": CANONICAL["tables"], "zscore_templates": CANONICAL["zscore_templates"]},
		{"schema_version": 1, "tables": {**CANONICAL["tables"], "prices": "bad-name"}, "zscore_templates": CANONICAL["zscore_templates"]},
		{"schema_version": 1, "tables": {**CANONICAL["tables"], "prices": ""}, "zscore_templates": CANONICAL["zscore_templates"]},
		{"schema_version": 1, "tables": {**CANONICAL["tables"], "prices": "transaction_dates"}, "zscore_templates": CANONICAL["zscore_templates"]},
		{"schema_version": 1, "tables": CANONICAL["tables"], "zscore_templates": {"price": "zscores_{window}{window}", "price_delta": "zscores_delta_{window}"}},
		{"schema_version": 1, "tables": CANONICAL["tables"], "zscore_templates": {"price": "zscores_{window:02d}", "price_delta": "zscores_delta_{window}"}},
	],
)
def test_invalid_versions_types_names_templates_and_collisions_are_rejected(tmp_path, payload):
	with pytest.raises(ValueError):
		load_table_names(write_payload(tmp_path / "names.json", payload))


def test_invalid_windows_and_generated_collisions_are_rejected():
	registry = load_table_names()
	with pytest.raises(ValueError):
		registry.resolve_zscore_tables((True,))
	with pytest.raises(ValueError):
		registry.resolve_zscore_tables((2, 2))
	colliding = TableNames(
		"zscores_price_2", registry.sessions, registry.tickers, registry.prices, registry.deltas,
		registry.imports, registry.zscore_series, registry.zscore_sessions,
		registry.price_template, registry.price_delta_template,
	)
	with pytest.raises(ValueError):
		colliding.resolve_zscore_tables((2,))


def test_explicit_table_names_path_is_configuration_relative_when_cwd_differs(tmp_path, monkeypatch):
	config_directory = tmp_path / "config"
	config_directory.mkdir()
	config_path = config_directory / "database.json"
	names_path = config_directory / "custom.json"
	write_payload(names_path, CANONICAL)
	config_path.write_text(json.dumps({"host": "h", "database": "d", "user": "u", "table_names_file": "custom.json"}), encoding="utf-8")
	monkeypatch.chdir(tmp_path)
	configuration = load_database_configuration(config_path)
	assert configuration.table_names_file == names_path.resolve()
	assert configuration.load_table_names().source == "stockguidance_source"


def test_explicit_missing_file_never_falls_back_to_packaged_resource(tmp_path):
	with pytest.raises(ValueError, match="selected table-names file"):
		load_table_names(tmp_path / "missing.json")


def test_unreadable_and_malformed_files_fail(tmp_path):
	with pytest.raises(ValueError):
		load_table_names(tmp_path)
	with pytest.raises(ValueError):
		load_table_names(write_payload(tmp_path / "malformed.json", ["not", "an", "object"]))


def test_identifier_quoting_and_custom_ddl_use_validated_names(tmp_path):
	custom = json.loads(json.dumps(CANONICAL))
	custom["tables"] = {key: f"custom_{value}" for key, value in custom["tables"].items()}
	registry = load_table_names(write_payload(tmp_path / "custom.json", custom))
	assert quote_identifier(registry.source) == "`custom_stockguidance_source`"
	statements = update_schema_statements(registry)
	assert statements[0].startswith("CREATE TABLE IF NOT EXISTS `custom_stockguidance_source`")
	assert all("stockguidance_source" not in statement or "custom_stockguidance_source" in statement for statement in statements)


def test_preview_prints_names_and_ddl_without_database_access(capsys, monkeypatch):
	monkeypatch.setattr(preview_schema, "simplified_schema_statements", lambda registry, windows: ("fixture ddl",))
	assert preview_schema.main([]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["tables"]["source"] == "stockguidance_source"
	assert payload["ddl"] == ["fixture ddl"]
