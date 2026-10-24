from pathlib import Path
from types import SimpleNamespace

import pytest

from stockguidance.config.source import SourceConfig
from stockguidance.market_data.providers import configured_polygon, credentials


@pytest.mark.parametrize("environment_credential", [False, True])
def test_credentials_are_loaded_only_on_fetch_and_provider_is_reused(samples, monkeypatch, environment_credential, capsys):
	imports, transports, fetches = [], [], []
	credential = "fixture-credential-never-log"
	monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
	monkeypatch.delenv("POLYGON_API_KEY", raising=False)
	if environment_credential:
		monkeypatch.setenv("POLYGON_API_KEY", credential)

	def import_module(name):
		imports.append(name)
		return SimpleNamespace(DEFAULT_API_KEY=credential)

	def transport(value, retries):
		transports.append((value, retries))
		return object()

	class Provider:
		def __init__(self, transport):
			pass

		def fetch(self, source, session):
			fetches.append((source, session))
			return "fixture-response"

	monkeypatch.setattr(credentials, "import_module", import_module)
	monkeypatch.setattr(configured_polygon, "PolygonHttpTransport", transport)
	monkeypatch.setattr(configured_polygon, "PolygonGroupedProvider", Provider)
	provider = configured_polygon.ConfiguredPolygonProvider(3)
	assert not imports and not transports and not fetches
	source = SourceConfig(samples.boundary.source, Path("fixture-root"), Path("fixture-db.id"))
	for unused in range(2):
		assert provider.fetch(source, samples.decision().previous_session) == "fixture-response"
	assert imports == ([] if environment_credential else ["polygon_fetch"])
	assert transports == [(credential, 3)] and len(fetches) == 2
	assert capsys.readouterr().out == ""


def test_repository_credential_fallback_when_root_module_is_not_importable(tmp_path, monkeypatch, capsys):
	monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
	monkeypatch.delenv("POLYGON_API_KEY", raising=False)
	monkeypatch.setattr(credentials, "__file__", str(tmp_path / "src/stockguidance/market_data/providers/credentials.py"))
	(tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
	(tmp_path / "polygon_fetch.py").write_text("DEFAULT_API_KEY = 'fixture-checkout-credential'\n")
	monkeypatch.chdir(tmp_path.parent)
	def missing_module(name):
		raise ModuleNotFoundError("Module is not on the import path", name=name)
	monkeypatch.setattr(credentials, "import_module", missing_module)
	assert credentials.polygon_credential() == "fixture-checkout-credential"
	assert capsys.readouterr().out == ""


def test_missing_credential_explains_environment_configuration(tmp_path, monkeypatch):
	monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
	monkeypatch.delenv("POLYGON_API_KEY", raising=False)
	monkeypatch.setattr(credentials, "__file__", str(tmp_path / "src/stockguidance/market_data/providers/credentials.py"))
	def missing_module(name):
		raise ModuleNotFoundError("Module is not on the import path", name=name)
	monkeypatch.setattr(credentials, "import_module", missing_module)
	with pytest.raises(ValueError, match="set MASSIVE_API_KEY or POLYGON_API_KEY"):
		credentials.polygon_credential()
