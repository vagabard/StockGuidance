from dataclasses import replace
from typing import Any

from stockguidance.artifacts.ensembles import LoadedEnsemble
from stockguidance.artifacts.loading import LoadedModel
from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.closes import CloseQuery
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.datasets.building import build_sample
from stockguidance.features.specification import CloseFeature, FeatureSpec, FeatureSpecV2, SignalMode
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.portfolio.planning import plan_orders
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.state import PortfolioState
from stockguidance.recommendations.artifacts import load_recommendation_artifact, require_compatible_artifact
from stockguidance.recommendations.configuration import ArtifactKind, RecommendationJob
from stockguidance.recommendations.eligibility import EligibilityState, assess_eligibility, eligibility_revision
from stockguidance.recommendations.preparation import prepare_request
from stockguidance.recommendations.scoring import predict_ranking, recommendation_statistics
from stockguidance.recommendations.result import RecommendationResult
from stockguidance.recommendations.result import RankedTicker
from stockguidance.workflows.failures import FailureCode, StageExecutionError, StageFailure, WorkflowStage
from stockguidance.workflows.recommendation_dependencies import HistoryTrainer, RecommendationServices
from stockguidance.workflows.recommendation_result import RecommendationRunResult, ValidationOnlyRefresh
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history
from stockguidance.workflows.update_market_data import UpdateSettings, update_market_data
from stockguidance.workflows.validate_database import ValidationSettings, validate_database
from stockguidance.workflows.update_result import UpdateResult
from stockguidance.database.simplified_runtime import SimplifiedUpdateResult, SimplifiedValidationResult
from stockguidance.validation.result import ValidationResult
from stockguidance.runs.contracts import RunConfig, RunOperation
from stockguidance.runs.service import RunDirectoryService, RunHandle
from stockguidance.recommendations.configuration import RefreshMode


def _failure(stage: WorkflowStage, code: FailureCode, message: str) -> StageExecutionError:
	return StageExecutionError(StageFailure(stage, code, message))


