from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime
from stockguidance.database.repositories.closes import CloseQuery, CloseRepository
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.closes import CloseIssue
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.portfolio.marks import ClosingMark


@dataclass(frozen=True)
class ExecutionFrame:
	source: SourceIdentity
	session: TradingSession
	ticker_ids: tuple[int, ...]
	quotes: tuple[ClosingMark, ...]
	issues: tuple[CloseIssue, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity) or not isinstance(self.session, TradingSession) or not isinstance(self.ticker_ids, tuple) or not self.ticker_ids:
			raise ValueError("Execution frames require exact source, session and ticker scope")
		if not isinstance(self.quotes, tuple) or any(not isinstance(item, ClosingMark) or item.session != self.session for item in self.quotes) or not isinstance(self.issues, tuple) or any(not isinstance(item, CloseIssue) or item.session != self.session for item in self.issues):
			raise ValueError("Execution quotes/issues must belong to the requested session")
		keys = (*tuple(item.ticker_id for item in self.quotes), *tuple(item.ticker_id for item in self.issues))
		if len(set(keys)) != len(keys) or set(keys) != set(self.ticker_ids) or len(set(self.ticker_ids)) != len(self.ticker_ids):
			raise ValueError("Every requested ticker requires exactly one execution price or explicit issue")


class ExecutionPriceSource(Protocol):
	@property
	def source(self) -> SourceIdentity: ...
	@property
	def audit_cutoff(self) -> datetime: ...
	def read(self, session: TradingSession, ticker_ids: tuple[int, ...]) -> ExecutionFrame: ...


class RepositoryExecutionPrices:
	"""Retrospective fill evidence is isolated from decision-time signal reads."""

	def __init__(self, boundary: HistoryBoundary, repository: CloseRepository, audit_cutoff: datetime) -> None:
		require_aware_datetime(audit_cutoff, "execution audit cutoff")
		self.boundary, self.repository, self.audit_cutoff = boundary, repository, audit_cutoff

	@property
	def source(self) -> SourceIdentity:
		return self.boundary.source

	def read(self, session: TradingSession, ticker_ids: tuple[int, ...]) -> ExecutionFrame:
		query = CloseQuery(SessionQuery(self.boundary, (session,), self.audit_cutoff), ticker_ids)
		data = self.repository.read(query)
		if data.query != query:
			raise ValueError("Execution repository changed the request")
		quotes = tuple(ClosingMark(item.ticker_id, session, Decimal(str(item.close)), item.revision) for item in data.observations)
		return ExecutionFrame(self.source, session, ticker_ids, quotes, data.issues)
