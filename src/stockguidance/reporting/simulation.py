from dataclasses import asdict
from decimal import Decimal

from stockguidance.portfolio.serialization import portfolio_payload, position_payload
from stockguidance.portfolio.valuation import PortfolioValue
from stockguidance.market_data.identity import content_id
from stockguidance.recommendations.eligibility import TickerEligibility
from stockguidance.reporting.orders import order_plan_payload
from stockguidance.reporting.training import training_payload
from stockguidance.simulation.events import SimulationEvent
from stockguidance.simulation.result import SimulationResult


def valuation_payload(value: PortfolioValue) -> dict[str, object]:
	return {"session": value.session.session_date.isoformat(), "total_equity": str(value.total_equity), "marks_current": value.marks_current, "allocations": [{name: str(amount) if isinstance(amount, Decimal) else amount for name, amount in asdict(item).items()} for item in value.allocations], "positions": [{"allocation": item.allocation, **position_payload(item.position), "marked_price": str(item.price), "price_session": item.price_session.session_date.isoformat(), "marked_value": str(item.value), "mark_current": item.current} for item in value.positions]}


def event_payload(event: SimulationEvent) -> dict[str, object]:
	return {"kind": event.kind.value, "at": event.at.isoformat(), "session": event.session.session_date.isoformat(), "detail": event.detail, "allocation": event.allocation, "ticker_id": event.ticker_id, "amount": None if event.amount is None else str(event.amount), "quantity": None if event.quantity is None else str(event.quantity), "portfolio": None if event.portfolio is None else portfolio_payload(event.portfolio)}


def eligibility_payload(eligibility: TickerEligibility, *, detailed: bool) -> dict[str, object]:
	result: dict[str, object] = {"ticker_id": eligibility.ticker_id, "state": eligibility.state.value, "detail": eligibility.detail}
	observation = eligibility.observation
	if detailed and observation is not None:
		result.update({"transactions": observation.transactions, "observation_session": observation.timing.session.session_date.isoformat(), "observed_at": observation.timing.observed_at.isoformat(), "available_at": observation.timing.available_at.isoformat(), "revision": observation.revision})
	return result


