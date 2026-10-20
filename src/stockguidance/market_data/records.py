from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from zoneinfo import ZoneInfo

from stockguidance.calendar.sessions import TradingSession
from stockguidance.market_data.identity import content_id, mapping


def decimal_price(value: object) -> Decimal:
	if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
		raise ValueError("Expected a numeric price")
	try:
		result = Decimal(str(value))
		if not result.is_finite() or result <= 0 or result >= Decimal("1e60"):
			raise ValueError("Price must be finite, positive, and representable")
		with localcontext() as context:
			context.prec = max(70, len(result.as_tuple().digits) + abs(result.as_tuple().exponent) + 8)
			result = result.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
		if result <= 0:
			raise ValueError("Price rounds to zero")
		return result
	except InvalidOperation as error:
		raise ValueError("Invalid price") from error


def decimal_quantity(value: object) -> Decimal:
	if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
		raise ValueError("Expected a numeric quantity")
	try:
		result = Decimal(str(value))
	except InvalidOperation as error:
		raise ValueError("Invalid quantity") from error
	if not result.is_finite() or result < 0 or result > Decimal(2**63 - 1):
		raise ValueError("Quantity must be nonnegative and representable")
	return result


def count_value(value: object) -> int:
	if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
		raise ValueError("Expected an integer count")
	result = decimal_quantity(value)
	if result != result.to_integral_value():
		raise ValueError("Count must be a nonnegative representable integer")
	return int(result)


@dataclass(frozen=True)
class DailyBar:
	ticker: str
	open: Decimal
	high: Decimal
	low: Decimal
	close: Decimal
	volume: Decimal
	vwap: Decimal | None
	transactions: int | None

	def __post_init__(self) -> None:
		if not isinstance(self.ticker, str) or not self.ticker or len(self.ticker) > 16 or any(character.isspace() for character in self.ticker):
			raise ValueError("Invalid ticker key")
		for price in (self.open, self.high, self.low, self.close):
			if not isinstance(price, Decimal) or price != decimal_price(price):
				raise ValueError("Daily prices must use database precision")
		if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
			raise ValueError("Inconsistent OHLC prices")
		if decimal_quantity(self.volume) != self.volume:
			raise ValueError("Daily volume must use exact decimal quantity precision")
		if self.vwap is not None:
			decimal_price(self.vwap)
		if self.transactions is not None:
			count_value(self.transactions)


def bar_payload(bar: DailyBar) -> dict[str, object]:
	volume = decimal_quantity(bar.volume)
	volume_value: int | str = int(volume) if volume == volume.to_integral_value() else format(volume.normalize(), "f")
	return {"T": bar.ticker, "o": str(decimal_price(bar.open)), "h": str(decimal_price(bar.high)), "l": str(decimal_price(bar.low)), "c": str(decimal_price(bar.close)), "v": volume_value, "vw": None if bar.vwap is None else str(decimal_price(bar.vwap)), "n": bar.transactions}


def decode_bar(value: object) -> DailyBar:
	row = mapping(value)
	ticker = row["T"]
	if not isinstance(ticker, str):
		raise ValueError("Ticker must be text")
	return DailyBar(ticker, decimal_price(row["o"]), decimal_price(row["h"]), decimal_price(row["l"]), decimal_price(row["c"]), decimal_quantity(row["v"]), None if row.get("vw") is None else decimal_price(row["vw"]), None if row.get("n") is None else count_value(row["n"]))


def provider_bar(value: object, session: TradingSession) -> DailyBar:
	row = mapping(value)
	timestamp = count_value(row["t"])
	observed = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
	if observed.astimezone(ZoneInfo("America/New_York")).date() != session.session_date:
		raise ValueError("Provider record belongs to a different session")
	return decode_bar(row)


def records_id(records: tuple[DailyBar, ...]) -> str:
	return content_id([bar_payload(bar) for bar in sorted(records, key=lambda item: item.ticker)])
