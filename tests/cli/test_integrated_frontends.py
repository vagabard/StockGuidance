from dataclasses import asdict
from datetime import date
import json
from pathlib import Path

import pytest

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.artifacts.loading import load_artifact
from stockguidance.cli import batch, incremental
from stockguidance.cli import update_setup, validation_setup
from stockguidance.cli.update_market_data import main as update_main
from stockguidance.cli.validate_database import main as validate_main
from stockguidance.market_data.history_store import JsonHistoryStore


POLICIES = ["--publication-delay-minutes", "15", "--publication-evidence", "Fixture-only", "--recent-window-days", "30", "--recent-refresh-hours", "720", "--archive-refresh-days", "30", "--correction-evidence", "Fixture-only"]


def test_default_validation_and_explicit_updater_repair_round_trip_detect_a_leading_gap(harness, monkeypatch, tmp_path, capsys):
	assert harness.run().successful
	JsonHistoryStore(harness.source.data_root / "history").save(harness.boundary)
	harness.database.sql("DELETE FROM raw_stock_data WHERE date_id = (SELECT id FROM transaction_dates WHERE transaction_date = ?)", ("2024-07-01",))
	harness.database.sql("DELETE FROM daily_delta_stock_data WHERE date_id = (SELECT id FROM transaction_dates WHERE transaction_date = ?)", ("2024-07-01",))
	for setup in (update_setup, validation_setup):
		monkeypatch.setattr(setup, "SystemClock", lambda: harness.clock)
		monkeypatch.setattr(setup, "MySQLConnectionFactory", lambda: harness.database)
	monkeypatch.setattr(update_setup, "ConfiguredPolygonProvider", lambda retries: harness.provider)
	common = ["--data-root", str(harness.source.data_root), "--dbinfo-file", str(harness.source.database_config), "--start-date", "2024-07-01", "--end-date", "2024-07-08"]
	validation = [*common, "--publication-delay-minutes", "15", "--publication-evidence", "Fixture-only", "--format", "json"]
	commits = harness.database.commits
	assert validate_main(validation) == 2
	repair = json.loads(capsys.readouterr().out)
	assert not repair["complete"] and repair["repair_plan"]["items"]
	assert harness.database.commits == commits
	path = tmp_path / "repair.json"
	path.write_text(json.dumps(repair), encoding="utf-8")
	assert update_main([*common, *POLICIES, "--repair-plan", str(path)]) == 0
	assert json.loads(capsys.readouterr().out)["successful"]
	assert validate_main(validation) == 0
	assert json.loads(capsys.readouterr().out)["complete"]


@pytest.mark.parametrize("allocation", ["alternate_sessions", "staggered_halves"])
@pytest.mark.parametrize("model_policy", ["fixed", "chronological_retrain"])
def test_batch_simulation_reports_preserve_chronology_for_both_schedules(simulation, simulation_json, tmp_path, capsys, allocation, model_policy):
	simulation_json["allocation"]["policy"] = allocation
	if model_policy == "chronological_retrain":
		configuration = asdict(simulation.configuration)
		simulation_json["retraining"] = {"start_date": "2024-07-02", "end_date": "2024-07-11", "decision_minutes_before_close": 30, "every_buy_decisions": 2, "model": configuration.pop("model"), "training": configuration}
	selection = {"path": str(simulation.reference.path), "artifact_id": simulation.reference.artifact_id} if model_policy == "fixed" else str(tmp_path / "walk-forward")
	config = tmp_path / "simulation.json"
	config.write_text(json.dumps(simulation_json), encoding="utf-8")
	report = tmp_path / "report.json"
	plan = {"schema": "stockguidance-batch-1", "steps": [{"name": "evaluate", "kind": "simulate", "config": str(config), "report": str(report), "options": {"model_policy": model_policy, "artifact": selection, "artifact_kind": "model"}}]}
	path = tmp_path / "batch.json"
	path.write_text(json.dumps(plan), encoding="utf-8")
	assert batch.main(["--config", str(path)]) == 0
	assert json.loads(capsys.readouterr().out)["successful"]
	payload = json.loads(report.read_text(encoding="utf-8"))
	assert payload["allocation"]["policy"] == allocation
	assert payload["model_policy"] == model_policy
	assert payload["target"]["formula"] == "close(T+1) / close(T) - 1"
	for training in payload["training_runs"]:
		artifact = training["result"]["artifact"]
		loaded = load_artifact(ArtifactReference(Path(artifact["path"]), artifact["artifact_id"]))
		assert loaded.metadata.training.last_exit.isoformat() < training["entry_session"]
		assert loaded.metadata.training.training_cutoff.isoformat() == training["decision_cutoff"]


def test_incremental_command_rejects_fixed_models_before_any_historical_work(simulation, tmp_path):
	with pytest.raises(SystemExit) as error:
		incremental.main(["--config", str(tmp_path / "does-not-exist.json"), "--model-policy", "fixed", "--artifact", str(simulation.reference.path), "--artifact-id", simulation.reference.artifact_id])
	assert error.value.code == 2


@pytest.mark.parametrize("allocation", ["alternate_sessions", "staggered_halves"])
def test_incremental_frontend_runs_one_chronological_ledger_for_both_allocations(simulation, simulation_json, tmp_path, capsys, allocation):
	simulation_json["allocation"]["policy"] = allocation
	configuration = asdict(simulation.configuration)
	simulation_json["retraining"] = {"start_date": "2024-07-02", "end_date": "2024-07-11", "decision_minutes_before_close": 30, "every_buy_decisions": 2, "model": configuration.pop("model"), "training": configuration}
	path = tmp_path / "incremental.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")
	assert incremental.main(["--config", str(path), "--model-policy", "chronological_retrain", "--artifacts-directory", str(tmp_path / "models")]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] and payload["model_policy"] == "chronological_retrain"
	assert payload["allocation"]["policy"] == allocation
	assert payload["training_runs"] and len(payload["equity"]) == 5
