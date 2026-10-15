from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime, require_date, require_enum, require_positive_int, require_text
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.validation.coverage import CoverageState


@dataclass(frozen=True)
class SessionQuery:
	boundary: HistoryBoundary
	sessions: tuple[TradingSession, ...]
	as_of: datetime

	def __post_init__(self) -> None:
		if not isinstance(self.boundary, HistoryBoundary):
			raise ValueError("Session query requires an established history boundary")
		require_aware_datetime(self.as_of, "query cutoff")
		if not isinstance(self.sessions, tuple) or not self.sessions:
			raise ValueError("Session query requires a nonempty tuple of sessions")
		previous: date | None = None
		for session in self.sessions:
			if not isinstance(session, TradingSession) or session.calendar_id != self.boundary.calendar_id:
				raise ValueError("Query sessions must use the history calendar")
			if session.session_date < self.boundary.history_start:
				raise ValueError("Session query crosses the supported history boundary")
			if session.closes_at > self.as_of:
				raise ValueError("Session query crosses the information cutoff")
			if previous is not None and session.session_date <= previous:
				raise ValueError("Query sessions must be unique and chronological")
			previous = session.session_date


@dataclass(frozen=True)
class StoredSession:
	date_id: int
	session_date: date
	state: CoverageState
	detail: str
	available_at: datetime | None = None

	def __post_init__(self) -> None:
		require_positive_int(self.date_id, "date_id")
		require_date(self.session_date, "session_date")
		require_enum(self.state, CoverageState, "stored coverage state")
		require_text(self.detail, "stored coverage detail")
		if self.available_at is not None:
			require_aware_datetime(self.available_at, "stored available_at")
		if self.state is CoverageState.COMPLETE and self.available_at is None:
			raise ValueError("Complete stored coverage requires actual availability evidence")


class SessionRepository(Protocol):
	def read(self, query: SessionQuery) -> tuple[StoredSession, ...]: ...
