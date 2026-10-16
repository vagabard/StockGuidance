from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime, require_enum, require_text
from stockguidance.portfolio.state import PortfolioState


class EventKind(StrEnum):
	AVAILABILITY = "next_session_availability"
	DECISION = "decision"
	TRAINING = "training"
	ORDER = "order"
	BUY = "buy_fill"
	SALE = "sale_fill"
	UNFILLED_BUY = "unfilled_buy"
	UNFILLED_EXIT = "unfilled_exit"
	SKIP = "skipped_decision"
	MARK = "marked_equity"
	FAILURE = "required_stage_failure"


@dataclass(frozen=True)
class SimulationEvent:
	kind: EventKind
	at: datetime
	session: TradingSession
	detail: str
	allocation: str | None = None
	ticker_id: int | None = None
	amount: Decimal | None = None
	quantity: Decimal | None = None
	portfolio: PortfolioState | None = None

	def __post_init__(self) -> None:
		require_enum(self.kind, EventKind, "event kind")
		require_aware_datetime(self.at, "event time")
		require_text(self.detail, "event detail")
		if not isinstance(self.session, TradingSession):
			raise ValueError("Simulation events require an exchange session")
