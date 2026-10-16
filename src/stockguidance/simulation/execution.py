from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_DOWN

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.portfolio.availability import SaleProceedsTiming
from stockguidance.portfolio.planning import OrderPlan
from stockguidance.portfolio.precision import ExecutionPolicy, money
from stockguidance.portfolio.state import PortfolioState, Position, RestingProceeds
from stockguidance.simulation.events import EventKind, SimulationEvent
from stockguidance.simulation.prices import ExecutionFrame
from stockguidance.targets.construction import close_to_close_return
from stockguidance.targets.specification import TargetSpec


@dataclass(frozen=True)
class RealizedTrade:
	allocation: str
	position: Position
	exit_session_date: str
	exit_price: Decimal
	net_proceeds: Decimal
	realized_pnl: Decimal
	net_return: Decimal
	close_return: float | None
	actual_price_return: float
	exited_on_time: bool


def sell_positions(state: PortfolioState, frame: ExecutionFrame, calendar: SessionCalendar, execution: ExecutionPolicy, target: TargetSpec) -> tuple[PortfolioState, tuple[SimulationEvent, ...], tuple[RealizedTrade, ...]]:
	if state.as_of > frame.session.closes_at:
		raise ValueError("Sale events cannot precede ledger state")
	quotes = {item.ticker_id: item for item in frame.quotes}
	events, trades = [], []
	state = replace(state, as_of=frame.session.closes_at)
	for allocation_index in range(len(state.allocations)):
		for position in state.allocations[allocation_index].positions:
			if position.exit_session.sequence > frame.session.sequence:
				continue
			allocation = state.allocations[allocation_index]
			quote = quotes.get(position.ticker_id)
			gross = Decimal(0) if quote is None else money(quote.price * position.quantity, rounding=ROUND_DOWN)
			if quote is None or gross < execution.fee_per_fill:
				events.append(SimulationEvent(EventKind.UNFILLED_EXIT, frame.session.closes_at, frame.session, "Exit close unavailable or proceeds cannot cover fee; position retained and exit retried next session", allocation.name, position.ticker_id, portfolio=state))
				continue
			net = gross - execution.fee_per_fill
			resting = RestingProceeds(net, SaleProceedsTiming(frame.session, calendar.shift(frame.session, 1)))
			updated = replace(allocation, positions=tuple(item for item in allocation.positions if item != position), resting=(*allocation.resting, resting), realized_pnl=allocation.realized_pnl + net - position.cost_basis, fees_paid=allocation.fees_paid + execution.fee_per_fill)
			state = replace(state, allocations=tuple(updated if index == allocation_index else item for index, item in enumerate(state.allocations)))
			actual_return = close_to_close_return(float(position.entry_price), float(quote.price), target)
			on_time = position.exit_session == frame.session
			trades.append(RealizedTrade(allocation.name, position, frame.session.session_date.isoformat(), quote.price, net, net - position.cost_basis, net / position.cost_basis - 1, actual_return if on_time else None, actual_return, on_time))
			events.append(SimulationEvent(EventKind.SALE, frame.session.closes_at, frame.session, "Sale proceeds rest until the next exchange session opens", allocation.name, position.ticker_id, net, position.quantity, state))
	return state, tuple(events), tuple(trades)


def fill_orders(state: PortfolioState, plan: OrderPlan, frame: ExecutionFrame) -> tuple[PortfolioState, tuple[SimulationEvent, ...]]:
	if frame.session != plan.timing.entry_session or state.allocation != plan.portfolio.allocation or state.as_of > frame.session.closes_at:
		raise ValueError("Order plan, execution session and ledger differ")
	quotes = {item.ticker_id: item for item in frame.quotes}
	events = []
	state = replace(state, as_of=frame.session.closes_at)
	for order in plan.orders:
		allocation_index = next(index for index, item in enumerate(state.allocations) if item.name == order.allocation)
		allocation = state.allocations[allocation_index]
		if order.maximum_debit > allocation.available_cash or any(item.ticker_id == order.ticker_id for item in allocation.positions):
			raise ValueError("Order cannot consume unavailable/resting cash or duplicate a position")
		quote = quotes.get(order.ticker_id)
		quantity, debit = (Decimal(0), Decimal(0)) if quote is None else plan.execution.size(order.maximum_debit, quote.price)
		if quote is None or quantity == 0:
			events.append(SimulationEvent(EventKind.UNFILLED_BUY, frame.session.closes_at, frame.session, "Entry close unavailable or budget too small; reserved capital remains cash without redistribution", allocation.name, order.ticker_id, order.maximum_debit, portfolio=state))
			continue
		position = Position(order.ticker_id, quantity, frame.session, plan.timing.exit_session, quote.price, debit)
		updated = replace(allocation, available_cash=allocation.available_cash - debit, positions=(*allocation.positions, position), fees_paid=allocation.fees_paid + plan.execution.fee_per_fill)
		state = replace(state, allocations=tuple(updated if index == allocation_index else item for index, item in enumerate(state.allocations)))
		events.append(SimulationEvent(EventKind.BUY, frame.session.closes_at, frame.session, "Fractional notional order filled at entry close", allocation.name, order.ticker_id, debit, quantity, state))
	return state, tuple(events)
