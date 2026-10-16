from dataclasses import asdict
import importlib
import json

import pytest

from stockguidance.artifacts.decoding import decode_metadata, metadata_payload
from stockguidance.artifacts.loading import load_artifact
from stockguidance.cli import compare_signals, recommend, simulate, train_model, update_openings, validate_database
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.workflows import recommendation_runtime, signal_comparison_runtime
from stockguidance.training import jobs
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history
from test_workflows import opening_model, recommendation_openings


@pytest.mark.parametrize("name", ["update_openings", "compare_signals"])
def test_stage10_frontend_help_is_import_safe(name, tmp_path, monkeypatch, capsys):
	monkeypatch.chdir(tmp_path)
	with pytest.raises(SystemExit) as error:
		importlib.import_module(f"stockguidance.cli.{name}").main(["--help"])
	assert error.value.code == 0 and capsys.readouterr().out
	assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("allocation", ["alternate_sessions", "staggered_halves"])
def test_documented_simulation_frontend_consumes_persisted_openings(simulation, simulation_json, opening_signals, tmp_path, capsys, allocation):
	features, reference = opening_model(simulation, opening_signals, tmp_path)
	simulation_json["features"] = asdict(features)
	simulation_json["allocation"]["policy"] = allocation
	simulation_json["data_root"] = str(opening_signals.store.root)
	JsonHistoryStore(opening_signals.store.root / "history").save(simulation.samples.boundary)
	path = tmp_path / "opening-simulation.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")
	assert simulate.main(["--config", str(path), "--model-policy", "fixed", "--artifact", str(reference.path), "--artifact-id", reference.artifact_id]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["features"]["signal_mode"] == "entry_open"
	assert payload["features"]["version"] == "entry-open-v1"
	assert payload["allocation"]["policy"] == allocation
	assert payload["target"]["formula"] == "close(T+1) / close(T) - 1"


def test_training_frontend_rebuilds_distinct_opening_artifact_from_receipts(job, opening_signals, tmp_path, monkeypatch, capsys):
	job["features"] = asdict(opening_signals.features)
	job["data_root"] = str(opening_signals.store.root)
	job["evaluation"] = None
	JsonHistoryStore(opening_signals.store.root / "history").save(opening_signals.samples.boundary)
	monkeypatch.setattr(jobs, "VerifiedCloseRepository", lambda *values: opening_signals.samples.repository)
	path = tmp_path / "opening-training.json"
	path.write_text(json.dumps(job), encoding="utf-8")
	artifact = tmp_path / "opening.sgmodel"
	assert train_model.main(["--config", str(path), "--artifact", str(artifact)]) == 0
	assert json.loads(capsys.readouterr().out)["successful"]
	metadata = load_artifact(artifact).metadata
	payload = json.loads(json.dumps(metadata_payload(metadata)))
	assert payload["preprocessing"] == "stateless_previous_closes_and_entry_open"
	assert payload["opening_contract"]["availability"].startswith("actual_receipt")
	assert decode_metadata(payload) == metadata
	payload["opening_contract"]["method"] = "same-session-final-daily-bar"
	with pytest.raises(ValueError):
		decode_metadata(payload)


@pytest.mark.parametrize("policy", ["current", "retrain"])
def test_recommendation_frontend_assembles_entry_open_capture_services(recommender, job_json, tmp_path, monkeypatch, capsys, policy):
	features, store, services, calls = recommendation_openings(recommender)
	job_json["features"] = asdict(features)
	monkeypatch.setattr(recommendation_runtime, "SystemClock", lambda: recommender.storage.clock)
	monkeypatch.setattr(recommendation_runtime, "MySQLConnectionFactory", lambda: recommender.storage.database)
	monkeypatch.setattr(recommendation_runtime, "ConfiguredPolygonProvider", lambda retries: recommender.storage.provider)
	monkeypatch.setattr(recommendation_runtime, "ConfiguredOpeningProvider", lambda retries: services.openings.provider)
	selection = tmp_path / "requested-open.sgmodel"
	arguments = []
	if policy == "current":
		result = train_from_history(HistoryTrainingRequest(recommender.plan.decisions, recommender.storage.clock.now(), features, recommender.job.target, recommender.plan.configuration, selection), recommender.storage.boundary, recommender.calendar, recommender.job.publication, services.closes)
		arguments = ["--artifact-id", result.artifact.artifact_id]
	else:
		configuration = asdict(recommender.plan.configuration)
		job_json["retraining"] = {"start_date": "2024-07-03", "end_date": "2024-07-08", "decision_minutes_before_close": 30, "model": configuration.pop("model"), "training": configuration}
	path = tmp_path / "opening-recommend.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")
	assert recommend.main(["--config", str(path), "--policy", policy, "--artifact", str(selection), *arguments]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["features"]["signal_mode"] == "entry_open" and payload["ranking"]
	assert len(calls) == len(features.ticker_ids)
	assert payload["fills_recorded"] is False


def test_read_only_frontend_checks_missing_full_universe_opening_receipts(recommender, tmp_path, monkeypatch, capsys):
	from stockguidance.cli import validation_setup
	monkeypatch.setattr(validation_setup, "SystemClock", lambda: recommender.storage.clock)
	monkeypatch.setattr(validation_setup, "MySQLConnectionFactory", lambda: recommender.storage.database)
	commits = recommender.storage.database.commits
	arguments = ["--data-root", str(recommender.job.source.data_root), "--dbinfo-file", str(recommender.job.source.database_config), "--start-date", "2024-07-03", "--end-date", "2024-07-08", "--publication-delay-minutes", "15", "--publication-evidence", "Fixture-only", "--ticker-ids", ",".join(map(str, recommender.job.features.ticker_ids)), "--lookback-sessions", "2", "--signal-mode", "entry_open", "--format", "json"]
	assert validate_database.main(arguments) == 2
	payload = json.loads(capsys.readouterr().out)
	assert any(row["check"] == "entry_open_observations" for row in payload["findings"])
	assert not (recommender.job.source.data_root / "market_data" / "openings").exists()
	assert recommender.storage.database.commits == commits


def test_opening_capture_frontend_uses_explicit_job_universe_and_only_previous_daily_data(recommender, job, tmp_path, monkeypatch, capsys):
	features, store, services, calls = recommendation_openings(recommender)
	job.update(source=asdict(recommender.job.source.identity), data_root=str(recommender.job.source.data_root), database_config=str(recommender.job.source.database_config), features=asdict(features))
	monkeypatch.setattr(update_openings, "SystemClock", lambda: recommender.storage.clock)
	monkeypatch.setattr(update_openings, "MySQLConnectionFactory", lambda: recommender.storage.database)
	monkeypatch.setattr(update_openings, "ConfiguredOpeningProvider", lambda: services.openings.provider)
	path = tmp_path / "capture.json"
	path.write_text(json.dumps(job), encoding="utf-8")
	assert update_openings.main(["--config", str(path), "--session", "2024-07-09"]) == 0
	assert json.loads(capsys.readouterr().out)["successful"]
	assert len(calls) == len(features.ticker_ids)


@pytest.mark.parametrize("allocation", ["alternate_sessions", "staggered_halves"])
def test_documented_comparison_frontend_runs_real_paired_chronological_fits(simulation, simulation_json, opening_signals, tmp_path, monkeypatch, capsys, allocation):
	simulation_json["data_root"] = str(opening_signals.store.root)
	simulation_json["allocation"]["policy"] = allocation
	configuration = asdict(simulation.configuration)
	simulation_json["retraining"] = {"start_date": "2024-07-02", "end_date": "2024-07-11", "decision_minutes_before_close": 30, "every_buy_decisions": 2, "model": configuration.pop("model"), "training": configuration}
	JsonHistoryStore(opening_signals.store.root / "history").save(simulation.samples.boundary)
	monkeypatch.setattr(signal_comparison_runtime, "ExchangeCalendar", lambda *values: simulation.calendar)
	monkeypatch.setattr(signal_comparison_runtime, "MySQLConnectionFactory", lambda: object())
	monkeypatch.setattr(signal_comparison_runtime, "MySQLValidationRepository", lambda *values: object())
	monkeypatch.setattr(signal_comparison_runtime, "VerifiedCloseRepository", lambda *values: simulation.samples.repository)
	monkeypatch.setattr(signal_comparison_runtime, "VerifiedEligibilityRepository", lambda *values: simulation.liquidity)
	path = tmp_path / "compare.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")
	assert compare_signals.main(["--config", str(path), "--artifacts-directory", str(tmp_path / "paired")]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["seed"] == 42 and payload["epochs"] == 1
	assert payload["previous_close"]["complete"] and payload["entry_open"]["complete"]
	assert payload["previous_close"]["allocation"] == payload["entry_open"]["allocation"]
