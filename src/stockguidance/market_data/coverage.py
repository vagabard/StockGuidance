from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_enum, require_text
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.validation.coverage import CoverageResult, CoverageState, SessionCoverage


class UpdateAction(StrEnum):
	NONE = "none"
	FETCH = "fetch"
	REPAIR = "repair"
	VERIFY = "verify"


@dataclass(frozen=True)
class SessionUpdate:
	session: TradingSession
	state: CoverageState
	action: UpdateAction
	expected_available_at: datetime | None
	stored_date_id: int | None
	detail: str

	def __post_init__(self) -> None:
		require_enum(self.state, CoverageState, "coverage state")
		require_enum(self.action, UpdateAction, "update action")
		require_text(self.detail, "update detail")


@dataclass(frozen=True)
class CoveragePlan:
	"""Read-only work proposal; expected publication is not provider verification."""

	request: MarketDataRequest
	boundary: HistoryBoundary
	calendar_version: str
	sessions: tuple[SessionUpdate, ...]
	query: SessionQuery | None
	latest_finalized: TradingSession | None
	latest_expected_available: TradingSession | None
	available_observations: tuple[ObservationTiming, ...]
	observed_first_in_scope: date | None
	observed_last_in_scope: date | None
	excluded_before_history: bool
	excluded_after_cutoff: bool
	fetchable_start: date | None = None
	fetchable_end: date | None = None
	provider_capability_state: str | None = None
	provider_capability_detail: str | None = None

	@property
	def fetch_sessions(self) -> tuple[TradingSession, ...]:
		return tuple(item.session for item in self.sessions if item.action is UpdateAction.FETCH)

	@property
	def repair_sessions(self) -> tuple[TradingSession, ...]:
		return tuple(item.session for item in self.sessions if item.action is UpdateAction.REPAIR)

	@property
	def coverage(self) -> CoverageResult:
		return CoverageResult(self.request, tuple(SessionCoverage(item.session, item.state, item.detail) for item in self.sessions))
