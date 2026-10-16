from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from stage7.conftest import retraining_job
from stockguidance.artifacts.loading import load_artifact
from stockguidance.datasets.building import build_sample
from stockguidance.datasets.training import build_training_dataset
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.reporting.recommendations import recommendation_payload
from stockguidance.workflows import recommend as workflow
from stockguidance.workflows.failures import FailureCode, StageExecutionError, StageFailure, WorkflowStage
from stockguidance.workflows.train_model import train_from_history


@pytest.mark.parametrize("policy", list(ModelRefreshPolicy))
def test_both_complete_paths_share_specs_and_use_only_the_explicit_artifact(recommender, policy):
	job = recommender.job if policy is ModelRefreshPolicy.CURRENT else retraining_job(recommender)
	calls = []

	def trainer(request, boundary, calendar, publication, repository):
		calls.append(request)
		assert request.training_cutoff == recommender.storage.clock.now()
		assert request.features == job.features and request.target == job.target
		return train_from_history(replace(request, artifact_path=request.artifact_path.with_name("returned-artifact.sgmodel")), boundary, calendar, publication, repository)

	run = recommender.run(job, trainer=trainer)
	assert len(calls) == (0 if policy is ModelRefreshPolicy.CURRENT else 1)
	assert run.recommendation.request.source == job.source
	assert run.recommendation.request.model.policy is policy
	assert tuple(item.ticker_id for item in run.scores) == job.features.ticker_ids
	assert run.recommendation.ranking == run.scores
	assert run.sample.features.values == pytest.approx((0.0, 0.05))
	assert run.sample.target_state.value == "not_requested"
	assert all(item.observation.timing.session.session_date == date(2024, 7, 8) for item in run.eligibility)
	assert run.validation.complete and run.refresh.successful
	assert recommender.storage.database.configs and all(path == job.source.database_config for path in recommender.storage.database.configs)
	assert not recommender.storage.provider.calls
	if calls:
		assert run.training.training_samples == 2
		assert any(item.entry_session == date(2024, 7, 8) and item.target_state == "not_yet_available" for item in run.training.excluded_training)
		assert run.recommendation.artifact_id == load_artifact(run.training.artifact).artifact_id
		assert run.training.artifact.path.name == "returned-artifact.sgmodel"
		assert not recommender.plan.artifact_path.exists()
	else:
		assert run.training is None
		assert run.recommendation.artifact_id == recommender.training.artifact.artifact_id
	payload = recommendation_payload(run)
	assert payload["entry_session"] == "2024-07-09" and payload["exit_session"] == "2024-07-10"
	assert payload["cash_rest_enforced"] is False and payload["orders_planned"] is False and payload["fills_recorded"] is False
	assert payload["features"]["signal_mode"] == "previous_close"
	assert payload["target"]["formula"] == job.target.raw_formula
	assert payload["data_revision"] and payload["feature_identity"] == run.sample.feature_cache_key
	assert "credential" not in str(payload) and str(job.source.database_config) not in str(payload)


def test_training_and_workflow_inference_have_exact_feature_parity(recommender):
	captured = []

	def trainer(request, boundary, calendar, publication, repository):
		dataset = build_training_dataset(request.decisions, request.training_cutoff, request.features, request.target, boundary, calendar, publication, repository)
		for sample in dataset.samples:
			inference = build_sample(request.features, request.target, sample.timing, boundary, calendar, publication, repository)
			assert sample.features == inference.features
			assert sample.feature_cache_key == inference.feature_cache_key
		captured.append(dataset)
		return train_from_history(request, boundary, calendar, publication, repository)

	run = recommender.run(retraining_job(recommender), trainer=trainer)
	training_form = build_training_dataset((run.sample.timing,), run.sample.timing.decision_cutoff, run.sample.features.specification, run.sample.target_specification, recommender.storage.boundary, recommender.calendar, recommender.job.publication, recommender.closes).samples[0]
	assert training_form.features == run.sample.features and training_form.feature_cache_key == run.sample.feature_cache_key
	assert len(captured) == 1


