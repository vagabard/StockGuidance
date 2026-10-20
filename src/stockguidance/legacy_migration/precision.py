from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.performance.archive_read import inventory, unique_object


ARCHIVE_SCHEMA_VERSION = 2
EXCHANGE_ZONE = ZoneInfo("America/New_York")
NUMERIC_FIELDS = ("open", "high", "low", "close", "volume", "vwap")


def _decimal(value: object, field_name: str) -> Decimal:
	if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
		raise ValueError(f"Invalid archive {field_name}")
	try:
		result = Decimal(str(value))
	except Exception as error:
		raise ValueError(f"Invalid archive {field_name}") from error
	if not result.is_finite():
		raise ValueError(f"Invalid archive {field_name}")
	return result


def _canonical_decimal(value: Decimal) -> str:
	if value == 0:
		return "0"
	return format(value.normalize(), "f")


def _digits(value: Decimal) -> tuple[int, int]:
	tuple_value = value.as_tuple()
	if value == 0:
		return 1, max(0, -tuple_value.exponent)
	integer_digits = max(1, len(tuple_value.digits) + tuple_value.exponent)
	return integer_digits, max(0, -tuple_value.exponent)


@dataclass(frozen=True)
class ExactArchiveBar:
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
			raise ValueError("Invalid archive ticker")
		prices = (self.open, self.high, self.low, self.close)
		if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 or value >= Decimal("1e60") for value in prices):
			raise ValueError("Invalid archive price")
		if self.vwap is not None and (not isinstance(self.vwap, Decimal) or not self.vwap.is_finite() or self.vwap <= 0 or self.vwap >= Decimal("1e60")):
			raise ValueError("Invalid archive vwap")
		if not isinstance(self.volume, Decimal) or not self.volume.is_finite() or self.volume < 0 or self.volume > Decimal(2**63 - 1):
			raise ValueError("Invalid archive volume")
		if self.transactions is not None and (type(self.transactions) is not int or not 0 <= self.transactions <= 2**63 - 1):
			raise ValueError("Invalid archive transactions")
		if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
			raise ValueError("Inconsistent archive OHLC")


def exact_bar_payload(bar: ExactArchiveBar) -> dict[str, object]:
	return {
		"T": bar.ticker,
		"o": _canonical_decimal(bar.open),
		"h": _canonical_decimal(bar.high),
		"l": _canonical_decimal(bar.low),
		"c": _canonical_decimal(bar.close),
		"v": _canonical_decimal(bar.volume),
		"vw": None if bar.vwap is None else _canonical_decimal(bar.vwap),
		"n": bar.transactions,
	}


def exact_records_id(bars: tuple[ExactArchiveBar, ...]) -> str:
	return content_id({"schema_version": ARCHIVE_SCHEMA_VERSION, "records": [exact_bar_payload(bar) for bar in sorted(bars, key=lambda item: item.ticker)]})


def _exact_row(row: dict[str, Any], session: date) -> ExactArchiveBar:
	timestamp = row.get("t")
	if type(timestamp) is not int or not 0 <= timestamp <= 2**63 - 1:
		raise ValueError("Invalid archive timestamp")
	if datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(EXCHANGE_ZONE).date() != session:
		raise ValueError("Archive timestamp and session disagree")
	transactions = row.get("n")
	if transactions is not None:
		if type(transactions) is not int or not 0 <= transactions <= 2**63 - 1:
			raise ValueError("Invalid archive transactions")
	volume = _decimal(row.get("v"), "volume")
	return ExactArchiveBar(
		row.get("T"),
		_decimal(row.get("o"), "open"),
		_decimal(row.get("h"), "high"),
		_decimal(row.get("l"), "low"),
		_decimal(row.get("c"), "close"),
		volume,
		None if row.get("vw") is None else _decimal(row.get("vw"), "vwap"),
		transactions,
	)


def read_exact_archive(path: Path) -> tuple[tuple[ExactArchiveBar, ...], str]:
	session = date.fromisoformat(path.stem)
	digest = sha256()
	bars: dict[str, ExactArchiveBar] = {}
	with path.open("rb") as stream:
		for line in stream:
			digest.update(line)
			row = json.loads(line, parse_float=Decimal, object_pairs_hook=unique_object)
			if not isinstance(row, dict):
				raise ValueError("Archive record must be an object")
			bar = _exact_row(row, session)
			if bar.ticker in bars:
				raise ValueError("Duplicate archive ticker")
			bars[bar.ticker] = bar
	if not bars:
		raise ValueError("Empty archive does not establish no trading")
	return tuple(sorted(bars.values(), key=lambda item: item.ticker)), digest.hexdigest()


