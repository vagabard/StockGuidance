import json
from types import SimpleNamespace

import pytest

from stockguidance.database import connections
from stockguidance.database.configuration import load_database_configuration


@pytest.fixture
def configuration(tmp_path):
	path = tmp_path / "explicit-database.json"
	path.write_text(json.dumps({"host": "fixture-host", "database": "fixture-data", "user": "fixture-user", "password": "fixture-password-never-log", "port": 3307, "driver": "auto"}), encoding="utf-8")
	return path


def test_connector_uses_only_explicit_configuration_and_lazy_available_driver(configuration, monkeypatch):
	imports, calls = [], []
	connection = object()

	def connect(**values):
		calls.append(values)
		return connection

	def load(name):
		imports.append(name)
		if name == "mysql.connector":
			raise ImportError("Fixture unavailable driver")
		return SimpleNamespace(connect=connect)

	monkeypatch.setattr(connections, "import_module", load)
	assert not imports
	assert connections.MySQLConnectionFactory()(configuration) is connection
	assert imports == ["mysql.connector", "pymysql"]
	assert calls[0]["database"] == "fixture-data" and calls[0]["port"] == 3307
	assert calls[0]["autocommit"] is False
	assert "password" not in repr(load_database_configuration(configuration))


@pytest.mark.parametrize("change", [{"port": True}, {"port": 0}, {"port": 70000}, {"port": "3306"}, {"driver": "unknown"}, {"schema": "different"}, {"api_key": "fixture-private"}, {"user": None}])
def test_invalid_configuration_is_rejected_without_loading_drivers_or_exposing_values(configuration, monkeypatch, change):
	fields = json.loads(configuration.read_text(encoding="utf-8")) | change
	configuration.write_text(json.dumps(fields), encoding="utf-8")
	monkeypatch.setattr(connections, "import_module", lambda name: pytest.fail("Invalid configuration loaded a driver"))
	with pytest.raises(ValueError) as error:
		connections.MySQLConnectionFactory()(configuration)
	assert "fixture-password" not in str(error.value) and "fixture-private" not in str(error.value)


def test_connection_errors_are_sanitized_and_do_not_fallback_to_another_database(configuration, monkeypatch):
	imports = []

	def connect(**values):
		raise RuntimeError(values["password"])

	def load(name):
		imports.append(name)
		return SimpleNamespace(connect=connect)

	monkeypatch.setattr(connections, "import_module", load)
	with pytest.raises(RuntimeError, match="Configured MySQL connection failed") as error:
		connections.MySQLConnectionFactory()(configuration)
	assert error.value.__cause__ is None and "fixture-password" not in str(error.value)
	assert imports == ["mysql.connector"]