@pytest.mark.parametrize("stage", ["refresh", "validation", "features", "eligibility", "training", "artifact", "prediction", "ranking"])
def test_failure_at_every_required_stage_prevents_success(recommender, monkeypatch, stage):
	job = retraining_job(recommender)
	calls = []

	def fail(*arguments, **keywords):
		raise RuntimeError("fixture-provider-password-must-not-appear")

	def trainer(*arguments):
		calls.append("train")
		return train_from_history(*arguments)

	targets = {"refresh": "update_market_data", "validation": "validate_database", "features": "build_sample", "eligibility": "assess_eligibility", "prediction": "score_sample", "ranking": "rank_scores", "artifact": "load_recommendation_artifact"}
	if stage == "training":
		trainer = fail
	else:
		monkeypatch.setattr(workflow, targets[stage], fail)
	with pytest.raises(StageExecutionError) as error:
		recommender.run(job, trainer=trainer)
	expected = {"refresh": WorkflowStage.REFRESH, "validation": WorkflowStage.VALIDATION, "features": WorkflowStage.VALIDATION, "eligibility": WorkflowStage.VALIDATION, "training": WorkflowStage.TRAINING, "artifact": WorkflowStage.ARTIFACT, "prediction": WorkflowStage.PREDICTION, "ranking": WorkflowStage.PREDICTION}[stage]
	assert error.value.failure.stage is expected
	assert "password" not in str(error.value)
	if stage in {"refresh", "validation", "features", "eligibility"}:
		assert not calls and not job.retraining.artifact_path.exists()


def test_incomplete_refresh_prevents_validation_and_training(recommender, monkeypatch):
	refresh = recommender.storage.run()
	monkeypatch.setattr(workflow, "update_market_data", lambda *arguments: replace(refresh, failures=(StageFailure(WorkflowStage.REFRESH, FailureCode.PROVIDER, "Fixture failure"),)))
	monkeypatch.setattr(workflow, "validate_database", lambda *arguments: pytest.fail("Validation followed failed refresh"))
	with pytest.raises(StageExecutionError) as error:
		recommender.run(retraining_job(recommender), trainer=lambda *arguments: pytest.fail("Training followed failed refresh"))
	assert error.value.failure.stage is WorkflowStage.REFRESH


def test_missing_required_liquidity_is_explicit_and_stops_retraining(recommender):
	class NoLiquidity:
		def read(self, query):
			assert query.scope.sessions == (recommender.calendar.session(date(2024, 7, 8)),)
			return ()

	services = replace(recommender.services, eligibility=NoLiquidity())
	with pytest.raises(StageExecutionError, match="transaction counts are unavailable"):
		recommender.run(retraining_job(recommender), services_override=services, trainer=lambda *arguments: pytest.fail("Unavailable liquidity reached training"))


def test_no_eligible_tickers_cannot_return_a_successful_ranked_list(recommender):
	with pytest.raises(StageExecutionError, match="No ticker satisfies"):
		recommender.run(replace(recommender.job, eligibility=EligibilityPolicy(200)))


def test_allocation_policy_does_not_change_model_semantics_or_claim_planning(recommender):
	first = recommender.run()
	second = recommender.run(replace(recommender.job, allocation=replace(recommender.job.allocation, policy=AllocationPolicy.STAGGERED_HALVES)))
	assert first.scores == second.scores and first.sample.feature_cache_key == second.sample.feature_cache_key
	assert first.recommendation.artifact_id == second.recommendation.artifact_id
	assert recommendation_payload(second)["cash_rest_enforced"] is False


def test_a_training_run_that_misses_the_entry_close_cannot_recommend(recommender):
	def trainer(*arguments):
		result = train_from_history(*arguments)
		recommender.storage.clock.value = recommender.calendar.session(recommender.job.entry_date).closes_at
		return result

	with pytest.raises(StageExecutionError, match="Entry close passed"):
		recommender.run(retraining_job(recommender), trainer=trainer)
