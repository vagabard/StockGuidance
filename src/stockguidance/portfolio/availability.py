from dataclasses import dataclass

from stockguidance.calendar.sessions import TradingSession, require_next_session


@dataclass(frozen=True)
class SaleProceedsTiming:
	"""Eligibility only; this contract neither books a sale nor supplies a ledger."""

	sale_session: TradingSession
	eligible_session: TradingSession

	def __post_init__(self) -> None:
		require_next_session(self.sale_session, self.eligible_session)

	def is_available_for(self, purchase_session: TradingSession) -> bool:
		if not isinstance(purchase_session, TradingSession):
			raise ValueError("purchase_session must be a TradingSession")
		if purchase_session.calendar_id != self.sale_session.calendar_id:
			raise ValueError("Proceeds and purchase must use the same calendar")
		return purchase_session.sequence >= self.eligible_session.sequence
