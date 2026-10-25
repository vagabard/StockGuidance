from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from enum import IntFlag, StrEnum
from typing import Any, Protocol
from collections.abc import Iterable

from stockguidance.market_data.identity import content_id
from stockguidance.market_data.records import DailyBar, count_value, decimal_price, decimal_quantity, decimal_vwap


FIELDS = ("open", "high", "low", "close", "volume", "vwap", "transactions")
METHOD = "linear-trading-session-v1"


class QualityFlag(IntFlag):
	CLEAN = 0
	BAD = 1
	INTERPOLATED_SINGLE = 2
	INTERPOLATED_GAP = 4
	FUTURE_LEAKAGE = 8


class HistoryMode(StrEnum):
	CLEAN = "clean"
	INTERPOLATED = "interpolated"


class Qualified(Protocol):
	@property
	def quality(self) -> QualityFlag: ...
	@property
	def gap_length(self) -> int: ...
	@property
	def provenance(self) -> tuple[str, ...]: ...


def combine_quality(items: Iterable[Qualified]) -> tuple[QualityFlag, int, tuple[str, ...]]:
	flags, gap = QualityFlag.CLEAN, 0
	provenance: set[str] = set()
	for item in items:
		flags |= item.quality
		gap = max(gap, item.gap_length)
		provenance.update(item.provenance)
	return flags, gap, tuple(sorted(provenance))


@dataclass(frozen=True)
class QualityPolicy:
	history: HistoryMode = HistoryMode.CLEAN
	retrospective: bool = False

	def __post_init__(self) -> None:
		if not isinstance(self.history, HistoryMode) or type(self.retrospective) is not bool:
			raise ValueError("Invalid quality policy")

	@property
	def identity(self) -> str:
		return content_id([METHOD, self.history.value, self.retrospective])

	def accepts(self, flags: QualityFlag) -> bool:
		return not flags & QualityFlag.BAD or (self.history is HistoryMode.INTERPOLATED and bool(flags & (QualityFlag.INTERPOLATED_SINGLE | QualityFlag.INTERPOLATED_GAP)))


def invalid_fields(values: tuple[object, ...]) -> tuple[str, ...]:
	if len(values) != len(FIELDS):
		raise ValueError("Invalid observed bar layout")
	invalid = []
	for index, (field, value) in enumerate(zip(FIELDS, values)):
		if value is None and index >= 5:
			continue
		try:
			(decimal_price if index < 4 else decimal_vwap if index == 5 else count_value if index == 6 else decimal_quantity)(Decimal(str(value)) if index == 6 and not isinstance(value, bool) else value)
		except (ValueError, TypeError, InvalidOperation):
			invalid.append(field)
	if not any(field in invalid for field in FIELDS[:4]):
		prices = tuple(Decimal(str(value)) for value in values[:4])
		if prices[2] > min(prices[0], prices[3]) or prices[1] < max(prices[0], prices[3]) or prices[2] > prices[1]:
			invalid.extend(FIELDS[:4])
	return tuple(invalid)


def decode_values(ticker: str, values: tuple[object, ...]) -> DailyBar:
	return DailyBar(ticker, decimal_price(values[0]), decimal_price(values[1]), decimal_price(values[2]), decimal_price(values[3]), decimal_quantity(values[4]), None if values[5] is None else decimal_vwap(values[5]), None if values[6] is None else count_value(Decimal(str(values[6]))))


@dataclass(frozen=True)
class ObservedBar:
	ticker: str
	session_date: date
	sequence: int
	values: tuple[object, ...]
	available_at: datetime

	def __post_init__(self) -> None:
		if not self.ticker or len(self.ticker) > 16 or any(character.isspace() for character in self.ticker) or len(self.values) != 7 or self.available_at.tzinfo is None:
			raise ValueError("Invalid quality observation")


