from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from stockguidance.cli.validate_database import main
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.validation.findings import FindingCategory, ValidationCheck
from stockguidance.validation.repair import RepairAction, RepairItem, load_repair_plan
from stockguidance.validation.reporting import report_payload


def test_default_cli_has_no_writes_fetches_repairs_or_metadata_changes(harness, monkeypatch, capsys):
	from stockguidance.cli import validation_setup
	from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
	from stockguidance.market_data.history_store import JsonHistoryStore
	from stockguidance.market_data.providers.polygon import PolygonGroupedProvider

	harness.run()
	files_before = {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	commits_before = harness.database.commits
	harness.database.statements.clear()
	harness.provider.calls.clear()
	monkeypatch.setattr(validation_setup, "SystemClock", lambda: harness.clock)
	monkeypatch.setattr(validation_setup, "LegacyWriteConnectionFactory", lambda: harness.database)

	def forbidden(*arguments, **keywords):
		raise AssertionError("Default validation attempted a write or fetch")

	for owner, name in ((MySQLMarketDataRepository, "import_session"), (MySQLMarketDataRepository, "replace_derived"), (ManifestStore, "save"), (ManifestStore, "save_download"), (ManifestStore, "mark_complete"), (ManifestStore, "mark_imported"), (JsonHistoryStore, "save"), (PolygonGroupedProvider, "fetch"), (Path, "write_bytes"), (Path, "write_text")):
		monkeypatch.setattr(owner, name, forbidden)
	arguments = ["--data-root", str(harness.source.data_root), "--dbinfo-file", str(harness.source.database_config), "--history-start", "2024-07-01", "--history-evidence", "Fixture only", "--end-date", "2024-07-08", "--publication-delay-minutes", "15", "--publication-evidence", "Fixture only"]
	assert main(arguments) == 0
	assert "Database validation: complete" in capsys.readouterr().out
	assert all(statement.startswith(("SELECT", "START TRANSACTION")) for statement, parameters in harness.database.statements)
	assert harness.database.commits == commits_before
	assert not harness.provider.calls
	assert all(config == harness.source.database_config for config in harness.database.configs)
	assert files_before == {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	assert not (harness.source.data_root / "history").exists()


def test_json_output_is_scoped_and_machine_readable(harness, validator, capsys):
	harness.run()
	assert main(["--format", "json", "--data-root", "custom", "--dbinfo-file", "custom.id"], run=lambda arguments: validator.run()) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["source"]["interval"] == "1d"
	assert len(payload["sessions"]) == 5
	assert payload["sessions"][0]["source_count"] == payload["sessions"][0]["raw_count"] == 1
	assert payload["repair_plan"]["schema_version"] == 1
	assert "fixture-credential-reference" not in str(payload)


def test_incomplete_and_failed_exit_codes_and_sanitized_errors(validator, capsys):
	assert main(["--format", "json"], run=lambda arguments: validator.run()) == 2
	assert not json.loads(capsys.readouterr().out)["complete"]

	def fail(arguments):
		raise RuntimeError("fixture secret credential")

	assert main(["--format", "json"], run=fail) == 1
	assert "fixture secret" not in capsys.readouterr().out


def test_unknown_history_creates_no_files(tmp_path, capsys):
	assert main(["--data-root", str(tmp_path), "--format", "json"]) == 1
	assert "Supported history is unknown" in capsys.readouterr().out
	assert list(tmp_path.iterdir()) == []


def test_parser_has_no_repair_or_initialization_mode():
	with pytest.raises(SystemExit) as error:
		main(["--initialize-schema"])
	assert error.value.code == 2


def test_repair_json_round_trip_and_explicit_updater_recovery(harness, validator, tmp_path):
	assert harness.run().successful
	harness.database.sql("DELETE FROM raw_stock_data WHERE date_id = 1")
	harness.database.sql("DELETE FROM daily_delta_stock_data WHERE date_id = 1")
	harness.database.sql("DELETE FROM daily_delta_stock_data WHERE date_id = 2")
	result = validator.run()
	assert not result.complete
	path = tmp_path / "validation.json"
	path.write_text(json.dumps(report_payload(result)), encoding="utf-8")
	plan = load_repair_plan(path)
	assert plan == result.repair_plan
	harness.provider.calls.clear()
	assert harness.run(replace(harness.settings, repair_plan=plan)).successful
	assert validator.run().complete
	assert not harness.provider.calls


def test_unverified_source_repair_explicitly_forces_refresh(harness, validator, tmp_path):
	updated = harness.run()
	store = ManifestStore(harness.source.data_root)
	store.path_for(harness.source.identity, updated.manifests[1].download.session).unlink()
	plan = validator.run().repair_plan
	assert any(item.action is RepairAction.REFRESH_SOURCE for item in plan.items)
	harness.provider.calls.clear()
	assert harness.run(replace(harness.settings, repair_plan=plan)).successful
	assert len(harness.provider.calls) == 1
	assert validator.run().complete


def test_manual_or_wrong_scope_plans_fail_before_provider_or_database_access(harness, validator):
	plan = validator.run().repair_plan
	manual = replace(plan, items=(RepairItem(RepairAction.REVIEW_SCHEMA, None, ValidationCheck.SCHEMA),))
	for invalid in (manual, replace(plan, start=plan.start - timedelta(days=1)), replace(plan, source=replace(plan.source, universe="another-universe")), replace(plan, as_of=harness.clock.now() + timedelta(days=1))):
		harness.database.statements.clear()
		harness.provider.calls.clear()
		with pytest.raises(ValueError):
			harness.run(replace(harness.settings, repair_plan=invalid))
		assert not harness.database.statements and not harness.provider.calls


def test_updater_and_validator_recompute_values_despite_forged_receipt(harness, validator):
	from stockguidance.database.repositories.sessions import SessionQuery
	updated = harness.run()
	query = SessionQuery(harness.boundary, tuple(item.session for item in updated.coverage.sessions), harness.clock.now())
	harness.database.sql("UPDATE daily_delta_stock_data SET next_day_change_in_price = 99 WHERE date_id = 1")
	snapshot = harness.repository.inspect(query)[0]
	harness.database.sql("UPDATE stockguidance_imports SET derived_content_id = ? WHERE session_date = '2024-07-01'", (snapshot.derived_content_id,))
	result = validator.run()
	assert any(finding.check is ValidationCheck.OUTCOME and finding.category is FindingCategory.INVALID for finding in result.findings)
	assert not result.complete
	assert harness.run().successful
	assert validator.run().complete


def test_unfinished_markers_are_repaired_only_by_explicit_update(harness, validator):
	updated = harness.run()
	store = ManifestStore(harness.source.data_root)
	store.save(replace(updated.manifests[0], derived_complete=False))
	result = validator.run()
	assert any(item.action is RepairAction.COMPLETE_MANIFEST for item in result.repair_plan.items)
	assert harness.run(replace(harness.settings, repair_plan=result.repair_plan)).successful
	assert validator.run().complete


def test_updater_and_validator_both_reject_unassigned_orphan_rows(harness, validator):
	harness.run()
	harness.database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, open, high, low, close, volume) VALUES (1, 999, 10, 11, 9, 10, 100)")
	assert not validator.run().complete
	assert not harness.run().successful


def test_deferred_sessions_report_only_checks_actually_assessed(harness, validator):
	from datetime import datetime, timezone
	harness.clock.value = datetime(2024, 7, 3, 16, tzinfo=timezone.utc)
	harness.run()
	payload = report_payload(validator.run())
	assert payload["sessions"][-1]["checks"] == ["session"]
	assert payload["sessions"][-1]["raw_valid_keys"] == []
	assert payload["sessions"][-1]["raw_count"] is None


def test_explicit_source_refresh_overrides_unexpired_correction_policy(harness, validator):
	harness.run()
	plan = validator.run().repair_plan
	plan = replace(plan, items=(RepairItem(RepairAction.REFRESH_SOURCE, date(2024, 7, 2), ValidationCheck.SOURCE),))
	harness.provider.responses[date(2024, 7, 2)] = harness.response(close="12")
	harness.provider.calls.clear()
	assert harness.run(replace(harness.settings, repair_plan=plan)).successful
	assert [session.session_date for source, session in harness.provider.calls] == [date(2024, 7, 2)]
	assert validator.run().complete