def _recommend(job: RecommendationJob, boundary: HistoryBoundary, calendar: SessionCalendar, services: RecommendationServices, clock: Clock, *, trainer: HistoryTrainer | None = None, portfolio: PortfolioState | None = None, execution: ExecutionPolicy = ExecutionPolicy()) -> RecommendationRunResult:
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
	refresh: UpdateResult | SimplifiedUpdateResult | ValidationOnlyRefresh
	validation: ValidationResult | SimplifiedValidationResult
	try:
		if job.refresh_mode is RefreshMode.VALIDATE_ONLY:
			if services.simplified_readiness is None:
				if services.validation is None:
					raise ValueError("Validate-only recommendations require a validation repository")
				validation = validate_database(job.source, ValidationSettings(job.start, job.end, job.publication, request.timing.decision_cutoff, job.features if not isinstance(job.features, FeatureSpecV2) else None, job.target), boundary, calendar, services.validation, clock)
				refresh = ValidationOnlyRefresh(content_id({"mode": "validate-only", "coverage": validation.plan.request.start.isoformat(), "end": validation.plan.request.end.isoformat(), "findings": [str(item) for item in validation.findings]}), validation.complete)
			else:
				readiness_decisions = (request.timing,) if job.retraining is None else tuple((*job.retraining.decisions, request.timing))
				readiness = services.simplified_readiness.validate_only(
					readiness_decisions,
					job.features,
					job.target,
					label_cutoff=None if job.retraining is None else request.timing.decision_cutoff,
					availability_policy=job.availability_policy,
				)
				refresh, validation = readiness.update, readiness.validation
		elif services.simplified_readiness is None:
			if services.market_data is None:
				raise ValueError("Legacy recommendation services lack a market-data repository")
			refresh = update_market_data(job.source, UpdateSettings(job.start, job.end, job.publication, job.corrections), boundary, calendar, services.provider, services.market_data, clock)
		else:
			readiness_decisions = (request.timing,) if job.retraining is None else tuple((*job.retraining.decisions, request.timing))
			readiness = services.simplified_readiness.ensure(
				readiness_decisions,
				job.features,
				job.target,
				label_cutoff=None if job.retraining is None else request.timing.decision_cutoff,
				availability_policy=job.availability_policy,
			)
			refresh, validation = readiness.update, readiness.validation
		if not refresh.successful:
			raise ValueError("Incomplete refresh")
		if job.features.signal_mode is SignalMode.ENTRY_OPEN:
			if not isinstance(job.features, FeatureSpec):
				raise ValueError("Entry-open recommendations require the legacy opening feature contract")
			if services.openings is None:
				raise ValueError("Entry opening capture is not configured")
			opening_request = prepare_request(job, boundary, calendar, clock)
			services.openings.refresh(job.features, opening_request.timing, boundary, clock)
	except Exception:
		raise _failure(WorkflowStage.REFRESH, FailureCode.NOT_READY, "Required source reconciliation failed or coverage remains incomplete") from None
	try:
		request = prepare_request(job, boundary, calendar, clock)
		if services.simplified_readiness is None:
			if services.validation is None:
				raise ValueError("Legacy recommendation services lack a validation repository")
			if not isinstance(job.features, FeatureSpec):
				raise ValueError("Configured feature channels require the versioned database readiness workflow")
			close_features = replace(job.features, signal_mode=SignalMode.PREVIOUS_CLOSE, feature_order=tuple(feature for feature in job.features.feature_order if feature is not CloseFeature.ENTRY_OPEN_RETURN))
			validation = validate_database(job.source, ValidationSettings(job.start, job.end, job.publication, request.timing.decision_cutoff, close_features, job.target), boundary, calendar, services.validation, clock)
			if not validation.complete:
				raise ValueError("Incomplete validation")
		elif not getattr(validation, "complete", False):
			raise ValueError("Incomplete target-schema readiness")
		sample = build_sample(job.features, job.target, request.timing, boundary, calendar, job.publication, services.closes, availability_policy=job.availability_policy, channel_repository=services.channels)
		if sample.features.values is None:
			raise ValueError("Unavailable inference sample")
		query = CloseQuery(SessionQuery(boundary, (request.timing.previous_session,), request.timing.decision_cutoff), job.features.ticker_ids)
		eligibility = assess_eligibility(job.features.ticker_ids, job.eligibility, request.timing, services.eligibility.read(query), job.availability_policy, job.publication)
		if any(item.state is EligibilityState.UNAVAILABLE for item in eligibility):
			raise _failure(WorkflowStage.VALIDATION, FailureCode.NOT_READY, "Required previous-session transaction counts are unavailable by the decision cutoff")
		if not any(item.state is EligibilityState.ELIGIBLE for item in eligibility) and not job.permits_empty_selection:
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
		if not ranking and not job.permits_empty_selection:
			raise _failure(WorkflowStage.PREDICTION, FailureCode.NOT_READY, "No ticker satisfies the required previous-session transaction minimum")
		if decision_cutoff(clock) >= request.timing.entry_time:
			raise _failure(WorkflowStage.PREDICTION, FailureCode.NOT_READY, "Entry close passed before recommendations were ready")
		revision = content_id({"refresh": refresh.data_revision, "features": sample.feature_revision, "eligibility": eligibility_revision(eligibility)})
		eligible_count, median_eligible, median_returned = recommendation_statistics(scores, eligibility, ranking)
		result = RecommendationResult(request, artifact.artifact_id, revision, ranking, job.target.score_units, eligible_count, median_eligible, median_returned)
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


def _ranked_payload(item: RankedTicker, *, versioned: bool) -> dict[str, Any]:
	payload: dict[str, Any] = {"ticker_id": item.ticker_id, "score": item.score}
	if versioned:
		payload["symbol"] = item.symbol
	return payload


def _feature_report_mapping(features: FeatureSpec | FeatureSpecV2) -> dict[str, object]:
	if isinstance(features, FeatureSpecV2):
		return features.to_mapping()
	return {
		"ticker_ids": list(features.ticker_ids),
		"lookback_sessions": features.lookback_sessions,
		"signal_mode": features.signal_mode.value,
		"interval": features.interval.value,
		"missing_history": features.missing_history.value,
		"feature_order": [item.value for item in features.feature_order],
		"adjustment": features.adjustment.value,
		"version": features.version,
		"layout": features.layout,
	}