@dataclass(frozen=True)
class QualityRecord:
	observation: ObservedBar
	flags: QualityFlag
	fields: tuple[str, ...]
	effective: tuple[object, ...] | None = None
	gap_length: int = 0
	prior: date | None = None
	following: date | None = None
	available_at: datetime | None = None
	endpoints_available_at: datetime | None = None
	endpoint_values: tuple[tuple[object, ...], ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.flags, QualityFlag) or int(self.flags) & ~15 or any(field not in FIELDS for field in self.fields):
			raise ValueError("Invalid quality classification")
		if self.effective is not None and invalid_fields(self.effective):
			raise ValueError("Invalid effective quality values")
		if self.flags & (QualityFlag.INTERPOLATED_SINGLE | QualityFlag.INTERPOLATED_GAP):
			if not self.flags & QualityFlag.BAD or self.effective is None or self.prior is None or self.following is None or self.available_at is None or self.endpoints_available_at is None or self.gap_length < 1:
				raise ValueError("Interpolation requires bounded provenance and availability")
		for timestamp in (self.available_at, self.endpoints_available_at):
			if timestamp is not None and timestamp.tzinfo is None:
				raise ValueError("Quality availability must be timezone aware")

	def payload(self) -> dict[str, object]:
		return {
			"ticker": self.observation.ticker, "date": self.observation.session_date.isoformat(), "sequence": self.observation.sequence,
			"original": [None if value is None else str(value) for value in self.observation.values],
			"observed_at": self.observation.available_at.isoformat(), "flags": int(self.flags), "fields": list(self.fields),
			"effective": None if self.effective is None else [None if value is None else str(value) for value in self.effective],
			"gap_length": self.gap_length, "prior": None if self.prior is None else self.prior.isoformat(),
			"following": None if self.following is None else self.following.isoformat(), "method": METHOD,
			"available_at": None if self.available_at is None else self.available_at.isoformat(),
			"endpoints_available_at": None if self.endpoints_available_at is None else self.endpoints_available_at.isoformat(),
			"endpoint_values": [[None if value is None else str(value) for value in values] for values in self.endpoint_values],
		}

	@property
	def revision(self) -> str:
		return content_id(self.payload())

	@classmethod
	def from_payload(cls, payload: dict[str, Any]) -> "QualityRecord":
		if payload.get("method") != METHOD:
			raise ValueError("Unsupported quality method")
		observed = ObservedBar(payload["ticker"], date.fromisoformat(payload["date"]), payload["sequence"], tuple(payload["original"]), datetime.fromisoformat(payload["observed_at"]))
		return cls(observed, QualityFlag(payload["flags"]), tuple(payload["fields"]), None if payload["effective"] is None else tuple(payload["effective"]), payload["gap_length"], None if payload["prior"] is None else date.fromisoformat(payload["prior"]), None if payload["following"] is None else date.fromisoformat(payload["following"]), None if payload["available_at"] is None else datetime.fromisoformat(payload["available_at"]), None if payload["endpoints_available_at"] is None else datetime.fromisoformat(payload["endpoints_available_at"]), tuple(tuple(values) for values in payload.get("endpoint_values", [])))

	def usable(self, policy: QualityPolicy, cutoff: datetime) -> tuple[tuple[object, ...] | None, QualityFlag]:
		flags = self.flags
		if self.observation.available_at > cutoff or not policy.accepts(flags) or self.effective is None:
			return None, flags
		if self.available_at is not None and self.available_at > cutoff:
			if not policy.retrospective:
				return None, flags
			flags |= QualityFlag.FUTURE_LEAKAGE
		return self.effective, flags


def interpolate(observations: tuple[ObservedBar, ...], repaired_at: datetime) -> tuple[QualityRecord, ...]:
	ordered = sorted(observations, key=lambda item: (item.ticker, item.sequence))
	if len({(item.ticker, item.sequence) for item in ordered}) != len(ordered):
		raise ValueError("Duplicate quality observation")
	issues = [invalid_fields(item.values) for item in ordered]
	result = []
	for index, item in enumerate(ordered):
		fields = issues[index]
		if not fields:
			continue
		start, end = index, index
		while start > 0 and ordered[start - 1].ticker == item.ticker and issues[start - 1] and ordered[start - 1].sequence + 1 == ordered[start].sequence:
			start -= 1
		while end + 1 < len(ordered) and ordered[end + 1].ticker == item.ticker and issues[end + 1] and ordered[end].sequence + 1 == ordered[end + 1].sequence:
			end += 1
		prior = next((ordered[position] for position in range(start - 1, -1, -1) if ordered[position].ticker == item.ticker and not issues[position]), None)
		following = next((ordered[position] for position in range(end + 1, len(ordered)) if ordered[position].ticker == item.ticker and not issues[position]), None)
		gap = end - start + 1
		if prior is None or following is None:
			result.append(QualityRecord(item, QualityFlag.BAD, fields, gap_length=gap))
			continue
		values = list(item.values)
		try:
			with localcontext() as context:
				context.prec = 90
				weight = Decimal(item.sequence - prior.sequence) / Decimal(following.sequence - prior.sequence)
				for field in fields:
					position = FIELDS.index(field)
					left, right = prior.values[position], following.values[position]
					value = Decimal(str(left)) + weight * (Decimal(str(right)) - Decimal(str(left)))
					values[position] = value.quantize(Decimal(1) if position == 6 else Decimal("1e-22") if position == 5 else Decimal("1e-8"), rounding=ROUND_HALF_UP)
			decode_values(item.ticker, tuple(values))
		except (ValueError, InvalidOperation, TypeError):
			result.append(QualityRecord(item, QualityFlag.BAD, fields, gap_length=gap))
			continue
		endpoints_available = max(item.available_at, prior.available_at, following.available_at)
		flags = QualityFlag.BAD | (QualityFlag.INTERPOLATED_SINGLE if gap == 1 else QualityFlag.INTERPOLATED_GAP)
		result.append(QualityRecord(item, flags, fields, tuple(values), gap, prior.session_date, following.session_date, max(repaired_at, endpoints_available), endpoints_available, (prior.values, following.values)))
	return tuple(result)
