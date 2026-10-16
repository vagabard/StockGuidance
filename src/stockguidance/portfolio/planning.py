from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.portfolio.ledger import release_proceeds
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.portfolio.precision import ExecutionPolicy, money, require_money
from stockguidance.portfolio.schedule import scheduled_allocation
from stockguidance.portfolio.state import PortfolioState
from stockguidance.recommendations.result import RankedTicker


class PlanState(StrEnum):
	READY = "ready"
	NOT_SCHEDULED = "not_scheduled"
	POSITIONS_OPEN = "positions_open"
	NO_CASH = "no_available_cash"
	NO_SELECTION = "no_selection"


@dataclass(frozen=True)
class CashOrder:
	allocation: str
	ticker_id: int
	maximum_debit: Decimal

	def __post_init__(self) -> None:
		if self.allocation not in ("A", "B") or type(self.ticker_id) is not int or self.ticker_id <= 0:
			raise ValueError("Cash order requires an allocation and ticker ID")
		require_money(self.maximum_debit, "maximum order debit")
		if self.maximum_debit <= 0:
			raise ValueError("Cash order budget must be positive")


@dataclass(frozen=True)
class OrderPlan:
	portfolio: PortfolioState
	timing: DecisionTiming
	execution: ExecutionPolicy
	state: PlanState
	allocation: str | None
	orders: tuple[CashOrder, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.portfolio, PortfolioState) or not isinstance(self.timing, DecisionTiming) or not isinstance(self.execution, ExecutionPolicy) or not isinstance(self.state, PlanState):
			raise ValueError("Order plan requires typed portfolio, timing, execution and state")
		if self.portfolio.as_of > self.timing.decision_cutoff or self.allocation != scheduled_allocation(self.portfolio.allocation, self.timing.entry_session):
			raise ValueError("Order plan must use pre-decision state and the anchored allocation")
		if not isinstance(self.orders, tuple) or any(not isinstance(item, CashOrder) or item.allocation != self.allocation for item in self.orders) or len({item.ticker_id for item in self.orders}) != len(self.orders):
			raise ValueError("Plan orders must be unique and allocation-scoped")
		if bool(self.orders) != (self.state is PlanState.READY):
			raise ValueError("Only ready plans contain orders")
		if self.orders:
			balance = next(item for item in self.portfolio.allocations if item.name == self.allocation)
			if balance.positions or sum((item.maximum_debit for item in self.orders), Decimal(0)) > balance.available_cash:
				raise ValueError("Order plan exceeds independent available cash")


def plan_orders(state: PortfolioState, specification: AllocationSpec, timing: DecisionTiming, ranking: tuple[RankedTicker, ...], execution: ExecutionPolicy = ExecutionPolicy()) -> OrderPlan:
	if state.allocation != specification or state.as_of > timing.decision_cutoff:
		raise ValueError("Supplied portfolio specification or timestamp differs from the decision")
	if not isinstance(ranking, tuple) or any(not isinstance(item, RankedTicker) for item in ranking) or len({item.ticker_id for item in ranking}) != len(ranking):
		raise ValueError("Planning requires unique ranked tickers")
	if timing.decision_cutoff >= timing.entry_session.opens_at:
		state = release_proceeds(state, timing.entry_session, timing.decision_cutoff)
	allocation_name = scheduled_allocation(specification, timing.entry_session)
	status = PlanState.NOT_SCHEDULED
	orders: tuple[CashOrder, ...] = ()
	if allocation_name is not None:
		allocation = next(item for item in state.allocations if item.name == allocation_name)
		if allocation.positions:
			status = PlanState.POSITIONS_OPEN
		elif not ranking:
			status = PlanState.NO_SELECTION
		else:
			budget = money(allocation.available_cash / len(ranking), rounding=ROUND_DOWN)
			status = PlanState.READY if budget > execution.fee_per_fill else PlanState.NO_CASH
			if status is PlanState.READY:
				orders = tuple(CashOrder(allocation_name, item.ticker_id, budget) for item in ranking)
	return OrderPlan(state, timing, execution, status, allocation_name, orders)
