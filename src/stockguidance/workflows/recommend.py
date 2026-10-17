from dataclasses import replace

from stockguidance.artifacts.ensembles import LoadedEnsemble
from stockguidance.artifacts.loading import LoadedModel
from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.closes import CloseQuery
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.datasets.building import build_sample
from stockguidance.features.specification import CloseFeature, SignalMode
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.identity import content_id
from stockguidance.portfolio.planning import plan_orders
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.state import PortfolioState
from stockguidance.recommendations.artifacts import load_recommendation_artifact, require_compatible_artifact
from stockguidance.recommendations.configuration import ArtifactKind, RecommendationJob
from stockguidance.recommendations.eligibility import EligibilityState, assess_eligibility, eligibility_revision
from stockguidance.recommendations.preparation import prepare_request
from stockguidance.recommendations.scoring import predict_ranking
from stockguidance.recommendations.result import RecommendationResult
from stockguidance.workflows.failures import FailureCode, StageExecutionError, StageFailure, WorkflowStage
from stockguidance.workflows.recommendation_dependencies import HistoryTrainer, RecommendationServices
from stockguidance.workflows.recommendation_result import RecommendationRunResult
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history
from stockguidance.workflows.update_market_data import UpdateSettings, update_market_data
from stockguidance.workflows.validate_database import ValidationSettings, validate_database


def _failure(stage: WorkflowStage, code: FailureCode, message: str) -> StageExecutionError:
	return StageExecutionError(StageFailure(stage, code, message))


