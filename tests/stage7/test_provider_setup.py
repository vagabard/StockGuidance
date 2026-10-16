from pathlib import Path
from types import SimpleNamespace

import pytest

from stockguidance.config.source import SourceConfig
from stockguidance.market_data.providers import configured_polygon, credentials


@pytest.mark.parametrize("environment_credential", [False, True])
def test_credentials_are_loaded_only_on_fetch_and_provider_is_reused(samples, monkeypatch, environment_credential, capsys):
	imports, transports, fetches = [], [], []
	credential = "fixture-credential-never-log"
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
