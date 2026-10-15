from dataclasses import asdict, replace
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pytest

from stockguidance.artifacts.loading import load_artifact
from stockguidance.cli import train_model as cli
from stockguidance.cli import training_job
from stockguidance.config.source import SourceConfig
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.reporting.training import training_payload
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


@pytest.fixture
def job(training, samples, tmp_path):
	dataset, evaluation, config = training
	return {
		"source": asdict(samples.boundary.source), "data_root": str(tmp_path / "source"),
		"database_config": str(tmp_path / "fixture-secret-database.id"),
		"features": asdict(samples.specification), "model": asdict(config.model),
		"training": {name: value for name, value in asdict(config).items() if name != "model"},
		"start_date": "2024-07-03", "end_date": "2024-07-08", "training_cutoff": dataset.training_cutoff.isoformat(),
		"publication_delay_minutes": 15, "publication_evidence": "Fixture-only publication policy",
		"decision_minutes_before_close": 30,
		"evaluation": {"start_date": "2024-07-09", "end_date": "2024-07-11", "label_cutoff": evaluation.training_cutoff.isoformat()},
	}


def test_full_training_cli_reads_evidenced_history_and_returns_explicit_artifact(job, samples, tmp_path, monkeypatch, capsys):
	config_path = tmp_path / "job.json"
	config_path.write_text(json.dumps(job), encoding="utf-8")
	source = SourceConfig(samples.boundary.source, tmp_path / "source", tmp_path / "fixture-secret-database.id", "fixture-private-reference")
	JsonHistoryStore(source.data_root / "history").save(samples.boundary)
	calls = []

	def repository(source_config, repository):
		calls.append(source_config)
		assert source_config.data_root == source.data_root
		assert source_config.database_config == source.database_config
		return samples.repository

	monkeypatch.setattr(training_job, "VerifiedCloseRepository", repository)
	path = tmp_path / "explicit.sgmodel"
	assert cli.main(["--config", str(config_path), "--artifact", str(path)]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] and payload["artifact"]["path"] == str(path)
	assert payload["evaluation_kind"] == "chronological_holdout"
	assert payload["training_samples"] == 1 and payload["evaluation_samples"] == 2
	assert len(calls) == 1
	assert load_artifact(path).artifact_id == payload["artifact"]["artifact_id"]
	assert "fixture-secret-database" not in json.dumps(payload)
	assert not source.database_config.exists()
	assert all(session.session_date >= samples.boundary.history_start for query in samples.repository.calls for session in query.scope.sessions)
	assert all(query.scope.as_of <= datetime(2024, 7, 11, 21, tzinfo=timezone.utc) for query in samples.repository.calls)


def test_typed_history_workflow_uses_identical_shared_samples(training, samples, tmp_path):
	dataset, evaluation, config = training
	request = HistoryTrainingRequest(tuple(sample.timing for sample in dataset.samples), dataset.training_cutoff, samples.specification, samples.target, config, tmp_path / "model", tuple(sample.timing for sample in evaluation.samples), evaluation.training_cutoff)
	result = train_from_history(request, samples.boundary, samples.calendar, samples.publication, samples.repository)
	loaded = load_artifact(result.artifact)
	assert loaded.metadata.training.sample_keys == tuple(sample.cache_key for sample in dataset.eligible)
	assert loaded.metadata.training.excluded_sample_keys == tuple(sample.cache_key for sample in dataset.excluded)
	report = training_payload(result)
	assert report["training_metric_scope"] == "in_sample"
	assert report["evaluation_cutoff"] == evaluation.training_cutoff.isoformat()


@pytest.mark.parametrize("problem", ["unknown_field", "naive_cutoff", "entry_open", "old_target_transform", "missing_features", "overlap", "source_adjustment"])
def test_job_validation_fails_before_repository_work(job, tmp_path, problem, monkeypatch, capsys):
	if problem == "unknown_field":
		job["api_key"] = "fixture-must-not-be-logged"
	elif problem == "naive_cutoff":
		job["training_cutoff"] = "2024-07-09T19:00:00"
	elif problem == "entry_open":
		job["features"]["signal_mode"] = "entry_open"
	elif problem == "old_target_transform":
		job["training"]["day_change_centered"] = "normalized_labels"
	elif problem == "missing_features":
		del job["features"]
	elif problem == "overlap":
		job["evaluation"]["start_date"] = "2024-07-05"
	else:
		job["source"]["adjustment"] = "unadjusted"
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job), encoding="utf-8")
	monkeypatch.setattr(cli, "execute_job", lambda *args, **kwargs: pytest.fail("Invalid job reached execution"))
	assert cli.main(["--config", str(path), "--artifact", str(tmp_path / "model")]) == 2
	assert json.loads(capsys.readouterr().out) == {"successful": False, "failure": "invalid_training_configuration"}
	assert not (tmp_path / "model").exists()


def test_training_failure_is_nonzero_and_redacted(job, tmp_path, monkeypatch, capsys):
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job), encoding="utf-8")

	def fail(*args, **kwargs):
		raise RuntimeError("fixture-password-that-must-never-appear")

	monkeypatch.setattr(cli, "execute_job", fail)
	assert cli.main(["--config", str(path), "--artifact", str(tmp_path / "model")]) == 1
	payload = json.loads(capsys.readouterr().out)
	assert payload == {"successful": False, "failure": "training_or_export_failed"}
	assert not (tmp_path / "model").exists()


def test_unknown_history_requires_evidence_without_creating_directories(job, tmp_path):
	path = tmp_path / "job.json"
	path.write_text(json.dumps(job), encoding="utf-8")
	with pytest.raises(ValueError, match="persisted"):
		training_job.execute_job(training_job.load_job(path), tmp_path / "model")
	assert not (tmp_path / "source").exists()


def test_module_and_root_frontends_are_thin_and_parse_explicit_paths(tmp_path, capsys):
	import train_pytorch_network
	import train_pytorch_network_cli

	assert train_pytorch_network.main is cli.main
	assert train_pytorch_network_cli.build_parser is cli.build_parser
	arguments = cli.parse_args(["--config", "job.json", "--artifact", "model.sgmodel", "--resume", "state.pt"])
	assert arguments.artifact.name == "model.sgmodel"
	with pytest.raises(SystemExit) as error:
		cli.main(["--help"])
	assert error.value.code == 0
	assert "mature" in capsys.readouterr().out


def test_documented_job_is_valid_without_accessing_live_data(tmp_path):
	document = (Path(__file__).resolve().parents[2] / "docs" / "model_training.md").read_text(encoding="utf-8")
	match = re.search(r"```json\n(.*?)\n```", document, re.DOTALL)
	assert match is not None
	path = tmp_path / "job.json"
	path.write_text(match.group(1), encoding="utf-8")
	job = training_job.load_job(path)
	assert job.features.ticker_ids == (17, 3)
	assert job.configuration.model.final_activation == "none"


def test_touched_legacy_modules_parse_without_importing_controllers():
	root = Path(__file__).resolve().parents[2]
	for path in ("trainer/legacy_training.py", "trainer/legacy_arguments.py", "trainer/state/family_run.py", "trainer/state/single_run.py", "trainer/model_loader.py", "trainer/train_single_run.py", "trainer/io_run.py", "choose_next_day_stocks.py", "incremental_training_harness.py", "batch_run.py", "models/__init__.py"):
		ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
