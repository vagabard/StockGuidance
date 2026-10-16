from dataclasses import dataclass

from stockguidance.database.repositories.closes import CloseData, CloseQuery, CloseRepository
from stockguidance.market_data.openings import OpeningData, OpeningQuery, OpeningRepository


@dataclass(frozen=True)
class SignalRepository:
	closes: CloseRepository
	openings: OpeningRepository

	def read(self, query: CloseQuery) -> CloseData:
		return self.closes.read(query)

	def read_openings(self, query: OpeningQuery) -> OpeningData:
		return self.openings.read_openings(query)
