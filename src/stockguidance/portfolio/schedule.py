from stockguidance.calendar.sessions import TradingSession
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec


def scheduled_allocation(specification: AllocationSpec, session: TradingSession) -> str | None:
	if not isinstance(specification, AllocationSpec) or not isinstance(session, TradingSession) or session.calendar_id != specification.anchor_session.calendar_id:
		raise ValueError("Allocation schedule requires matching typed calendar sessions")
	offset = session.sequence - specification.anchor_session.sequence
	if offset < 0:
		raise ValueError("Session precedes the allocation anchor")
	if specification.policy is AllocationPolicy.STAGGERED_HALVES:
		return "A" if offset % 2 == 0 else "B"
	return "A" if offset % 2 == 0 else None


def buy_decision_index(specification: AllocationSpec, session: TradingSession) -> int:
	if scheduled_allocation(specification, session) is None:
		raise ValueError("Session has no scheduled buy decision")
	offset = session.sequence - specification.anchor_session.sequence
	return offset if specification.policy is AllocationPolicy.STAGGERED_HALVES else offset // 2
