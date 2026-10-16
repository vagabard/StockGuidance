from dataclasses import asdict, replace
from datetime import date
import json
from pathlib import Path
import re

import pytest

from stockguidance.cli import recommend as cli
from stockguidance.cli import recommendation_setup as setup
from stockguidance.cli.recommendation_configuration import load_configuration
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.workflows import recommend as workflow


def configure_cli(recommender, monkeypatch):
	monkeypatch.setattr(setup, "SystemClock", lambda: recommender.storage.clock)
	monkeypatch.setattr(setup, "LegacyWriteConnectionFactory", lambda: recommender.storage.database)
	monkeypatch.setattr(setup, "ConfiguredPolygonProvider", lambda retries: recommender.storage.provider)


def arguments(recommender, path, policy="current"):
	values = ["--config", str(path), "--policy", policy, "--artifact", str(recommender.job.current_artifact.path if policy == "current" else recommender.plan.artifact_path)]
	if policy == "current":
		values += ["--artifact-id", recommender.job.model.artifact_id]
	return values


@pytest.mark.parametrize("policy", ["current", "retrain"])
def test_complete_cli_assembles_custom_source_and_returns_structured_results(recommender, job_json, tmp_path, monkeypatch, capsys, policy):
	configure_cli(recommender, monkeypatch)
	if policy == "retrain":
		job_json["retraining"] = {"start_date": "2024-07-03", "end_date": "2024-07-08", "decision_minutes_before_close": 30, "model": asdict(recommender.plan.configuration.model), "training": {name: value for name, value in asdict(recommender.plan.configuration).items() if name != "model"}}
	path = tmp_path / "recommend.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")
	calls = []
	original = workflow.train_from_history

	def trainer(*values):
		calls.append(values[0])
		return original(*values)

	monkeypatch.setattr(workflow, "train_from_history", trainer)
	assert cli.main(arguments(recommender, path, policy)) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] is True and payload["model_policy"] == policy
	assert payload["entry_session"] == "2024-07-09" and payload["exit_session"] == "2024-07-10"
	assert payload["ranking"] and payload["coverage"]["validated"]
	assert len(calls) == (0 if policy == "current" else 1)
	assert str(recommender.job.source.database_config) not in str(payload)
	assert all(config == recommender.job.source.database_config for config in recommender.storage.database.configs)


@pytest.mark.parametrize("problem", ["extra_secret", "entry_open", "old_transform", "source_adjustment", "invalid_policy_config", "missing_features", "bad_top_k", "bad_dates"])
def test_invalid_configuration_is_nonzero_before_dependency_assembly(recommender, job_json, tmp_path, monkeypatch, capsys, problem):
	if problem == "extra_secret":
		job_json["api_key"] = "fixture-secret-never-log"
	elif problem == "entry_open":
		job_json["features"]["signal_mode"] = "entry_open"
	elif problem == "old_transform":
		job_json["features"]["feature_order"] = ["open_to_close"]
	elif problem == "source_adjustment":
		job_json["source"]["adjustment"] = "unadjusted"
	elif problem == "invalid_policy_config":
		job_json["retraining"] = {"start_date": "2024-07-03", "end_date": "2024-07-05", "decision_minutes_before_close": 30, "model": {}, "training": {}}
	elif problem == "missing_features":
		del job_json["features"]
	elif problem == "bad_top_k":
		job_json["top_k"] = 0
	else:
		job_json["end_date"] = "2024-07-10"
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")
	monkeypatch.setattr(cli, "assemble_and_recommend", lambda *values: pytest.fail("Invalid configuration reached setup"))
	assert cli.main(arguments(recommender, path)) == 2
	payload = json.loads(capsys.readouterr().out)
	assert not payload["successful"] and "fixture-secret" not in str(payload)
	if problem == "entry_open":
		assert "entry_open" in payload["message"] and "Stage 10" in payload["message"]
	assert not recommender.storage.provider.calls


@pytest.mark.parametrize("values", [[], ["--config", "job.json", "--policy", "old", "--artifact", "x"], ["--config", "job.json", "--policy", "current", "--artifact", "x"], ["--config", "job.json", "--policy", "retrain", "--artifact", "x", "--artifact-id", "0" * 64], ["--config", "job.json", "--policy", "retrain", "--artifact", "x", "--artifact-kind", "ensemble"], ["--config", "job.json", "--policy", "retrain", "--artifact", "x", "--max-retries", "0"]])
def test_invalid_arguments_exit_two(values):
	with pytest.raises(SystemExit) as error:
		cli.main(values)
	assert error.value.code == 2


def test_setup_failure_is_redacted_and_nonzero(recommender, job_json, tmp_path, monkeypatch, capsys):
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")

	def fail(*values):
		raise RuntimeError("fixture-password-never-log")

	monkeypatch.setattr(cli, "assemble_and_recommend", fail)
	assert cli.main(arguments(recommender, path)) == 1
	assert "password" not in capsys.readouterr().out


def test_required_workflow_failure_produces_no_success_payload(recommender, job_json, tmp_path, monkeypatch, capsys):
	configure_cli(recommender, monkeypatch)
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")
	recommender.job.current_artifact.path.write_bytes(b"old-model-manifest")
	assert cli.main(arguments(recommender, path)) == 1
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] is False and payload["stage"] == "artifact"
	assert not recommender.storage.database.statements and not recommender.storage.provider.calls


def test_reporting_failure_is_nonzero_without_a_success_payload(recommender, job_json, tmp_path, monkeypatch, capsys):
	configure_cli(recommender, monkeypatch)
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")

	def fail(result):
		raise OSError("fixture-report-password-never-log")

	monkeypatch.setattr(cli, "recommendation_payload", fail)
	assert cli.main(arguments(recommender, path)) == 1
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] is False and "password" not in str(payload)


def test_text_output_and_root_frontends_use_the_same_cli(recommender, job_json, tmp_path, monkeypatch, capsys):
	import choose_next_day_stocks
	import run_recommender_from_model_dir

	assert choose_next_day_stocks.main is cli.main
	assert run_recommender_from_model_dir.main is cli.main
	configure_cli(recommender, monkeypatch)
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")
	assert choose_next_day_stocks.main(arguments(recommender, path) + ["--format", "text"]) == 0
	output = capsys.readouterr().out
	assert "Ranked suggestions" in output and "Stage 8" in output
	assert "2024-07-09" in output and "2024-07-10" in output


def test_unknown_history_fails_without_creating_paths(recommender, job_json, tmp_path, monkeypatch, capsys):
	configure_cli(recommender, monkeypatch)
	job_json["data_root"] = str(tmp_path / "unknown")
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")
	assert cli.main(arguments(recommender, path)) == 1
	assert json.loads(capsys.readouterr().out)["successful"] is False
	assert not (tmp_path / "unknown").exists() and not recommender.storage.database.statements


def test_documented_configuration_parses_without_database_or_provider_calls(tmp_path):
	document = (Path(__file__).resolve().parents[2] / "docs" / "recommendations.md").read_text(encoding="utf-8")
	match = re.search(r"```json\n(.*?)\n```", document, re.DOTALL)
	assert match is not None
	path = tmp_path / "documented.json"
	path.write_text(match.group(1), encoding="utf-8")
	configuration = load_configuration(path)
	assert configuration.features.ticker_ids == (17, 3)
	assert configuration.eligibility.minimum_transactions == 200
