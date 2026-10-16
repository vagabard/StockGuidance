from dataclasses import replace

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.config.validation import require_aware_datetime
from stockguidance.portfolio.ledger import release_proceeds
from stockguidance.portfolio.marks import ClosingMark
from stockguidance.portfolio.planning import OrderPlan, plan_orders
from stockguidance.portfolio.schedule import buy_decision_index, scheduled_allocation
from stockguidance.portfolio.state import initial_portfolio
from stockguidance.portfolio.valuation import value_portfolio
from stockguidance.simulation.benchmarks import BenchmarkEntry, BenchmarkResult, benchmark_entry, evaluate_benchmark
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.simulation.events import EventKind, SimulationEvent
from stockguidance.simulation.execution import RealizedTrade, fill_orders, sell_positions
from stockguidance.simulation.prices import ExecutionPriceSource
from stockguidance.simulation.result import SimulationResult
from stockguidance.simulation.score_source import DecisionUnavailable, ScoreSource


def simulate(request: SimulationRequest, boundary: HistoryBoundary, calendar: SessionCalendar, scores: ScoreSource, prices: ExecutionPriceSource) -> SimulationResult:
	boundary.validate_for(request.source, calendar)
	if request.start < boundary.history_start or request.allocation.anchor_session != calendar.session(request.allocation.anchor_session.session_date) or prices.source != request.source:
		raise ValueError("Simulation history, allocation anchor or execution source differs")
	sessions = calendar.sessions(request.start, request.end)
	if not sessions or sessions[0].session_date != request.start or sessions[-1].session_date != request.end:
		raise ValueError("Simulation bounds must identify exchange sessions")
	require_aware_datetime(prices.audit_cutoff, "execution audit cutoff")
	if prices.audit_cutoff < sessions[-1].closes_at:
		raise ValueError("Execution audit cutoff must include the evaluation end close")
	decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - request.decision_lead) for session in sessions)
	if any(timing.decision_cutoff < timing.entry_session.opens_at for timing in decisions):
		raise ValueError("Simulation decisions must follow each actual session open, including early closes")
	scores.validate(request, decisions[0], calendar)
	initial = initial_portfolio(request.allocation, request.initial_capital, decisions[0].previous_session.closes_at)
	state = initial
	events, plans, forecasts, valuations, frames = [], [], [], [], []
	trades: list[RealizedTrade] = []
	benchmarks: list[BenchmarkResult] = []
	pending: list[BenchmarkEntry] = []
	last_quotes: dict[int, ClosingMark] = {}
	for timing in decisions:
		session = timing.entry_session
		state = release_proceeds(state, session, session.opens_at)
		events.append(SimulationEvent(EventKind.AVAILABILITY, session.opens_at, session, "Only this allocation's eligible prior-sale proceeds become available", portfolio=state))
		state = replace(state, as_of=timing.decision_cutoff)
		plan: OrderPlan | None = None
		forecast = None
		if scheduled_allocation(request.allocation, session) is not None:
			events.append(SimulationEvent(EventKind.DECISION, timing.decision_cutoff, session, "Anchored scheduled buy decision", portfolio=state))
			try:
				forecast = scores.forecast(request, timing, buy_decision_index(request.allocation, session))
				if forecast.sample.timing != timing or forecast.sample.features.specification != request.features or forecast.sample.target_specification != request.target or forecast.sample.source != request.source or forecast.sample.calendar_version != calendar.version:
					raise ValueError("Score source changed decision/model specifications")
				forecasts.append(forecast)
				if forecast.training is not None:
					events.append(SimulationEvent(EventKind.TRAINING, timing.decision_cutoff, session, "Chronological fit uses only outcomes mature by this decision cutoff"))
				plan = plan_orders(state, request.allocation, timing, forecast.prediction.ranking, request.execution)
				plans.append(plan)
				for order in plan.orders:
					events.append(SimulationEvent(EventKind.ORDER, timing.decision_cutoff, session, "Fixed maximum debit includes fee; quantity is determined only at the closing fill", order.allocation, order.ticker_id, order.maximum_debit))
				if not plan.orders:
					events.append(SimulationEvent(EventKind.SKIP, timing.decision_cutoff, session, plan.state.value, plan.allocation, portfolio=state))
			except DecisionUnavailable as error:
				events.append(SimulationEvent(EventKind.SKIP, timing.decision_cutoff, session, str(error), portfolio=state))
			except Exception:
				events.append(SimulationEvent(EventKind.FAILURE, timing.decision_cutoff, session, "Required historical training, prediction or planning failed", portfolio=state))
		try:
			frame = prices.read(session, request.features.ticker_ids)
			if frame.source != request.source or frame.session != session or frame.ticker_ids != request.features.ticker_ids:
				raise ValueError("Execution source changed its exact request")
			frames.append(frame)
		except Exception:
			events.append(SimulationEvent(EventKind.FAILURE, session.closes_at, session, "Required historical execution evidence failed", portfolio=state))
			break
		state, sale_events, realized = sell_positions(state, frame, calendar, request.execution, request.target)
		events.extend(sale_events)
		trades.extend(realized)
		if plan is not None:
			state, buy_events = fill_orders(state, plan, frame)
			events.extend(buy_events)
		last_quotes.update((item.ticker_id, item) for item in frame.quotes)
		benchmarks.extend(evaluate_benchmark(item, frame, request.target) for item in pending)
		pending = [] if forecast is None else [benchmark_entry(forecast, frame)]
		valuation = value_portfolio(state, session, last_quotes)
		valuations.append(valuation)
		events.append(SimulationEvent(EventKind.MARK, session.closes_at, session, "Cash plus resting proceeds plus marked positions; stale marks remain explicit", amount=valuation.total_equity, portfolio=state))
	benchmarks.extend(evaluate_benchmark(item, None, request.target) for item in pending)
	return SimulationResult(request, scores.policy, calendar.version, initial, state, tuple(events), tuple(plans), tuple(forecasts), tuple(valuations), tuple(trades), tuple(benchmarks), scores.training_runs, tuple(frames), prices.audit_cutoff)