def simulation_payload(result: SimulationResult, *, schema: str = "stockguidance-simulation-1") -> dict[str, object]:
	if schema not in ("stockguidance-simulation-1", "stockguidance-simulation-2"):
		raise ValueError("Unsupported simulation report schema")
	request = result.request
	final = None if not result.valuations else valuation_payload(result.valuations[-1])
	evidence = [{"session": frame.session.session_date.isoformat(), "quotes": [{"ticker_id": item.ticker_id, "price": str(item.price), "revision": item.revision} for item in frame.quotes], "issues": [{"ticker_id": item.ticker_id, "state": item.state.value} for item in frame.issues]} for frame in result.execution_frames]
	return {
		"schema": schema, "successful": result.successful, "complete": result.complete,
		"replay": {"saved_scores": True, "execution_quotes": True, "coverage": "full_model_universe"} if schema == "stockguidance-simulation-2" else None,
		"model_policy": result.score_policy.policy.value, "retrain_every_buy_decisions": result.score_policy.retrain_every_buy_decisions,
		"cadence_basis": "scheduled_buy_decisions_from_allocation_anchor_including_skips",
		"evaluation": "fixed_pre_period_model" if result.score_policy.retrain_every_buy_decisions is None else "chronological_retraining",
		"source": asdict(request.source), "features": {**asdict(request.features), "version": request.features.version, "layout": request.features.layout},
		"availability_policy": request.availability_policy.value, "availability_assumption": request.availability_policy.assumption, "point_in_time_replay": not request.availability_policy.retrospective, "data_snapshot": None if request.data_snapshot is None else request.data_snapshot.isoformat(),
		"research_disclaimer": None if not request.availability_policy.retrospective else "Retrospective research assumes historical publication timing; corrected/adjusted history and the selected universe may contain hindsight and do not establish an original point-in-time replay.",
		"target": {**asdict(request.target), "formula": request.target.raw_formula},
		"calendar": {"id": request.allocation.anchor_session.calendar_id, "version": result.calendar_version},
		"start_session": request.start.isoformat(), "end_session": request.end.isoformat(), "decision_lead_seconds": request.decision_lead.total_seconds(),
		"allocation": {"policy": request.allocation.policy.value, "anchor_session": request.allocation.anchor_session.session_date.isoformat(), "cash_rest": request.allocation.cash_rest.value, "rebalance": False},
		"execution": {"assumption": "pre_close_decision_same_close_fill", "fee_per_fill": str(request.execution.fee_per_fill), "currency_quantum": "0.01", "share_quantum": "0.00000001", "quantity_rounding": "down", "buy_cost_rounding": "ceiling", "sale_proceeds_rounding": "down", "mark_rounding": "half_even", "missing_entry": "unfilled_keep_reserved_cash", "missing_exit": "retain_position_retry_next_close", "end_positions": "retain_and_mark_without_forced_sale"},
		"eligibility": asdict(request.eligibility), "top_k": request.top_k,
		"initial_portfolio": portfolio_payload(result.initial), "final_portfolio": portfolio_payload(result.final), "final_valuation": final,
		"plans": [order_plan_payload(item) for item in result.plans],
		"decisions": [{"entry_session": item.sample.timing.entry_session.session_date.isoformat(), "decision_cutoff": item.sample.timing.decision_cutoff.isoformat(), "artifact_id": item.artifact_id, "members": [{"artifact_id": identity, "weight": weight} for identity, weight in zip(item.prediction.member_artifact_ids, item.prediction.member_weights)], "feature_identity": item.sample.feature_cache_key, "data_revision": item.prediction.data_revision, "scores": [asdict(score) for score in item.prediction.scores], "ranking": [asdict(score) for score in item.prediction.ranking], "eligibility": [eligibility_payload(eligibility, detailed=schema == "stockguidance-simulation-2") for eligibility in item.prediction.eligibility]} for item in result.forecasts],
		"training_runs": [{"decision_cutoff": timing.decision_cutoff.isoformat(), "entry_session": timing.entry_session.session_date.isoformat(), "result": training_payload(training)} for timing, training in result.training_runs],
		"events": [event_payload(item) for item in result.events], "equity": [valuation_payload(item) for item in result.valuations],
		"realized_trades": [{"allocation": item.allocation, **position_payload(item.position), "actual_exit_session": item.exit_session_date, "exit_price": str(item.exit_price), "net_proceeds": str(item.net_proceeds), "realized_pnl": str(item.realized_pnl), "net_return": str(item.net_return), "close_return": item.close_return, "actual_price_return": item.actual_price_return, "exited_on_time": item.exited_on_time} for item in result.trades],
		"benchmarks": [{"entry_session": item.entry.timing.entry_session.session_date.isoformat(), "exit_session": item.entry.timing.exit_session.session_date.isoformat(), "universe_at_decision": list(item.entry.eligible_tickers), "selected_at_decision": list(item.entry.selected_tickers), "close_returns": [{"ticker_id": ticker_id, "return": value} for ticker_id, value in item.returns], "equal_weight_return": item.equal_weight_return, "selected_return": item.selected_return, "selected_percentiles": [{"ticker_id": ticker_id, "percentile": percentile} for ticker_id, percentile in item.selected_percentiles], "complete": item.complete} for item in result.benchmarks],
		"benchmark_policy": "decision_eligible_universe_close_T_to_close_T_plus_1_strict_lower_percentiles_no_missing_outcome_redistribution",
		"prediction_cache": "disabled",
		"execution_audit_cutoff": result.execution_audit_cutoff.isoformat(), "execution_evidence": evidence,
		"execution_revision": content_id({"source": asdict(request.source), "audit_cutoff": result.execution_audit_cutoff.isoformat(), "frames": evidence}),
	}


def render_simulation(result: SimulationResult) -> str:
	lines = [f"Close-to-next-session-close simulation: {result.request.start} through {result.request.end}", f"Model policy: {result.score_policy.policy.value}; allocation: {result.request.allocation.policy.value}", f"Successful: {result.successful}; complete data/fills: {result.complete}"]
	if result.valuations:
		value = result.valuations[-1]
		lines.append(f"Marked equity: {value.total_equity}; current marks: {value.marks_current}")
		lines.extend(f"{item.name}: cash={item.available_cash}; resting={item.resting_proceeds}; positions={item.positions_value}; equity={item.equity}; realized_pnl={item.realized_pnl}" for item in value.allocations)
	lines.append(f"Realized trades: {len(result.trades)}; end positions: {sum(len(item.positions) for item in result.final.allocations)}")
	return "\n".join(lines)
