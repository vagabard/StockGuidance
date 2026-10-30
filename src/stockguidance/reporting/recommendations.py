from dataclasses import asdict

from stockguidance.portfolio.serialization import portfolio_payload
from stockguidance.reporting.orders import order_plan_payload
from stockguidance.reporting.training import training_payload
from stockguidance.recommendations.result import RankedTicker
from stockguidance.workflows.recommendation_result import RecommendationRunResult


def recommendation_payload(run: RecommendationRunResult) -> dict[str, object]:
	result = run.recommendation
	request, timing = result.request, result.request.timing
	versioned = request.target.version != "raw_return-v1" or any(item.symbol is not None for item in run.scores)
	def ranked(item: RankedTicker) -> dict[str, object]:
		payload: dict[str, object] = {"ticker_id": item.ticker_id, "score": item.score}
		if versioned:
			payload["symbol"] = item.symbol
		return payload
	symbols = {item.ticker_id: item.symbol for item in run.scores}
	return {
		"schema": "stockguidance-recommendation-2" if versioned else "stockguidance-recommendation-1",
		"successful": True, "kind": "ranked_suggestions" if run.order_plan is None else "portfolio_order_plan", "orders_planned": run.order_plan is not None and bool(run.order_plan.orders), "fills_recorded": False,
		"cash_rest_enforced": run.order_plan is not None, "planning_status": "requires_supplied_portfolio_state" if run.order_plan is None else run.order_plan.state.value,
		"supplied_portfolio": None if run.portfolio is None else portfolio_payload(run.portfolio),
		"order_plan": None if run.order_plan is None else order_plan_payload(run.order_plan),
		"model_policy": request.model.policy.value, "artifact_id": result.artifact_id,
		"availability_policy": request.availability_policy.value,
		"availability_assumption": request.availability_policy.assumption,
		"point_in_time_replay": not request.availability_policy.retrospective,
		"members": [{"artifact_id": identity, "weight": weight} for identity, weight in zip(run.member_artifact_ids, run.member_weights)],
		"source": asdict(request.source.identity), "data_revision": result.data_revision,
		"refresh_mode": request.refresh_mode.value,
		"score_units": result.score_units, "top_k": request.top_k, "eligible_count": result.eligible_count,
		"median_eligible_score": result.median_eligible_score, "median_returned_score": result.median_returned_score,
		"refresh_revision": run.refresh.data_revision, "feature_revision": run.sample.feature_revision,
		"feature_identity": run.sample.feature_cache_key,
		"coverage": {"start": request.coverage.start.isoformat(), "end": request.coverage.end.isoformat(), "as_of": request.coverage.as_of.isoformat(), "refreshed": run.refresh.successful, "validated": run.validation.complete},
		"calendar": {"id": timing.entry_session.calendar_id, "version": run.sample.calendar_version},
		"previous_session": timing.previous_session.session_date.isoformat(),
		"entry_session": timing.entry_session.session_date.isoformat(), "exit_session": timing.exit_session.session_date.isoformat(),
		"decision_cutoff": timing.decision_cutoff.isoformat(), "entry_close": timing.entry_time.isoformat(), "exit_close": timing.exit_time.isoformat(),
		"execution_assumption": "pre_close_decision_same_close_fill",
		"features": {**asdict(request.features), "version": request.features.version, "layout": request.features.layout},
		"target": {**asdict(request.target), "formula": request.target.raw_formula},
		"allocation": {"policy": request.allocation.policy.value, "anchor_session": request.allocation.anchor_session.session_date.isoformat(), "cash_rest": request.allocation.cash_rest.value},
		"eligibility_policy": {**asdict(request.eligibility), "observation_session": "previous_completed_session", "missing_policy": "fail_required_readiness"},
		"eligibility": [{"ticker_id": item.ticker_id, "symbol": symbols.get(item.ticker_id) if versioned else None, "state": item.state.value, "detail": item.detail, "session": None if item.observation is None else item.observation.timing.session.session_date.isoformat(), "available_at": None if item.observation is None else item.observation.timing.available_at.isoformat(), "transactions": None if item.observation is None else item.observation.transactions} for item in run.eligibility],
		"scores": [ranked(item) for item in run.scores], "ranking": [ranked(item) for item in result.ranking],
		"training": None if run.training is None else training_payload(run.training),
	}


def render_recommendations(run: RecommendationRunResult) -> str:
	result, timing = run.recommendation, run.recommendation.request.timing
	status = "Cash-rest order planning requires supplied portfolio state; no fills are recorded." if run.order_plan is None else f"Cash-rest order plan: {run.order_plan.state.value}; {len(run.order_plan.orders)} orders; no fills are recorded."
	lines = [f"Ranked suggestions: entry close {timing.entry_session.session_date}; exit close {timing.exit_session.session_date}", f"Decision cutoff: {timing.decision_cutoff.isoformat()}", f"Artifact: {result.artifact_id}", f"Source revision: {result.data_revision}", f"Allocation policy: {result.request.allocation.policy.value}", status]
	lines.extend(f"#{rank}: {item.symbol + ' ' if item.symbol is not None else ''}ticker_id={item.ticker_id} score={item.score:.8f} {result.score_units}" for rank, item in enumerate(result.ranking, start=1))
	if not result.ranking:
		lines.append("No eligible tickers; recommendation is valid but empty.")
	if run.order_plan is not None:
		lines.extend(f"Plan {item.allocation}: ticker_id={item.ticker_id} maximum_debit={item.maximum_debit}" for item in run.order_plan.orders)
	return "\n".join(lines)
