from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime, require_positive_int, require_text
from stockguidance.market_data.closes import CloseIssue, valid_close
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.identity import content_id


OPENING_ENDPOINT = "/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}"
OPENING_METHOD = "first_regular_session_minute_open-v1"


@dataclass(frozen=True)
class OpeningObservation:
	ticker_id: int
	symbol: str
	session: TradingSession
	price: float
	available_at: datetime
	response_identity: str
	method: str = OPENING_METHOD

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "opening ticker ID")
		require_text(self.symbol, "opening symbol")
		valid_close(self.price)
		require_aware_datetime(self.available_at, "opening receipt")
		require_text(self.response_identity, "opening response identity")
		if not isinstance(self.session, TradingSession) or self.available_at < self.session.opens_at + timedelta(minutes=1) or self.method != OPENING_METHOD:
			raise ValueError("Opening requires a completed first regular-session minute and actual receipt")

	@property
	def revision(self) -> str:
		return content_id({"ticker_id": self.ticker_id, "symbol": self.symbol, "session": self.session.session_date.isoformat(), "observed_at": self.session.opens_at.isoformat(), "price": self.price, "available_at": self.available_at.isoformat(), "response_identity": self.response_identity, "method": self.method})


@dataclass(frozen=True)
class OpeningQuery:
	boundary: HistoryBoundary
	sessions: tuple[TradingSession, ...]
	ticker_ids: tuple[int, ...]
	as_of: datetime

	def __post_init__(self) -> None:
		require_aware_datetime(self.as_of, "opening cutoff")
		if not isinstance(self.boundary, HistoryBoundary) or not isinstance(self.sessions, tuple) or not self.sessions or not isinstance(self.ticker_ids, tuple) or not self.ticker_ids:
			raise ValueError("Opening query requires explicit history, sessions and ticker order")
		for ticker_id in self.ticker_ids:
			require_positive_int(ticker_id, "opening query ticker ID")
		if len(set(self.ticker_ids)) != len(self.ticker_ids) or any(not isinstance(session, TradingSession) or session.calendar_id != self.boundary.calendar_id or session.session_date < self.boundary.history_start for session in self.sessions):
			raise ValueError("Opening query crosses history/calendar or repeats tickers")
		if any(right.sequence <= left.sequence for left, right in zip(self.sessions, self.sessions[1:])):
			raise ValueError("Opening query sessions must be chronological")


@dataclass(frozen=True)
class OpeningData:
	query: OpeningQuery
	observations: tuple[OpeningObservation, ...]
	issues: tuple[CloseIssue, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.query, OpeningQuery) or not isinstance(self.observations, tuple) or not isinstance(self.issues, tuple):
			raise ValueError("Invalid opening result contracts")
		expected = {(session, ticker_id) for session in self.query.sessions for ticker_id in self.query.ticker_ids}
		keys = []
		for observation in self.observations:
			if not isinstance(observation, OpeningObservation) or observation.available_at > self.query.as_of:
				raise ValueError("Opening result contains unavailable observations")
			keys.append((observation.session, observation.ticker_id))
		for issue in self.issues:
			if not isinstance(issue, CloseIssue):
				raise ValueError("Invalid opening issue")
			keys.append((issue.session, issue.ticker_id))
		if len(keys) != len(set(keys)) or set(keys) != expected:
			raise ValueError("Opening result must account for every exact session/ticker key once")

	@property
	def revision(self) -> str:
		return content_id({"source": asdict(self.query.boundary.source), "endpoint": OPENING_ENDPOINT, "method": OPENING_METHOD, "observations": sorted(item.revision for item in self.observations), "issues": sorted((issue.session.session_date.isoformat(), issue.ticker_id, issue.state.value) for issue in self.issues)})


@runtime_checkable
class OpeningRepository(Protocol):
	def read_openings(self, query: OpeningQuery) -> OpeningData: ...
