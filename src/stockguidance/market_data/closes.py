from dataclasses import dataclass
from enum import StrEnum
import math

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_enum, require_positive_int, require_text
from stockguidance.market_data.availability import ObservationTiming


class PriceState(StrEnum):
	MISSING = "missing"
	INVALID = "invalid"
	UNVERIFIED = "unverified"
	NOT_YET_AVAILABLE = "not_yet_available"
	EXPECTED_ABSENCE = "expected_absence"


def valid_close(value: object) -> float:
	if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
		raise ValueError("Close price must be a finite positive number")
	return float(value)


@dataclass(frozen=True)
class CloseObservation:
	ticker_id: int
	close: float
	timing: ObservationTiming
	revision: str

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "close ticker_id")
		valid_close(self.close)
		if not isinstance(self.timing, ObservationTiming):
			raise ValueError("Close requires observation/availability timing")
		require_text(self.revision, "close data revision")


@dataclass(frozen=True)
class CloseIssue:
	ticker_id: int
	session: TradingSession
	state: PriceState
	detail: str

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "unavailable ticker_id")
		if not isinstance(self.session, TradingSession):
			raise ValueError("Unavailable close requires a session")
		require_enum(self.state, PriceState, "price state")
		require_text(self.detail, "price detail")
