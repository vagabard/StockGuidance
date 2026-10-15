from dataclasses import replace
from datetime import timedelta

import pytest

from stockguidance.market_data.intervals import ObservationInterval
from stockguidance.models.policy import ModelRefreshPolicy, ModelSelection
from stockguidance.recommendations.request import RecommendationRequest
from stockguidance.recommendations.result import RankedTicker, RecommendationResult
from stockguidance.validation.coverage import CoverageResult, CoverageState, SessionCoverage
from stockguidance.workflows.failures import FailureCode, StageExecutionError, StageFailure, WorkflowStage


def test_result_identifies_timing_specs_artifact_and_revision(recommendation_request: RecommendationRequest) -> None:
	result = RecommendationResult(recommendation_request, "fixture-artifact", "revision-3", (RankedTicker(3, 0.7), RankedTicker(7, 0.4)))
	assert result.request.timing.exit_session.sequence == result.request.timing.entry_session.sequence + 1
	assert result.request.features.ticker_ids == (7, 3)
	assert result.data_revision == "revision-3"
	assert result.request.allocation == recommendation_request.allocation


@pytest.mark.parametrize("ranking", [(), (RankedTicker(3, 1.0), RankedTicker(3, 0.5)), (RankedTicker(99, 1.0),), (RankedTicker(7, 0.0), RankedTicker(3, 1.0))])
def test_invalid_result_rankings(recommendation_request: RecommendationRequest, ranking: tuple[RankedTicker, ...]) -> None:
	with pytest.raises(ValueError):
		RecommendationResult(recommendation_request, "fixture-artifact", "revision-3", ranking)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), True, "0.5"])
def test_scores_are_validated_at_the_prediction_boundary(score: object) -> None:
	with pytest.raises(ValueError, match="finite"):
		RankedTicker(7, score)


def test_current_result_cannot_silently_select_another_model(recommendation_request: RecommendationRequest) -> None:
	with pytest.raises(ValueError, match="requested artifact"):
		RecommendationResult(recommendation_request, "other-model", "revision-3", (RankedTicker(7, 1.0),))
	refreshed = replace(recommendation_request, model=ModelSelection(ModelRefreshPolicy.RETRAIN))
	assert RecommendationResult(refreshed, "new-model", "revision-3", (RankedTicker(7, 1.0),)).artifact_id == "new-model"


def test_request_configuration_mismatches_are_rejected(recommendation_request: RecommendationRequest) -> None:
	with pytest.raises(ValueError, match="identities differ"):
		replace(recommendation_request, coverage=replace(recommendation_request.coverage, source=replace(recommendation_request.coverage.source, universe="other")))
	with pytest.raises(ValueError, match="decision cutoff"):
		replace(recommendation_request, coverage=replace(recommendation_request.coverage, as_of=recommendation_request.coverage.as_of + timedelta(days=1)))
	with pytest.raises(ValueError, match="allocation anchor"):
		replace(recommendation_request, allocation=replace(recommendation_request.allocation, anchor_session=recommendation_request.timing.exit_session))
	with pytest.raises(ValueError, match="calendars differ"):
		replace(recommendation_request, allocation=replace(recommendation_request.allocation, anchor_session=replace(recommendation_request.allocation.anchor_session, calendar_id="other")))


def test_external_source_and_range_values_are_validated(recommendation_request: RecommendationRequest) -> None:
	with pytest.raises(ValueError, match="start must not follow"):
		replace(recommendation_request.coverage, start=recommendation_request.coverage.end + timedelta(days=1))
	with pytest.raises(ValueError, match="session date"):
		replace(recommendation_request.coverage, start=recommendation_request.timing.decision_cutoff)
	with pytest.raises(ValueError, match="nonempty"):
		replace(recommendation_request.source.identity, universe="")
	with pytest.raises(ValueError, match="reserved"):
		replace(recommendation_request.source.identity, interval=ObservationInterval.FIVE_MINUTES)


@pytest.mark.parametrize("state", list(CoverageState))
def test_coverage_categories_are_distinct(recommendation_request: RecommendationRequest, state: CoverageState) -> None:
	assessment = SessionCoverage(recommendation_request.timing.previous_session, state, "Fixture provider evidence")
	result = CoverageResult(recommendation_request.coverage, (assessment,))
	assert result.assessed_sessions_complete is (state is CoverageState.COMPLETE)
	assert not CoverageResult(recommendation_request.coverage, ()).assessed_sessions_complete


def test_inconsistent_coverage_cannot_be_presented_as_complete(recommendation_request: RecommendationRequest) -> None:
	assessment = SessionCoverage(recommendation_request.timing.previous_session, CoverageState.COMPLETE, "Fixture evidence")
	with pytest.raises(ValueError, match="unique"):
		CoverageResult(recommendation_request.coverage, (assessment, assessment))
	with pytest.raises(ValueError, match="outside"):
		CoverageResult(recommendation_request.coverage, (replace(assessment, session=recommendation_request.timing.exit_session),))
	with pytest.raises(ValueError, match="future daily close"):
		CoverageResult(recommendation_request.coverage, (replace(assessment, session=recommendation_request.timing.entry_session),))


def test_failures_propagate_stage_and_code() -> None:
	failure = StageFailure(WorkflowStage.REFRESH, FailureCode.PROVIDER, "Fixture provider failed", retryable=True)
	with pytest.raises(StageExecutionError, match="refresh: provider") as raised:
		raise StageExecutionError(failure)
	assert raised.value.failure is failure
	with pytest.raises(ValueError):
		replace(failure, stage="unknown")
