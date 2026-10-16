from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from stockguidance.calendar.sessions import TradingSession
from stockguidance.portfolio.state import PortfolioState


def release_proceeds(state: PortfolioState, session: TradingSession, at: datetime) -> PortfolioState:
	if at < state.as_of or at < session.opens_at or session.calendar_id != state.allocation.anchor_session.calendar_id:
		raise ValueError("Cash availability events must follow the ledger and eligible session open")
	allocations = []
	for allocation in state.allocations:
		released = tuple(item for item in allocation.resting if item.timing.is_available_for(session))
		remaining = tuple(item for item in allocation.resting if not item.timing.is_available_for(session))
		allocations.append(replace(allocation, available_cash=allocation.available_cash + sum((item.amount for item in released), Decimal(0)), resting=remaining))
	return replace(state, as_of=at, allocations=tuple(allocations))
