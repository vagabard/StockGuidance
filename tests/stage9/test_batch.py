from dataclasses import asdict
import json
from pathlib import Path

import pytest

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.cli import batch as cli
from stockguidance.config.batch import load_batch
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.recommendations.jobs import load_configuration
from stockguidance.workflows import batch as workflow
from stockguidance.workflows import recommendation_runtime
from stockguidance.workflows.batch import BatchPlan, BatchStep, run_batch
from stockguidance.workflows.batch_tasks import ProducedArtifact, RecommendationTask, TaskOutcome, TrainingTask
from stockguidance.training.jobs import TrainingJob
from stockguidance.training import jobs as training_jobs


@pytest.mark.parametrize("policy", ["current", "retrain"])
@pytest.mark.parametrize("allocation", ["alternate_sessions", "staggered_halves"])
def test_documented_batch_uses_typed_workflows_and_the_returned_artifact(recommender, job_json, tmp_path, monkeypatch, capsys, policy, allocation):
	monkeypatch.setattr(recommendation_runtime, "SystemClock", lambda: recommender.storage.clock)
	monkeypatch.setattr(recommendation_runtime, "MySQLConnectionFactory", lambda: recommender.storage.database)
	monkeypatch.setattr(recommendation_runtime, "ConfiguredPolygonProvider", lambda retries: recommender.storage.provider)
	monkeypatch.setattr(training_jobs, "MySQLConnectionFactory", lambda: recommender.storage.database)
	job_json["allocation"]["policy"] = allocation
	steps = []
	if policy == "retrain":
		job_json["retraining"] = {"start_date": "2024-07-03", "end_date": "2024-07-08", "decision_minutes_before_close": 30, "model": asdict(recommender.plan.configuration.model), "training": {name: value for name, value in asdict(recommender.plan.configuration).items() if name != "model"}}
		selection = str(tmp_path / "retrained.sgmodel")
	else:
		training = {"source": job_json["source"], "data_root": job_json["data_root"], "database_config": job_json["database_config"], "features": job_json["features"], "model": asdict(recommender.plan.configuration.model), "training": {name: value for name, value in asdict(recommender.plan.configuration).items() if name != "model"}, "start_date": "2024-07-03", "end_date": "2024-07-08", "training_cutoff": recommender.storage.clock.now().isoformat(), "publication_delay_minutes": 15, "publication_evidence": "Fixture-only", "decision_minutes_before_close": 30, "evaluation": None}
		training_path = tmp_path / "training.json"
		training_path.write_text(json.dumps(training), encoding="utf-8")
		steps.append({"name": "fit", "kind": "train", "config": str(training_path), "report": str(tmp_path / "fit-report.json"), "options": {"artifact": str(tmp_path / "fit.sgmodel"), "checkpoint": None, "resume": None}})
		selection = {"from_step": "fit"}
	configuration = tmp_path / "recommend.json"
	configuration.write_text(json.dumps(job_json), encoding="utf-8")
	report = tmp_path / "recommend-report.json"
	steps.append({"name": "recommend", "kind": "recommend", "config": str(configuration), "report": str(report), "options": {"policy": policy, "artifact": selection, "artifact_kind": "model", "portfolio_state": None, "capital": None, "fee_per_fill": "0.00"}})
	path = tmp_path / "batch.json"
	path.write_text(json.dumps({"schema": "stockguidance-batch-1", "steps": steps}), encoding="utf-8")
	assert cli.main(["--config", str(path)]) == 0
	result = json.loads(capsys.readouterr().out)
	payload = json.loads(report.read_text(encoding="utf-8"))
	assert result["successful"] and payload["successful"]
	assert payload["model_policy"] == policy and payload["allocation"]["policy"] == allocation
	assert payload["target"]["formula"] == "close(T+1) / close(T) - 1"
	assert payload["fills_recorded"] is False
	assert payload["artifact_id"] == result["steps"][-1]["artifact_id"]
	if policy == "current":
		assert payload["artifact_id"] == result["steps"][0]["artifact_id"]
		assert payload["training"] is None
	assert "database_config" not in str(payload) and "password" not in str(payload)


def make_task(recommender, tmp_path):
	plan = recommender.plan
	job = TrainingJob(recommender.job.source, recommender.job.features, plan.configuration, plan.decisions[0].entry_session.session_date, plan.decisions[-1].entry_session.session_date, recommender.storage.clock.now(), recommender.job.publication, plan.decisions[0].entry_time - plan.decisions[0].decision_cutoff, None, None, None)
	return TrainingTask(job, tmp_path / "model.sgmodel")


@pytest.mark.parametrize("failure", ["execution", "report", "incomplete"])
def test_batch_failure_stops_later_steps_and_never_reports_success(recommender, tmp_path, monkeypatch, failure):
	task = make_task(recommender, tmp_path)
	plan = BatchPlan((BatchStep("first", task, tmp_path / "first.json"), BatchStep("second", TrainingTask(task.job, tmp_path / "second.sgmodel"), tmp_path / "second.json")))
	calls = []

	def execute(task, artifacts):
		calls.append(task)
		if failure == "execution":
			raise RuntimeError("fixture-password-never-log")
		return TaskOutcome({"schema": "stockguidance-training-1", "successful": True}, 2 if failure == "incomplete" else 0)

	if failure == "report":
		monkeypatch.setattr(workflow, "write_report", lambda *values: (_ for unused in ()).throw(OSError("fixture-secret")))
	result = run_batch(plan, executor=execute)
	assert result.exit_code == (2 if failure == "incomplete" else 1)
	assert len(calls) == 1 and not (tmp_path / "second.json").exists()
	assert result.payload()["successful"] is False and "secret" not in str(result.payload())


@pytest.mark.parametrize("problem", ["duplicate_name", "duplicate_output", "forward_reference"])
def test_invalid_plans_are_rejected_before_any_output(recommender, job_json, tmp_path, problem):
	task = make_task(recommender, tmp_path)
	first = BatchStep("first", task, tmp_path / "first.json")
	second = BatchStep("second", TrainingTask(task.job, tmp_path / "second.sgmodel"), tmp_path / "second.json")
	if problem == "duplicate_name":
		second = BatchStep("first", second.task, second.report)
	elif problem == "duplicate_output":
		second = BatchStep("second", second.task, first.report)
	else:
		configuration_path = tmp_path / "recommend.json"
		configuration_path.write_text(json.dumps(job_json), encoding="utf-8")
		configuration = load_configuration(configuration_path)
		first = BatchStep("first", RecommendationTask(configuration, ModelRefreshPolicy.CURRENT, ProducedArtifact("future")), tmp_path / "first.json")
	with pytest.raises(ValueError):
		BatchPlan((first, second))
	assert not (tmp_path / "first.json").exists()
