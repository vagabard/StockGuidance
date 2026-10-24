from datetime import date, timedelta
import json

from stockguidance.cli.validate_database import main
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.history import DiscoveryMetadata
from stockguidance.market_data.providers.contracts import CapabilityState
from stockguidance.validation.findings import FindingCategory, ValidationCheck
from support.ingestion import harness


def _arguments(harness):
	return [
		"--data-root", str(harness.source.data_root),
		"--dbinfo-file", str(harness.source.database_config),
		"--end-date", "2024-07-08",
		"--publication-delay-minutes", "15",
		"--publication-evidence", "Fixture-only policy",
		"--format", "json",
	]


def test_omitted_history_discovers_local_scope_without_provider(harness, monkeypatch, capsys):
	from stockguidance.cli import validation_setup

	harness.run()
	files_before = {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	harness.provider.calls.clear()
	monkeypatch.setattr(validation_setup, "SystemClock", lambda: harness.clock)
	monkeypatch.setattr(validation_setup, "MySQLConnectionFactory", lambda: harness.database)

	assert main(_arguments(harness)) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["local_observed_bounds"]["first"] == "2024-07-01"
	assert payload["effective_scope"] == {"start": "2024-07-01", "end": "2024-07-08"}
	assert payload["history_start"] is None
	assert payload["history_provenance"] == "unresolved"
	assert payload["provider_capability"]["state"] == CapabilityState.UNKNOWN
	assert not harness.provider.calls
	assert files_before == {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}


def test_automatic_scope_reports_interior_holes_and_keeps_repair_offline(harness, monkeypatch, capsys):
	from stockguidance.cli import validation_setup

	harness.run()
	harness.provider.calls.clear()
	harness.database.sql("DELETE FROM transaction_dates WHERE transaction_date = ?", ("2024-07-03",))
	commits_before = harness.database.commits
	monkeypatch.setattr(validation_setup, "SystemClock", lambda: harness.clock)
	monkeypatch.setattr(validation_setup, "MySQLConnectionFactory", lambda: harness.database)

	assert main(_arguments(harness)) == 2
	payload = json.loads(capsys.readouterr().out)
	assert any(item["check"] == ValidationCheck.SESSION.value and item["category"] == FindingCategory.MISSING.value and item["session_date"] == "2024-07-03" for item in payload["findings"])
	assert payload["repair_plan"]["schema_version"] == 2
	assert any(item["action"] == "blocked_out_of_access" for item in payload["repair_plan"]["items"])
	assert not harness.provider.calls
	assert harness.database.commits == commits_before


def test_cached_capability_blocks_old_gap_refresh_without_contacting_provider(harness, monkeypatch, capsys):
	from stockguidance.cli import validation_setup

	harness.run()
	harness.provider.calls.clear()
	store = JsonHistoryStore(harness.source.data_root / "history")
	store.save_discovery_metadata(DiscoveryMetadata(
		harness.source.identity, "XNYS", harness.clock.now(),
		tuple(date(2024, 7, day) for day in (1, 2, 3, 5, 8)),
		tuple(date(2024, 7, day) for day in (1, 2, 3, 5, 8)),
		(date(2024, 7, 4),), CapabilityState.VERIFIED.value, harness.clock.now(),
		"Fixture account capability", date(2024, 7, 5), date(2024, 7, 10), timedelta(days=1), date(2024, 7, 5),
	))
	harness.database.sql("DELETE FROM transaction_dates WHERE transaction_date = ?", ("2024-07-01",))
	monkeypatch.setattr(validation_setup, "SystemClock", lambda: harness.clock)
	monkeypatch.setattr(validation_setup, "MySQLConnectionFactory", lambda: harness.database)

	assert main(_arguments(harness)) == 2
	payload = json.loads(capsys.readouterr().out)
	assert payload["provider_capability"]["fresh"]
	assert any(item["action"] == "blocked_out_of_access" and item["session_date"] == "2024-07-01" for item in payload["repair_plan"]["items"])
	assert not harness.provider.calls