def recommend(job: RecommendationJob, boundary: HistoryBoundary, calendar: SessionCalendar, services: RecommendationServices, clock: Clock, *, trainer: HistoryTrainer | None = None, portfolio: PortfolioState | None = None, execution: ExecutionPolicy = ExecutionPolicy()) -> RecommendationRunResult:
	try:
		request = prepare_request(job, boundary, calendar, clock)
		if portfolio is not None:
			portfolio.validate_calendar(calendar)
			plan_orders(portfolio, job.allocation, request.timing, (), execution)
	except Exception:
		raise _failure(WorkflowStage.REQUEST, FailureCode.INVALID_REQUEST, "Recommendation timing, coverage or training specification is invalid") from None
	artifact: LoadedModel | LoadedEnsemble | None = None
	if job.current_artifact is not None:
		try:
			artifact = load_recommendation_artifact(job.current_artifact, job.artifact_kind)
			require_compatible_artifact(artifact, job, request.timing, calendar)
		except Exception:
			raise _failure(WorkflowStage.ARTIFACT, FailureCode.INCOMPATIBLE_ARTIFACT, "Explicit current artifact is unavailable or incompatible; legacy models require rebuilding") from None
	try:
		refresh = update_market_data(job.source, UpdateSettings(job.start, job.end, job.publication, job.corrections), boundary, calendar, services.provider, services.market_data, clock)
		if not refresh.successful:
			raise ValueError("Incomplete refresh")
		if job.features.signal_mode is SignalMode.ENTRY_OPEN:
			if services.openings is None:
				raise ValueError("Entry opening capture is not configured")
			opening_request = prepare_request(job, boundary, calendar, clock)
			services.openings.refresh(job.features, opening_request.timing, boundary, clock)
	except Exception:
		raise _failure(WorkflowStage.REFRESH, FailureCode.NOT_READY, "Required source reconciliation failed or coverage remains incomplete") from None
	try:
		request = prepare_request(job, boundary, calendar, clock)
		close_features = replace(job.features, signal_mode=SignalMode.PREVIOUS_CLOSE, feature_order=tuple(feature for feature in job.features.feature_order if feature is not CloseFeature.ENTRY_OPEN_RETURN))
		validation = validate_database(job.source, ValidationSettings(job.start, job.end, job.publication, request.timing.decision_cutoff, close_features, job.target), boundary, calendar, services.validation, clock)
		if not validation.complete:
			raise ValueError("Incomplete validation")
		sample = build_sample(job.features, job.target, request.timing, boundary, calendar, job.publication, services.closes, availability_policy=job.availability_policy)
		if sample.features.values is None:
			raise ValueError("Unavailable inference sample")
		query = CloseQuery(SessionQuery(boundary, (request.timing.previous_session,), request.timing.decision_cutoff), job.features.ticker_ids)
		eligibility = assess_eligibility(job.features.ticker_ids, job.eligibility, request.timing, services.eligibility.read(query), job.availability_policy, job.publication)
		if any(item.state is EligibilityState.UNAVAILABLE for item in eligibility):
			raise _failure(WorkflowStage.VALIDATION, FailureCode.NOT_READY, "Required previous-session transaction counts are unavailable by the decision cutoff")
		if not any(item.state is EligibilityState.ELIGIBLE for item in eligibility):
			raise _failure(WorkflowStage.VALIDATION, FailureCode.NOT_READY, "No ticker satisfies the required previous-session transaction minimum")
	except StageExecutionError:
		raise
	except Exception:
		raise _failure(WorkflowStage.VALIDATION, FailureCode.NOT_READY, "Required coverage, previous-close features or eligibility could not be verified") from None
	training = None
	if job.retraining is not None:
		try:
			plan = job.retraining
			training_request = HistoryTrainingRequest(plan.decisions, request.timing.decision_cutoff, job.features, job.target, plan.configuration, plan.artifact_path, availability_policy=job.availability_policy)
			training = (trainer or train_from_history)(training_request, boundary, calendar, job.publication, services.closes)
		except Exception:
			raise _failure(WorkflowStage.TRAINING, FailureCode.TRAINING_FAILED, "Required mature-outcome training or artifact export failed") from None
		try:
			artifact = load_recommendation_artifact(training.artifact, ArtifactKind.MODEL)
			require_compatible_artifact(artifact, job, request.timing, calendar)
		except Exception:
			raise _failure(WorkflowStage.ARTIFACT, FailureCode.INCOMPATIBLE_ARTIFACT, "Training returned an unavailable or incompatible artifact") from None
	try:
		if artifact is None:
			raise ValueError("No explicit artifact")
		prediction = predict_ranking(artifact, sample, eligibility, job.top_k)
		scores, ranking = prediction.scores, prediction.ranking
		if not ranking:
			raise _failure(WorkflowStage.PREDICTION, FailureCode.NOT_READY, "No ticker satisfies the required previous-session transaction minimum")
		if decision_cutoff(clock) >= request.timing.entry_time:
			raise _failure(WorkflowStage.PREDICTION, FailureCode.NOT_READY, "Entry close passed before recommendations were ready")
		revision = content_id({"refresh": refresh.data_revision, "features": sample.feature_revision, "eligibility": eligibility_revision(eligibility)})
		result = RecommendationResult(request, artifact.artifact_id, revision, ranking)
	except StageExecutionError:
		raise
	except Exception:
		raise _failure(WorkflowStage.PREDICTION, FailureCode.PREDICTION_FAILED, "Required prediction or ranking failed") from None
	try:
		order_plan = None if portfolio is None else plan_orders(portfolio, job.allocation, request.timing, ranking, execution)
		if order_plan is not None and decision_cutoff(clock) >= request.timing.entry_time:
			raise _failure(WorkflowStage.PLANNING, FailureCode.NOT_READY, "Entry close passed before portfolio order plans were ready")
	except StageExecutionError:
		raise
	except Exception:
		raise _failure(WorkflowStage.PLANNING, FailureCode.INVALID_REQUEST, "Supplied portfolio state cannot support a valid cash-rest order plan") from None
	return RecommendationRunResult(result, scores, eligibility, sample, refresh, validation, prediction.member_artifact_ids, prediction.member_weights, training, order_plan, portfolio)