def _recommendation_report(run: RecommendationRunResult) -> dict[str, Any]:
	result = run.recommendation
	request = result.request
	versioned = request.target.version != "raw_return-v1" or any(item.symbol is not None for item in run.scores)
	symbols = {item.ticker_id: item.symbol for item in run.scores}
	return {
		"schema": "stockguidance-recommendation-2" if versioned else "stockguidance-recommendation-1", "successful": True,
		"artifact_id": result.artifact_id,
		"source": {"provider": request.source.identity.provider, "endpoint": request.source.identity.endpoint, "market": request.source.identity.market, "universe": request.source.identity.universe, "adjustment": request.source.identity.adjustment.value, "interval": request.source.identity.interval.value},
		"data_revision": result.data_revision,
		"refresh_mode": request.refresh_mode.value,
		"score_units": result.score_units,
		"top_k": request.top_k,
		"eligible_count": result.eligible_count,
		"median_eligible_score": result.median_eligible_score,
		"median_returned_score": result.median_returned_score,
		"scores": [_ranked_payload(item, versioned=versioned) for item in run.scores],
		"eligibility": [{"ticker_id": item.ticker_id, "symbol": symbols.get(item.ticker_id), "state": item.state.value, "detail": item.detail, "session": None if item.observation is None else item.observation.timing.session.session_date.isoformat(), "available_at": None if item.observation is None else item.observation.timing.available_at.isoformat(), "transactions": None if item.observation is None else item.observation.transactions} for item in run.eligibility],
		"ranking": [_ranked_payload(item, versioned=versioned) for item in result.ranking],
		"timing": {"previous_session": request.timing.previous_session.session_date.isoformat(), "entry_session": request.timing.entry_session.session_date.isoformat(), "exit_session": request.timing.exit_session.session_date.isoformat(), "decision_cutoff": request.timing.decision_cutoff.isoformat(), "entry_close": request.timing.entry_time.isoformat()},
		"features": _feature_report_mapping(request.features),
		"target": request.target.to_mapping(),
		"availability_policy": request.availability_policy.value,
	}


def _persist_recommendation_run(service: RunDirectoryService, handle: RunHandle, job: RecommendationJob, result: RecommendationRunResult) -> None:
	from stockguidance.recommendations.artifacts import load_recommendation_artifact

	report = _recommendation_report(result)
	metadata = None
	artifact_reference = job.current_artifact
	if artifact_reference is None and result.training is not None:
		artifact_reference = result.training.artifact
	if artifact_reference is not None:
		loaded = load_recommendation_artifact(artifact_reference, job.artifact_kind if job.current_artifact is not None else ArtifactKind.MODEL)
		metadata = loaded.metadata if isinstance(loaded, LoadedModel) else loaded.members[0].metadata
		service.register_external(handle.run_id, name="model", kind="artifact", sha256=artifact_reference.artifact_id, external_reference=str(artifact_reference.path))
	universe = None if metadata is None or metadata.universe is None else metadata.universe.to_mapping()
	feature_mapping = _feature_report_mapping(result.recommendation.request.features)
	specifications: dict[str, object] = {"features": feature_mapping, "target": result.recommendation.request.target.to_mapping(), "model": None if metadata is None else metadata.model.to_mapping(), "loss": None if metadata is None or metadata.loss is None else metadata.loss.to_mapping(), "preprocessing": None if metadata is None or metadata.preprocessing is None else metadata.preprocessing.to_mapping()}
	results = {
		"predictions": {"schema": "recommendation-scores-1", "scope": "ordered_model_universe", "rows": report["scores"], "score_units": result.recommendation.score_units},
		"eligibility": report["eligibility"],
		"exclusions": [item for item in report["eligibility"] if item["state"] != "eligible"],
		"metrics": {"score_units": result.recommendation.score_units, "eligible_count": result.recommendation.eligible_count, "median_eligible_score": result.recommendation.median_eligible_score, "median_returned_score": result.recommendation.median_returned_score, "returned_count": len(result.recommendation.ranking)},
		"reports": {"path": "reports/recommendation.json", "schema": report["schema"]},
	}
	atomic_write(handle.reports_directory / "recommendation.json", encode_json(report))
	service.update_sections(
		handle.run_id,
		resolved_configuration={"timing": report["timing"], "data_revision": result.recommendation.data_revision, "refresh_mode": job.refresh_mode.value},
		sections={
			"identity": {"source": report["source"], "artifact_id": result.recommendation.artifact_id, "data_revision": result.recommendation.data_revision},
			"resolution": report["timing"],
			"universe": universe or {"ticker_ids": list(result.recommendation.request.features.ticker_ids), "ticker_labels": [item.symbol for item in result.scores]},
			"specifications": specifications,
			"provenance": {"refresh_mode": job.refresh_mode.value, "decision_cutoff": result.recommendation.request.timing.decision_cutoff.isoformat(), "feature_revision": result.sample.feature_revision, "refresh_revision": result.refresh.data_revision},
			"results": results,
		},
	)


