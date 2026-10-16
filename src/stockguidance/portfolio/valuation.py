from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from stockguidance.calendar.sessions import TradingSession
from stockguidance.portfolio.precision import money
from stockguidance.portfolio.marks import ClosingMark
from stockguidance.portfolio.state import PortfolioState, Position


@dataclass(frozen=True)
class PositionMark:
	allocation: str
	position: Position
	price: Decimal
	price_session: TradingSession
	value: Decimal
	current: bool


@dataclass(frozen=True)
class AllocationValue:
	name: str
	available_cash: Decimal
	resting_proceeds: Decimal
	positions_value: Decimal
	equity: Decimal
	realized_pnl: Decimal
	unrealized_pnl: Decimal
	fees_paid: Decimal


@dataclass(frozen=True)
class PortfolioValue:
	session: TradingSession
	portfolio: PortfolioState
	allocations: tuple[AllocationValue, ...]
	positions: tuple[PositionMark, ...]

	@property
	def total_equity(self) -> Decimal:
		return sum((item.equity for item in self.allocations), Decimal(0))

	@property
	def marks_current(self) -> bool:
		return all(item.current for item in self.positions)


def value_portfolio(state: PortfolioState, session: TradingSession, last_quotes: Mapping[int, ClosingMark]) -> PortfolioValue:
	allocations, marks = [], []
	for allocation in state.allocations:
		allocation_marks = []
		for position in allocation.positions:
			quote = last_quotes.get(position.ticker_id)
			if quote is not None and quote.session.sequence > session.sequence:
				raise ValueError("Portfolio marks cannot use future prices")
			price = position.entry_price if quote is None else quote.price
			price_session = position.entry_session if quote is None else quote.session
			allocation_marks.append(PositionMark(allocation.name, position, price, price_session, money(price * position.quantity), price_session == session))
		position_value = sum((item.value for item in allocation_marks), Decimal(0))
		basis = sum((item.cost_basis for item in allocation.positions), Decimal(0))
		resting = sum((item.amount for item in allocation.resting), Decimal(0))
		allocations.append(AllocationValue(allocation.name, allocation.available_cash, resting, position_value, allocation.available_cash + resting + position_value, allocation.realized_pnl, position_value - basis, allocation.fees_paid))
		marks.extend(allocation_marks)
	return PortfolioValue(session, state, tuple(allocations), tuple(marks))
