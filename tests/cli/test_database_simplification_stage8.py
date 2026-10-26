from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import stockguidance.cli.update_setup as update_setup
import stockguidance.cli.update_zscores as update_zscores
import stockguidance.cli.validation_setup as validation_setup
import stockguidance.database.configuration as configuration_module
import stockguidance.database.simplified_runtime as simplified_runtime
import stockguidance.workflows.daily_maintenance as daily_maintenance
import stockguidance.workflows.simplified_maintenance as simplified_maintenance


class TargetConfiguration:
	data_configuration_file = None

	def load_table_names(self):
		return "target-table-registry"


def _dbinfo(tmp_path: Path) -> str:
	return str(tmp_path / "dbinfo.id")


def test_update_market_data_setup_dispatches_target_schema(tmp_path: Path, monkeypatch):
	expected = object()
	config = TargetConfiguration()
	monkeypatch.setattr(update_setup, "load_database_configuration", lambda _path: config)
	monkeypatch.setattr(update_setup, "MySQLConnectionFactory", lambda: "target-connect")
	monkeypatch.setattr(simplified_runtime, "target_schema_state", lambda *_args: "target")
	monkeypatch.setattr(update_setup, "assemble_and_update_simplified", lambda *args: expected)

	result = update_setup.assemble_and_update(Namespace(dbinfo_file=_dbinfo(tmp_path), verbose=False))
	assert result is expected


def test_zscore_setup_dispatches_target_schema(tmp_path: Path, monkeypatch):
	expected = {"mode": "simplified"}
	config = TargetConfiguration()
	monkeypatch.setattr(update_zscores, "load_database_configuration", lambda _path: config)
	monkeypatch.setattr(update_zscores, "MySQLConnectionFactory", lambda: "target-connect")
	monkeypatch.setattr(update_zscores, "target_schema_state", lambda *_args: "target")
	monkeypatch.setattr(update_zscores, "assemble_simplified_zscores", lambda *args: expected)

	result = update_zscores.assemble_and_update_zscores(Namespace(dbinfo_file=_dbinfo(tmp_path)))
	assert result is expected


def test_validation_setup_dispatches_target_schema(tmp_path: Path, monkeypatch):
	expected = object()
	config = TargetConfiguration()
	monkeypatch.setattr(validation_setup, "load_database_configuration", lambda _path: config)
	monkeypatch.setattr(validation_setup, "MySQLConnectionFactory", lambda: "target-connect")
	monkeypatch.setattr(validation_setup, "target_schema_state", lambda *_args: "target")
	monkeypatch.setattr(validation_setup, "assemble_simplified_validation", lambda *args: expected)

	result = validation_setup.assemble_and_validate(Namespace(dbinfo_file=_dbinfo(tmp_path)))
	assert result is expected


def test_daily_maintenance_dispatches_target_schema_before_legacy_stages(tmp_path: Path, monkeypatch):
	expected = {"runtime": "simplified"}
	config = TargetConfiguration()
	monkeypatch.setattr(configuration_module, "load_database_configuration", lambda _path: config)
	monkeypatch.setattr(daily_maintenance, "MySQLConnectionFactory", lambda: "target-connect")
	monkeypatch.setattr(simplified_runtime, "target_schema_state", lambda *_args: "target")
	monkeypatch.setattr(simplified_maintenance, "run_simplified_daily_maintenance", lambda _arguments: expected)

	result = daily_maintenance.run_daily_maintenance(SimpleNamespace(dbinfo_file=_dbinfo(tmp_path)))
	assert result is expected