@dataclass
class NumericFieldStats:
	count: int = 0
	fractional_count: int = 0
	max_value: Decimal = Decimal(0)
	max_integer_digits: int = 1
	max_fractional_digits: int = 0
	max_source_precision: int = 0

	def add(self, value: Decimal) -> None:
		integer_digits, fractional_digits = _digits(value)
		self.count += 1
		self.fractional_count += int(value != value.to_integral_value())
		self.max_value = max(self.max_value, value)
		self.max_integer_digits = max(self.max_integer_digits, integer_digits)
		self.max_fractional_digits = max(self.max_fractional_digits, fractional_digits)
		self.max_source_precision = max(self.max_source_precision, integer_digits + fractional_digits)

	def payload(self) -> dict[str, object]:
		return {
			"count": self.count,
			"fractional_count": self.fractional_count,
			"max_value": _canonical_decimal(self.max_value),
			"max_integer_digits": self.max_integer_digits,
			"max_fractional_digits": self.max_fractional_digits,
			"max_source_precision": self.max_source_precision,
		}


@dataclass(frozen=True)
class ArchiveFileInventory:
	path: str
	session: str
	file_sha256: str
	valid: bool
	record_count: int
	records_id: str | None
	keys: tuple[str, ...]
	finding: str | None = None


@dataclass
class ArchiveInventory:
	root: Path
	files: list[ArchiveFileInventory] = field(default_factory=list)
	stats: dict[str, NumericFieldStats] = field(default_factory=lambda: {field_name: NumericFieldStats() for field_name in NUMERIC_FIELDS})
	keys: set[str] = field(default_factory=set)
	key_dates: dict[str, tuple[str, str]] = field(default_factory=dict)

	@property
	def valid_files(self) -> tuple[ArchiveFileInventory, ...]:
		return tuple(item for item in self.files if item.valid)

	@property
	def invalid_files(self) -> tuple[ArchiveFileInventory, ...]:
		return tuple(item for item in self.files if not item.valid)

	def identity(self, existing: tuple[tuple[int, str], ...] = ()) -> dict[str, object]:
		archive_keys = set(self.keys)
		archive_folded: dict[str, list[str]] = {}
		for key in sorted(archive_keys):
			archive_folded.setdefault(key.casefold(), []).append(key)
		existing_rows = [{"id": identifier, "label": label} for identifier, label in existing]
		exact_matches = [{"id": identifier, "label": label, "archive_key": label, "action": "preserve_id"} for identifier, label in existing if label in archive_keys]
		aliases: list[dict[str, object]] = []
		for identifier, label in existing:
			if label in archive_keys:
				continue
			candidates = archive_folded.get(label.casefold(), [])
			if len(candidates) == 1:
				aliases.append({"id": identifier, "label": label, "archive_key": candidates[0], "action": "rename_preserve_id"})
			elif candidates:
				aliases.append({"id": identifier, "label": label, "candidates": candidates, "action": "unresolved_collision"})
			else:
				aliases.append({"id": identifier, "label": label, "action": "database_only"})
		archive_only = sorted(archive_keys - {item["archive_key"] for item in exact_matches})
		collisions = [{"folded_key": folded, "keys": values} for folded, values in sorted(archive_folded.items()) if len(values) > 1]
		return {
			"schema_version": 1,
			"archive_key_count": len(archive_keys),
			"archive_keys": sorted(archive_keys),
			"archive_key_dates": {key: list(self.key_dates[key]) for key in sorted(self.key_dates)},
			"existing_rows": existing_rows,
			"exact_matches": exact_matches,
			"aliases": aliases,
			"archive_only_keys": archive_only,
			"collisions": collisions,
			"unresolved_aliases": [item for item in aliases if item["action"] == "unresolved_collision"],
		}


