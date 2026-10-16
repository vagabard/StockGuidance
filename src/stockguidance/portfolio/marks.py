from dataclasses import dataclass
from decimal import Decimal

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_positive_int, require_text
from stockguidance.portfolio.precision import require_decimal


@dataclass(frozen=True)
class ClosingMark:
	ticker_id: int
	session: TradingSession
	price: Decimal
	revision: str

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "closing ticker ID")
		require_decimal(self.price, "closing price")
		require_text(self.revision, "closing revision")
		if self.price <= 0 or not isinstance(self.session, TradingSession):
			raise ValueError("Closing marks require positive prices and typed sessions")
