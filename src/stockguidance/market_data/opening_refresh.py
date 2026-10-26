from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from stockguidance.calendar.clock import Clock
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.closes import CloseQuery
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.closes import CloseData
from stockguidance.database.repositories.validation import DatabaseInspection
from stockguidance.features.specification import FeatureSpec, SignalMode
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.market_data.openings import OpeningData, OpeningQuery
from stockguidance.market_data.providers.openings import OpeningProvider


class CloseInspectionRepository(Protocol):
	def inspect(self, query: CloseQuery) -> tuple[CloseData, DatabaseInspection]: ...


@dataclass(frozen=True)
class OpeningRefresh:
	source: SourceConfig
	store: OpeningStore
	provider: OpeningProvider
	closes: CloseInspectionRepository

	def refresh(self, features: FeatureSpec, timing: DecisionTiming, boundary: HistoryBoundary, clock: Clock) -> OpeningData:
		if features.signal_mode is not SignalMode.ENTRY_OPEN or boundary.source != self.source.identity or features.adjustment is not self.source.identity.adjustment:
			raise ValueError("Opening capture requires matching entry_open source/specification")
		if not timing.entry_session.opens_at + timedelta(minutes=1) <= clock.now() < timing.entry_time:
			raise ValueError("Opening capture must complete after the first minute and before the close")
		query = OpeningQuery(boundary, (timing.entry_session,), features.ticker_ids, clock.now())
		existing = self.store.read_openings(query)
		if existing.issues:
			previous = CloseQuery(SessionQuery(boundary, (timing.previous_session,), clock.now()), features.ticker_ids)
			data, inspection = self.closes.inspect(previous)
			if data.issues:
				raise ValueError("Opening symbols require verified prior-session observations for the full universe")
			symbols = dict(inspection.ticker_symbols)
			if any(ticker_id not in symbols for ticker_id in features.ticker_ids):
				raise ValueError("Opening universe has unresolved ticker symbols")
			for issue in existing.issues:
				if clock.now() >= timing.entry_time:
					raise ValueError("Opening capture missed the entry close")
				observation = self.provider.fetch(self.source, timing.entry_session, issue.ticker_id, symbols[issue.ticker_id], clock)
				if observation is not None:
					if observation.ticker_id != issue.ticker_id or observation.symbol != symbols[issue.ticker_id] or observation.session != timing.entry_session or observation.available_at > clock.now():
						raise ValueError("Opening provider changed the requested identity or receipt time")
					self.store.save(self.source.identity, observation)
		result = self.store.read_openings(OpeningQuery(boundary, (timing.entry_session,), features.ticker_ids, clock.now()))
		if result.issues or clock.now() >= timing.entry_time:
			raise ValueError("Full-universe openings are not verified before the entry close")
		return result
