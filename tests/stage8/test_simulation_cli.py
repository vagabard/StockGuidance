from dataclasses import asdict, replace
from datetime import date, timedelta
import json

import pytest

from stockguidance.cli import simulate as cli
from stockguidance.workflows import simulation_runtime as setup
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.features.specification import FeatureSpec
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


@pytest.fixture
def simulation_json(simulation, tmp_path, monkeypatch):
	JsonHistoryStore(tmp_path / "source" / "history").save(simulation.samples.boundary)
	(tmp_path / "custom-config.id").write_text(json.dumps({"host": "fixture", "database": "fixture", "user": "fixture", "password": ""}), encoding="utf-8")
	monkeypatch.setattr(setup, "ExchangeCalendar", lambda *values: simulation.calendar)
	monkeypatch.setattr(setup, "MySQLConnectionFactory", lambda: object())
	monkeypatch.setattr(setup, "MySQLValidationRepository", lambda *values: object())
	monkeypatch.setattr(setup, "VerifiedCloseRepository", lambda *values: simulation.samples.repository)
	monkeypatch.setattr(setup, "VerifiedEligibilityRepository", lambda *values: simulation.liquidity)
	return {"source": asdict(simulation.request.source), "data_root": str(tmp_path / "source"), "database_config": str(tmp_path / "custom-config.id"), "features": asdict(simulation.request.features), "start_date": "2024-07-05", "end_date": "2024-07-11", "allocation": {"policy": "alternate_sessions", "anchor_date": "2024-07-05"}, "initial_capital": "1000.00", "decision_minutes_before_close": 30, "publication": {"delay_minutes": 15, "evidence": "Fixture-only publication"}, "execution_audit_cutoff": "2024-07-11T21:00:00+00:00", "eligibility": {"minimum_transactions": 200}, "top_k": 1, "fee_per_fill": "0.00", "retraining": None}


def arguments(simulation, path):
	return ["--config", str(path), "--model-policy", "fixed", "--artifact", str(simulation.reference.path), "--artifact-id", simulation.reference.artifact_id]


@pytest.mark.parametrize("policy", ["alternate_sessions", "staggered_halves"])
@pytest.mark.parametrize("model_policy", ["fixed", "chronological_retrain"])
def test_complete_cli_paths_for_both_schedules_and_model_policies(simulation, simulation_json, tmp_path, capsys, policy, model_policy):
	simulation_json["allocation"]["policy"] = policy
	if model_policy == "chronological_retrain":
		configuration = asdict(simulation.configuration)
		simulation_json["retraining"] = {"start_date": "2024-07-02", "end_date": "2024-07-11", "decision_minutes_before_close": 30, "every_buy_decisions": 2, "model": configuration.pop("model"), "training": configuration}
	path = tmp_path / "simulate.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")
	values = arguments(simulation, path) if model_policy == "fixed" else ["--config", str(path), "--model-policy", model_policy, "--artifacts-directory", str(tmp_path / "walk-forward")]
	assert cli.main(values) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] and payload["complete"]
	assert payload["model_policy"] == model_policy and payload["allocation"]["policy"] == policy
	assert len(payload["final_portfolio"]["allocations"]) == (2 if policy == "staggered_halves" else 1)
	assert payload["target"]["formula"] == "close(T+1) / close(T) - 1"
	assert bool(payload["training_runs"]) == (model_policy == "chronological_retrain")
	assert str(tmp_path / "custom-config.id") not in str(payload)


@pytest.mark.parametrize("problem", ["extra_secret", "bad_target_feature", "entry_open", "float_cash", "negative_fee", "invalid_cadence", "future_audit"])
def test_invalid_simulation_configuration_fails_before_setup(simulation, simulation_json, tmp_path, monkeypatch, capsys, problem):
	if problem == "extra_secret":
		simulation_json["api_key"] = "fixture-secret-never-log"
	elif problem == "bad_target_feature":
		simulation_json["features"]["feature_order"] = ["close_over_open"]
	elif problem == "entry_open":
		simulation_json["features"]["signal_mode"] = "entry_open"
	elif problem == "float_cash":
		simulation_json["initial_capital"] = 1000.00
	elif problem == "negative_fee":
		simulation_json["fee_per_fill"] = "-0.01"
	elif problem == "invalid_cadence":
		simulation_json["retraining"] = {"every_buy_decisions": 0}
	else:
		simulation_json["execution_audit_cutoff"] = "2100-01-01T00:00:00+00:00"
	path = tmp_path / "invalid.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")
	monkeypatch.setattr(cli, "assemble_and_simulate", lambda *values: pytest.fail("Invalid config reached setup"))
	assert cli.main(arguments(simulation, path)) == 2
	output = capsys.readouterr().out
	assert not json.loads(output)["successful"] and "secret" not in output
	if problem == "entry_open":
		assert "entry_open" in output and "opening feature" in output