def scan_archive(root: Path, start: date | None = None, end: date | None = None) -> ArchiveInventory:
	result = ArchiveInventory(root.resolve())
	for path in inventory(root):
		session = date.fromisoformat(path.stem)
		if (start is not None and session < start) or (end is not None and session > end):
			continue
		try:
			bars, file_hash = read_exact_archive(path)
			for bar in bars:
				for name, value in (("open", bar.open), ("high", bar.high), ("low", bar.low), ("close", bar.close), ("volume", bar.volume), ("vwap", bar.vwap)):
					if value is not None:
						result.stats[name].add(value)
				result.keys.add(bar.ticker)
				observed = session.isoformat()
				first_last = result.key_dates.get(bar.ticker)
				result.key_dates[bar.ticker] = (observed, observed) if first_last is None else (min(first_last[0], observed), max(first_last[1], observed))
			result.files.append(ArchiveFileInventory(str(path.resolve()), path.stem, file_hash, True, len(bars), exact_records_id(bars), ()))
		except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
			file_hash = sha256(path.read_bytes()).hexdigest()
			result.files.append(ArchiveFileInventory(str(path.resolve()), path.stem, file_hash, False, 0, None, (), type(error).__name__))
	return result


def scan_archive_keys(root: Path, start: date | None = None, end: date | None = None) -> ArchiveInventory:
	result = ArchiveInventory(root.resolve())
	for path in inventory(root):
		session = date.fromisoformat(path.stem)
		if (start is not None and session < start) or (end is not None and session > end):
			continue
		file_hash = sha256()
		try:
			with path.open("rb") as stream:
				for line in stream:
					file_hash.update(line)
					row = json.loads(line)
					key = row.get("T") if isinstance(row, dict) else None
					if not isinstance(key, str) or not key or len(key) > 16 or any(character.isspace() for character in key):
						raise ValueError("Invalid archive ticker")
					result.keys.add(key)
					observed = session.isoformat()
					first_last = result.key_dates.get(key)
					result.key_dates[key] = (observed, observed) if first_last is None else (min(first_last[0], observed), max(first_last[1], observed))
			result.files.append(ArchiveFileInventory(str(path.resolve()), path.stem, file_hash.hexdigest(), True, 0, None, ()))
		except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
			result.files.append(ArchiveFileInventory(str(path.resolve()), path.stem, file_hash.hexdigest(), False, 0, None, (), type(error).__name__))
	return result


def select_decimal_type(stats: NumericFieldStats, *, integer_headroom: int = 2, fractional_headroom: int = 2) -> str:
	integer_digits = stats.max_integer_digits + integer_headroom
	scale = stats.max_fractional_digits + fractional_headroom
	precision = integer_digits + scale
	if precision > 65:
		raise ValueError("Observed decimal range cannot fit MySQL DECIMAL")
	return f"DECIMAL({precision},{scale})"


def schema_selection(inventory_result: ArchiveInventory) -> dict[str, object]:
	columns = {name: select_decimal_type(stats) for name, stats in inventory_result.stats.items()}
	ddl = {
		"ticker_label": "VARCHAR(16) COLLATE utf8mb4_bin NOT NULL UNIQUE",
		"open": columns["open"],
		"high": columns["high"],
		"low": columns["low"],
		"close": columns["close"],
		"volume": columns["volume"],
		"vwap": columns["vwap"],
		"transactions": "BIGINT",
	}
	return {"columns": columns, "ddl": ddl, "schema_hash": content_id(ddl)}


def inventory_payload(inventory_result: ArchiveInventory) -> dict[str, object]:
	selection = schema_selection(inventory_result)
	return {
		"schema_version": 1,
		"operation": "archive_precision_inventory",
		"checked_at": datetime.now(timezone.utc).isoformat(),
		"root": str(inventory_result.root),
		"file_count": len(inventory_result.files),
		"valid_file_count": len(inventory_result.valid_files),
		"invalid_file_count": len(inventory_result.invalid_files),
		"archive_key_count": len(inventory_result.keys),
		"archive_keys": sorted(inventory_result.keys),
		"archive_key_dates": {key: list(inventory_result.key_dates[key]) for key in sorted(inventory_result.key_dates)},
		"files": [item.__dict__ for item in inventory_result.files],
		"numeric_fields": {name: stats.payload() for name, stats in inventory_result.stats.items()},
		"schema_selection": selection,
		"source_precision_rule": "Statistics use exact Decimal values from valid source JSON before legacy four-decimal study normalization.",
	}
