from typing import Protocol

from stockguidance.database.repositories.closes import CloseQuery
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.recommendations.eligibility import LiquidityObservation


class EligibilityRepository(Protocol):
	def read(self, query: CloseQuery) -> tuple[LiquidityObservation, ...]: ...


class VerifiedEligibilityRepository:
	def __init__(self, closes: VerifiedCloseRepository) -> None:
		self.closes = closes

	def read(self, query: CloseQuery) -> tuple[LiquidityObservation, ...]:
		if len(query.scope.sessions) != 1:
			raise ValueError("Liquidity must use exactly the previous completed session")
		data, inspection = self.closes.inspect(query)
		symbols = dict(inspection.ticker_symbols)
		bars = {(snapshot.session, bar.ticker): bar for snapshot in inspection.snapshots for bar in snapshot.records}
		observations: list[LiquidityObservation] = []
		for item in data.observations:
			transactions = bars[item.timing.session, symbols[item.ticker_id]].transactions
			if transactions is not None:
				observations.append(LiquidityObservation(item.ticker_id, transactions, item.timing, item.revision))
		return tuple(observations)