@pytest.mark.parametrize("values", [[], ["--config", "x", "--model-policy", "fixed"], ["--config", "x", "--model-policy", "chronological_retrain", "--artifact", "x"], ["--config", "x", "--model-policy", "fixed", "--artifact", "x", "--artifact-id", "0" * 64, "--artifacts-directory", "x"], ["--config", "x", "--model-policy", "chronological_retrain", "--artifacts-directory", "x", "--artifact-kind", "ensemble"]])
def test_invalid_model_policy_arguments_exit_two(values):
	with pytest.raises(SystemExit) as error:
		cli.main(values)
	assert error.value.code == 2


def test_missing_fill_returns_two_and_reports_retained_positions(simulation, simulation_json, tmp_path, capsys):
	simulation.samples.repository.missing.add((simulation.calendar.session(date(2024, 7, 8)), 17))
	path = tmp_path / "missing.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")
	assert cli.main(arguments(simulation, path)) == 2
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] and not payload["complete"]
	assert any(item["kind"] == "unfilled_exit" for item in payload["events"])


def test_unknown_history_fails_without_creating_data_or_models(simulation, simulation_json, tmp_path, capsys):
	simulation_json["data_root"] = str(tmp_path / "unknown-root")
	path = tmp_path / "unknown.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")
	assert cli.main(arguments(simulation, path)) == 1
	assert not json.loads(capsys.readouterr().out)["successful"]
	assert not (tmp_path / "unknown-root").exists()


def test_required_setup_or_reporting_failure_is_redacted(simulation, simulation_json, tmp_path, monkeypatch, capsys):
	path = tmp_path / "job.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")

	def fail(*values):
		raise RuntimeError("fixture-password-never-log")

	monkeypatch.setattr(cli, "simulation_payload", fail)
	assert cli.main(arguments(simulation, path)) == 1
	output = capsys.readouterr().out
	assert not json.loads(output)["successful"] and "password" not in output


def test_text_cli_uses_new_package_without_legacy_directory_discovery(simulation, simulation_json, tmp_path, capsys):
	path = tmp_path / "job.json"
	path.write_text(json.dumps(simulation_json), encoding="utf-8")
	assert cli.main(arguments(simulation, path) + ["--format", "text"]) == 0
	output = capsys.readouterr().out
	assert "Close-to-next-session-close simulation" in output and "end positions:" in output


def test_simulation_cli_uses_verified_sql_with_nonconsecutive_ids_without_writes(recommender, tmp_path, monkeypatch, capsys):
	job = recommender.job
	features = FeatureSpec(job.features.ticker_ids, 1)
	timing = recommender.plan.decisions[0]
	entry = recommender.calendar.session(date(2024, 7, 2))
	timing = replace(timing, previous_session=recommender.calendar.shift(entry, -1), entry_session=entry, exit_session=recommender.calendar.shift(entry, 1), decision_cutoff=entry.closes_at - timedelta(minutes=30))
	cutoff = recommender.calendar.session(date(2024, 7, 3)).closes_at + timedelta(minutes=16)
	training = train_from_history(HistoryTrainingRequest((timing,), cutoff, features, job.target, recommender.plan.configuration, tmp_path / "pre-period.sgmodel"), recommender.storage.boundary, recommender.calendar, job.publication, recommender.closes)
	fields = {"source": asdict(job.source.identity), "data_root": str(job.source.data_root), "database_config": str(job.source.database_config), "features": asdict(features), "start_date": "2024-07-05", "end_date": "2024-07-08", "allocation": {"policy": "alternate_sessions", "anchor_date": "2024-07-05"}, "initial_capital": "1000.00", "decision_minutes_before_close": 30, "publication": {"delay_minutes": 15, "evidence": "Fixture-only publication"}, "execution_audit_cutoff": "2024-07-08T21:00:00+00:00", "eligibility": {"minimum_transactions": 1}, "top_k": 1, "fee_per_fill": "0.00", "retraining": None}
	path = tmp_path / "verified-simulation.json"
	path.write_text(json.dumps(fields), encoding="utf-8")
	monkeypatch.setattr(setup, "MySQLConnectionFactory", lambda: recommender.storage.database)
	for old, new in ((1, 901), (2, 11), (3, 450), (4, 2), (5, 100)):
		for table, column in (("raw_stock_data", "date_id"), ("daily_delta_stock_data", "date_id"), ("transaction_dates", "id")):
			recommender.storage.database.sql(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new, old))
	recommender.storage.database.statements.clear()
	commits = recommender.storage.database.commits
	assert cli.main(["--config", str(path), "--model-policy", "fixed", "--artifact", str(training.artifact.path), "--artifact-id", training.artifact.artifact_id]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["final_valuation"]["total_equity"] == "1050.00"
	assert recommender.storage.database.commits == commits
	assert all(statement[0].strip().upper().startswith(("SELECT", "START TRANSACTION")) for statement in recommender.storage.database.statements)
	assert not recommender.storage.provider.calls
