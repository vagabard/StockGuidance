from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from zoneinfo import ZoneInfo

from stockguidance.calendar.sessions import TradingSession
from stockguidance.market_data.identity import content_id, mapping


_PRICE_LIMIT = Decimal("1e60")
_PRICE_QUANTA = {8: Decimal("1e-8"), 22: Decimal("1e-22")}


def is_non_trading_placeholder(values: tuple[object, ...]) -> bool:
	if len(values) != 7:
		return False
	open_value, high, low, close, volume, vwap, transactions = values
	try:
		prices = tuple(Decimal(str(value)) for value in (open_value, high, low, close, volume))
	except (InvalidOperation, TypeError, ValueError):
		return False
	return all(value == 0 for value in prices) and vwap is None and (transactions is None or transactions == 0)


def _decimal_price(value: object, places: int) -> Decimal:
	if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
		raise ValueError("Expected a numeric price")
	try:
		result = value if isinstance(value, Decimal) else Decimal(str(value))
		if not result.is_finite() or result <= 0 or result >= _PRICE_LIMIT:
			raise ValueError(f"Price must be finite, positive, and representable: {value!r}")
		with localcontext() as context:
			context.prec = 90
			result = result.quantize(_PRICE_QUANTA[places], rounding=ROUND_HALF_UP)
		if result <= 0:
			raise ValueError("Price rounds to zero")
		return result
	except InvalidOperation as error:
		raise ValueError("Invalid price") from error


def decimal_price(value: object) -> Decimal:
	return _decimal_price(value, 8)


def decimal_vwap(value: object) -> Decimal:
	return _decimal_price(value, 22)


def canonical_decimal(value: Decimal | int) -> str:
	decimal = Decimal(value)
	text = format(decimal, "f")
	return "0" if decimal == 0 else text.rstrip("0").rstrip(".") if "." in text else text


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
			decimal_vwap(self.vwap)
		if self.transactions is not None:
			count_value(self.transactions)


def bar_payload(bar: DailyBar) -> dict[str, object]:
	return {"T": bar.ticker, "o": canonical_decimal(bar.open), "h": canonical_decimal(bar.high),
		"l": canonical_decimal(bar.low), "c": canonical_decimal(bar.close),
		"v": canonical_decimal(decimal_quantity(bar.volume)), "vw": None if bar.vwap is None else canonical_decimal(decimal_vwap(bar.vwap)), "n": bar.transactions}


def decode_bar(value: object) -> DailyBar:
	row = mapping(value)
	ticker = row["T"]
	if not isinstance(ticker, str):
		raise ValueError("Ticker must be text")
	return DailyBar(ticker, decimal_price(row["o"]), decimal_price(row["h"]), decimal_price(row["l"]), decimal_price(row["c"]), decimal_quantity(row["v"]), None if row.get("vw") is None else decimal_vwap(row["vw"]), None if row.get("n") is None else count_value(row["n"]))


def provider_bar(value: object, session: TradingSession) -> DailyBar:
	row = mapping(value)
	timestamp = count_value(row["t"])
	observed = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
	if observed.astimezone(ZoneInfo("America/New_York")).date() != session.session_date:
		raise ValueError("Provider record belongs to a different session")
	return decode_bar(row)


def records_id(records: tuple[DailyBar, ...]) -> str:
	return content_id({"schema_version": 2, "records": [bar_payload(bar) for bar in sorted(records, key=lambda item: item.ticker)]})
