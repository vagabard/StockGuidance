from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from stockguidance.calendar.sessions import TradingSession, require_next_session
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.validation import require_aware_datetime, require_positive_int
from stockguidance.portfolio.availability import SaleProceedsTiming
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.portfolio.precision import SHARE, require_decimal, require_money
from stockguidance.portfolio.schedule import scheduled_allocation


@dataclass(frozen=True)
class Position:
	ticker_id: int
	quantity: Decimal
	entry_session: TradingSession
	exit_session: TradingSession
	entry_price: Decimal
	cost_basis: Decimal

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "position ticker ID")
		require_decimal(self.quantity, "position quantity")
		require_decimal(self.entry_price, "position entry price")
		require_money(self.cost_basis, "position cost basis")
		if self.quantity <= 0 or self.quantity % SHARE or self.entry_price <= 0 or self.cost_basis <= 0:
			raise ValueError("Positions require positive price, cost and eight-decimal share quantities")
		require_next_session(self.entry_session, self.exit_session)


@dataclass(frozen=True)
class RestingProceeds:
	amount: Decimal
	timing: SaleProceedsTiming

	def __post_init__(self) -> None:
		require_money(self.amount, "resting proceeds")
		if not isinstance(self.timing, SaleProceedsTiming):
			raise ValueError("Proceeds require next-session eligibility")


@dataclass(frozen=True)
class AllocationState:
	name: str
	available_cash: Decimal
	resting: tuple[RestingProceeds, ...] = ()
	positions: tuple[Position, ...] = ()
	realized_pnl: Decimal = Decimal("0.00")
	fees_paid: Decimal = Decimal("0.00")

	def __post_init__(self) -> None:
		if self.name not in ("A", "B"):
			raise ValueError("Allocation must be A or B")
		require_money(self.available_cash, "available cash")
		require_money(self.realized_pnl, "realized PnL", signed=True)
		require_money(self.fees_paid, "paid fees")
		if not isinstance(self.resting, tuple) or any(not isinstance(item, RestingProceeds) for item in self.resting) or not isinstance(self.positions, tuple) or any(not isinstance(item, Position) for item in self.positions):
			raise ValueError("Allocation balances require immutable typed positions and proceeds")
		if len({item.ticker_id for item in self.positions}) != len(self.positions):
			raise ValueError("An allocation cannot hold duplicate ticker positions")


@dataclass(frozen=True)
class PortfolioState:
	allocation: AllocationSpec
	as_of: datetime
	initial_capital: Decimal
	allocations: tuple[AllocationState, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.allocation, AllocationSpec):
			raise ValueError("Portfolio requires an allocation specification")
		require_aware_datetime(self.as_of, "portfolio as_of")
		require_money(self.initial_capital, "initial capital")
		expected = ("A",) if self.allocation.allocation_count == 1 else ("A", "B")
		if not isinstance(self.allocations, tuple) or any(not isinstance(item, AllocationState) for item in self.allocations) or tuple(item.name for item in self.allocations) != expected:
			raise ValueError("Portfolio must preserve its exact independent allocations")
		if self.allocation.allocation_count == 2 and self.initial_capital % Decimal("0.02"):
			raise ValueError("Initial halves require equal whole cents")
		for allocation in self.allocations:
			accounted = allocation.available_cash + sum((item.amount for item in allocation.resting), Decimal(0)) + sum((item.cost_basis for item in allocation.positions), Decimal(0))
			if accounted != self.initial_capital / self.allocation.allocation_count + allocation.realized_pnl:
				raise ValueError("Each allocation must conserve its own initial capital and realized gains without transfers")
			for position in allocation.positions:
				if position.entry_session.calendar_id != self.allocation.anchor_session.calendar_id or position.entry_session.closes_at > self.as_of:
					raise ValueError("Position belongs to another calendar or has not filled yet")
				if scheduled_allocation(self.allocation, position.entry_session) != allocation.name:
					raise ValueError("Position entry does not follow its independent anchored schedule")
			for proceeds in allocation.resting:
				if proceeds.timing.sale_session.calendar_id != self.allocation.anchor_session.calendar_id or proceeds.timing.sale_session.closes_at > self.as_of:
					raise ValueError("Resting proceeds belong to another calendar or a future sale")

	def validate_calendar(self, calendar: SessionCalendar) -> None:
		sessions = [self.allocation.anchor_session]
		for allocation in self.allocations:
			sessions.extend(session for position in allocation.positions for session in (position.entry_session, position.exit_session))
			sessions.extend(session for proceeds in allocation.resting for session in (proceeds.timing.sale_session, proceeds.timing.eligible_session))
		if any(session != calendar.session(session.session_date) for session in sessions):
			raise ValueError("Supplied portfolio sessions differ from the authoritative calendar")


def initial_portfolio(allocation: AllocationSpec, capital: Decimal, as_of: datetime) -> PortfolioState:
	require_money(capital, "initial capital")
	if capital <= 0 or (allocation.allocation_count == 2 and capital % Decimal("0.02")):
		raise ValueError("Initial capital must be positive and split into equal whole cents")
	portion = capital / allocation.allocation_count
	names = ("A",) if allocation.allocation_count == 1 else ("A", "B")
	return PortfolioState(allocation, as_of, capital, tuple(AllocationState(name, portion) for name in names))
