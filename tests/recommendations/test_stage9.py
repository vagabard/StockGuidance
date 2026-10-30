import json
from dataclasses import replace
from pathlib import Path

import pytest

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.artifacts.loading import load_artifact
from stockguidance.inference.scores import score_sample
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.recommendations.configuration import RefreshMode
from stockguidance.recommendations.eligibility import EligibilityState, LiquidityObservation, TickerEligibility
from stockguidance.recommendations.result import RankedTicker, RecommendationResult
from stockguidance.recommendations.scoring import recommendation_statistics
from stockguidance.runs.contracts import RunStatus
from stockguidance.runs.service import RunDirectoryService
from stockguidance.targets.specification import TargetKind, TargetSpecV2
from stockguidance.workflows.failures import StageExecutionError
from stockguidance.workflows.recommend import recommend


def test_validate_only_reuses_verified_data_without_provider_calls(recommender, tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs", project=tmp_path)
	job = replace(recommender.job, refresh_mode=RefreshMode.VALIDATE_ONLY)
	run = recommend(job, recommender.storage.boundary, recommender.calendar, recommender.services, recommender.storage.clock, runs=service)

	assert isinstance(run.refresh.mode, str)
	assert run.refresh.mode == RefreshMode.VALIDATE_ONLY.value
	assert run.refresh.successful
	assert not recommender.storage.provider.calls


def test_recommendation_run_saves_complete_scores_and_report(recommender, tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs", project=tmp_path)
	run = recommend(recommender.job, recommender.storage.boundary, recommender.calendar, recommender.services, recommender.storage.clock, runs=service)
	manifest = service.validate(next(item.run_id for item in service.list_runs() if item.manifest is not None))

	assert manifest.status is RunStatus.COMPLETED
	assert manifest.results["predictions"]["rows"]
	assert manifest.results["eligibility"]
	assert manifest.results["reports"] == {"path": "reports/recommendation.json", "schema": "stockguidance-recommendation-1"}
	assert manifest.artifacts and manifest.artifacts[0].external_reference
	report = json.loads((tmp_path / "runs" / manifest.run_id / "reports" / "recommendation.json").read_text(encoding="utf-8"))
	assert report["scores"] and report["ranking"]
	assert report["scores"] == manifest.results["predictions"]["rows"]
	assert run.recommendation.eligible_count == 1


def test_failed_recommendation_attempt_is_saved_without_raw_error_text(recommender, tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs", project=tmp_path)
	missing = ArtifactReference(tmp_path / "missing.sgmodel", "0" * 64)
	job = replace(recommender.job, model=replace(recommender.job.model, artifact_id=missing.artifact_id), current_artifact=missing)

	with pytest.raises(StageExecutionError):
		recommend(job, recommender.storage.boundary, recommender.calendar, recommender.services, recommender.storage.clock, runs=service)

	items = service.list_runs(include_invalid=False)
	assert len(items) == 1 and items[0].manifest is not None
	assert items[0].manifest.status is RunStatus.FAILED
	assert items[0].manifest.failure is not None
	assert "missing.sgmodel" not in items[0].manifest.failure.message


def test_versioned_target_can_return_a_valid_empty_selection(recommendation_request) -> None:
	request = replace(recommendation_request, target=TargetSpecV2(kind=TargetKind.RAW_RETURN), allow_empty_selection=True)
	result = RecommendationResult(request, "fixture-artifact", "revision-3", ())

	assert result.ranking == ()
	assert result.score_units == "fractional_return"
	assert result.median_eligible_score is None
	assert result.median_returned_score is None


def test_score_output_retains_stable_symbol_labels(recommender) -> None:
	run = recommender.run()
	scores = score_sample(load_artifact(recommender.training.artifact), run.sample, ("AAA",))

	assert scores[0].ticker_id == recommender.job.features.ticker_ids[0]
	assert scores[0].symbol == "AAA"


def test_recommendation_statistics_uses_target_space_medians(timing) -> None:
	def eligible(ticker_id: int, transactions: int) -> TickerEligibility:
		observation = LiquidityObservation(ticker_id, transactions, ObservationTiming(timing.previous_session, timing.previous_session.closes_at, timing.previous_session.closes_at), "fixture-revision")
		return TickerEligibility(ticker_id, EligibilityState.ELIGIBLE, "eligible", observation)

	scores = tuple(RankedTicker(ticker_id, score) for ticker_id, score in ((1, 1.0), (2, 3.0), (3, 5.0), (4, 7.0)))
	eligibility = tuple(eligible(ticker_id, 100) for ticker_id in (1, 2, 3, 4))
	count, eligible_median, returned_median = recommendation_statistics(scores, eligibility, (scores[3], scores[2], scores[1]))

	assert count == 4
	assert eligible_median == 4.0
	assert returned_median == 5.0