def _recommendation_run_settings(job: RecommendationJob, execution: ExecutionPolicy) -> dict[str, object]:
	features = job.features.to_mapping() if hasattr(job.features, "to_mapping") else {"ticker_ids": list(job.features.ticker_ids), "lookback_sessions": job.features.lookback_sessions, "feature_order": [item.value for item in job.features.feature_order]}
	return {
		"source": {"provider": job.source.identity.provider, "endpoint": job.source.identity.endpoint, "market": job.source.identity.market, "universe": job.source.identity.universe, "adjustment": job.source.identity.adjustment.value, "interval": job.source.identity.interval.value},
		"coverage": {"start": job.start.isoformat(), "end": job.end.isoformat(), "entry": job.entry_date.isoformat()},
		"features": features,
		"target": job.target.to_mapping(),
		"model": {"policy": job.model.policy.value, "artifact_id": job.model.artifact_id},
		"allocation": {"policy": job.allocation.policy.value, "anchor": job.allocation.anchor_session.session_date.isoformat()},
		"eligibility": {"minimum_transactions": job.eligibility.minimum_transactions},
		"top_k": job.top_k,
		"refresh_mode": job.refresh_mode.value,
		"publication": {"delay_seconds": None if job.publication.after_close is None else job.publication.after_close.total_seconds(), "provenance": job.publication.provenance},
		"corrections": {"recent_window_seconds": job.corrections.recent_window.total_seconds(), "recent_recheck_seconds": job.corrections.recent_recheck.total_seconds(), "archive_recheck_seconds": job.corrections.archive_recheck.total_seconds(), "provenance": job.corrections.provenance},
		"execution": {"fee_per_fill": str(execution.fee_per_fill)},
		"availability_policy": job.availability_policy.value,
	}


def recommend(job: RecommendationJob, boundary: HistoryBoundary, calendar: SessionCalendar, services: RecommendationServices, clock: Clock, *, trainer: HistoryTrainer | None = None, portfolio: PortfolioState | None = None, execution: ExecutionPolicy = ExecutionPolicy(), runs: RunDirectoryService | None = None) -> RecommendationRunResult:
	"""Run one recommendation attempt and persist its outcome under ``runs``."""

	service = runs or RunDirectoryService()
	handle = service.create(RunConfig(RunOperation.RECOMMENDATION, _recommendation_run_settings(job, execution)))
	service.start(handle.run_id)
	try:
		result = _recommend(job, boundary, calendar, services, clock, trainer=trainer, portfolio=portfolio, execution=execution)
		_persist_recommendation_run(service, handle, job, result)
		service.complete(handle.run_id)
		return result
	except Exception as error:
		try:
			stage = error.failure.stage.value if isinstance(error, StageExecutionError) else "recommendation"
			code = error.failure.code.value if isinstance(error, StageExecutionError) else type(error).__name__
			service.fail(handle.run_id, stage=stage, code=code, message=f"Recommendation failed during {stage}")
		except Exception:
			pass
		raise
