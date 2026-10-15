from dataclasses import dataclass
from enum import StrEnum

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_enum, require_text
from stockguidance.market_data.requests import MarketDataRequest


class CoverageState(StrEnum):
	COMPLETE = "complete"
	MISSING = "missing"
	INCOMPLETE = "incomplete"
	INCONSISTENT = "inconsistent"
	NOT_YET_AVAILABLE = "not_yet_available"
	UNVERIFIED = "unverified"


@dataclass(frozen=True)
class SessionCoverage:
	session: TradingSession
	state: CoverageState
	detail: str

	def __post_init__(self) -> None:
		if not isinstance(self.session, TradingSession):
			raise ValueError("session must be a TradingSession")
		require_enum(self.state, CoverageState, "state")
		require_text(self.detail, "detail")


@dataclass(frozen=True)
class CoverageResult:
	"""Assessed session results, not yet proof of entire-request completeness.

	Stage 2 must resolve expected sessions; Stage 3 must reconcile provider
	evidence. An empty assessment cannot establish readiness.
	"""

	request: MarketDataRequest
	sessions: tuple[SessionCoverage, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.request, MarketDataRequest):
			raise ValueError("request must be a MarketDataRequest")
		if not isinstance(self.sessions, tuple):
			raise ValueError("sessions must be an immutable tuple")
		seen: set[tuple[str, int]] = set()
		for assessment in self.sessions:
			if not isinstance(assessment, SessionCoverage):
				raise ValueError("sessions must contain SessionCoverage values")
			session = assessment.session
			if not self.request.start <= session.session_date <= self.request.end:
				raise ValueError("Assessed session falls outside requested coverage")
			if assessment.state is CoverageState.COMPLETE and session.closes_at > self.request.as_of:
				raise ValueError("A future daily close cannot have complete coverage at the cutoff")
			key = (session.calendar_id, session.sequence)
			if key in seen:
				raise ValueError("Coverage sessions must be unique")
			seen.add(key)

	@property
	def assessed_sessions_complete(self) -> bool:
		return bool(self.sessions) and all(item.state is CoverageState.COMPLETE for item in self.sessions)
