"""Typed, database-neutral contracts for the staged replacement schema.

This module deliberately contains no database access and no update planning.
The durable scope is the Python/Java interchange format; later stages may add
the planner and runners without changing these value types.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import Any, Mapping, cast


SCHEMA_VERSION = 1
SCOPE_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CALCULATION_STATUSES = frozenset({
	"ready", "insufficient_history", "missing_input", "invalid_input",
	"not_yet_available", "zero_variance",
})
_REPAIR_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "transactions")


class ContractError(ValueError):
	"""Raised when a persisted replacement-schema value is not contractual."""


class OperationKind(StrEnum):
	INITIAL_IMPORT = "initial_import"
	FORCED_REPLACEMENT = "forced_replacement"
	DERIVED_REPAIR = "derived_repair"
	RECOVERY = "recovery"


class UnitKind(StrEnum):
	RAW_SESSION = "raw_session"
	REPAIR_ROW = "repair_row"
	DELTA_SESSION = "delta_session"
	ZSCORE_SESSION = "zscore_session"
	CACHE_REVISION = "cache_revision"


class DataState(StrEnum):
	GOOD = "good"
	BAD = "bad"
	MISSING = "missing"
	INTERPOLATED = "interpolated"


class CalculationStatus(StrEnum):
	READY = "ready"
	INSUFFICIENT_HISTORY = "insufficient_history"
	MISSING_INPUT = "missing_input"
	INVALID_INPUT = "invalid_input"
	NOT_YET_AVAILABLE = "not_yet_available"
	ZERO_VARIANCE = "zero_variance"


DATA_STATE_IDS: Mapping[DataState, int] = {
	DataState.GOOD: 1,
	DataState.BAD: 2,
	DataState.MISSING: 3,
	DataState.INTERPOLATED: 4,
}
DATA_STATE_BY_ID: Mapping[int, DataState] = {value: key for key, value in DATA_STATE_IDS.items()}


def _sha256(value: object, name: str) -> str:
	if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
		raise ContractError(f"{name} must be a lowercase SHA-256 hex string")
	return value


def _positive_id(value: object, name: str) -> int:
	if type(value) is not int or value <= 0:
		raise ContractError(f"{name} must be a positive integer")
	return value


def _utc(value: datetime, name: str) -> datetime:
	if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
		raise ContractError(f"{name} must be timezone-aware UTC")
	return value.astimezone(timezone.utc)


def _datetime_text(value: datetime, name: str) -> str:
	return _utc(value, name).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: object, name: str) -> datetime:
	if not isinstance(value, str) or not value.endswith("Z"):
		raise ContractError(f"{name} must be an RFC 3339 UTC timestamp")
	try:
		return _utc(datetime.fromisoformat(value[:-1] + "+00:00"), name)
	except ValueError:
		raise ContractError(f"{name} must be an RFC 3339 UTC timestamp") from None


def _date(value: object, name: str) -> date:
	if not isinstance(value, date) or isinstance(value, datetime):
		raise ContractError(f"{name} must be a date")
	return value


def _date_text(value: date, name: str) -> str:
	return _date(value, name).isoformat()


def _text(value: object, name: str) -> str:
	if not isinstance(value, str) or not value:
		raise ContractError(f"{name} must be nonempty text")
	return value


def _object(value: object, name: str) -> dict[str, Any]:
	if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
		raise ContractError(f"{name} must be an object")
	return cast(dict[str, Any], value)


def _array(value: object, name: str) -> list[Any]:
	if not isinstance(value, list):
		raise ContractError(f"{name} must be an array")
	return value


def _exact_fields(value: Mapping[str, object], expected: set[str], name: str) -> None:
	if set(value) != expected:
		raise ContractError(f"{name} fields are incomplete or unknown")


@dataclass(frozen=True)
class PreparedPayload:
	payload_id: str
	session_date: date
	temporary_path: Path
	sha256: str
	raw_id: str
	available_at: datetime
	prepared_at: datetime

	def __post_init__(self) -> None:
		_text(self.payload_id, "payload_id")
		_date(self.session_date, "session_date")
		if not isinstance(self.temporary_path, Path) or not str(self.temporary_path):
			raise ContractError("temporary_path must be a nonempty path")
		_sha256(self.sha256, "sha256")
		_sha256(self.raw_id, "raw_id")
		_utc(self.available_at, "available_at")
		_utc(self.prepared_at, "prepared_at")

	def to_json(self) -> dict[str, object]:
		return {
			"payload_id": self.payload_id,
			"session_date": _date_text(self.session_date, "session_date"),
			"temporary_path": str(self.temporary_path),
			"sha256": self.sha256,
			"raw_id": self.raw_id,
			"available_at": _datetime_text(self.available_at, "available_at"),
			"prepared_at": _datetime_text(self.prepared_at, "prepared_at"),
		}

	@classmethod
	def from_json(cls, value: object) -> "PreparedPayload":
		item = _object(value, "payload")
		_exact_fields(item, {"payload_id", "session_date", "temporary_path", "sha256", "raw_id", "available_at", "prepared_at"}, "payload")
		try:
			return cls(
				_text(item["payload_id"], "payload_id"), date.fromisoformat(_text(item["session_date"], "session_date")),
				Path(_text(item["temporary_path"], "temporary_path")), _sha256(item["sha256"], "sha256"),
				_sha256(item["raw_id"], "raw_id"), _parse_datetime(item["available_at"], "available_at"),
				_parse_datetime(item["prepared_at"], "prepared_at"),
			)
		except (TypeError, ValueError):
			raise ContractError("Invalid prepared payload") from None


@dataclass(frozen=True)
class WorkUnit:
	unit_kind: str
	date_id: int | None = None
	ticker_id: int | None = None
	session_date: date | None = None
	series_key: str | None = None
	window: int | None = None

	def __post_init__(self) -> None:
		try:
			kind = UnitKind(self.unit_kind)
		except ValueError:
			raise ContractError("Unknown work-unit kind") from None
		object.__setattr__(self, "unit_kind", kind.value)
		for value, name in ((self.date_id, "date_id"), (self.ticker_id, "ticker_id")):
			if value is not None:
				_positive_id(value, name)
		if self.session_date is not None:
			_date(self.session_date, "session_date")
		if self.series_key is not None:
			_sha256(self.series_key, "series_key")
		if self.window is not None and (type(self.window) is not int or self.window < 2):
			raise ContractError("window must be an integer of at least 2")
		if kind is UnitKind.RAW_SESSION and (self.date_id is None or self.session_date is None):
			raise ContractError("raw_session requires date_id and session_date")
		if kind is UnitKind.REPAIR_ROW and (self.date_id is None or self.ticker_id is None):
			raise ContractError("repair_row requires date_id and ticker_id")
		if kind is UnitKind.DELTA_SESSION and (self.date_id is None or self.session_date is None):
			raise ContractError("delta_session requires date_id and session_date")
		if kind is UnitKind.ZSCORE_SESSION and (self.date_id is None or self.session_date is None or self.series_key is None or self.window is None):
			raise ContractError("zscore_session requires date, series, and window identifiers")

	def to_json(self) -> dict[str, object]:
		return {
			"unit_kind": self.unit_kind,
			"date_id": self.date_id,
			"ticker_id": self.ticker_id,
			"session_date": None if self.session_date is None else _date_text(self.session_date, "session_date"),
			"series_key": self.series_key,
			"window": self.window,
		}

	@classmethod
	def from_json(cls, value: object) -> "WorkUnit":
		item = _object(value, "work unit")
		_exact_fields(item, {"unit_kind", "date_id", "ticker_id", "session_date", "series_key", "window"}, "work unit")
		session = item["session_date"]
		try:
			return cls(
				_text(item["unit_kind"], "unit_kind"),
				None if item["date_id"] is None else _positive_id(item["date_id"], "date_id"),
				None if item["ticker_id"] is None else _positive_id(item["ticker_id"], "ticker_id"),
				None if session is None else date.fromisoformat(_text(session, "session_date")),
				None if item["series_key"] is None else _sha256(item["series_key"], "series_key"),
				None if item["window"] is None else item["window"],
			)
		except (TypeError, ValueError):
			raise ContractError("Invalid work unit") from None


def _unique_units(units: tuple[WorkUnit, ...], name: str) -> None:
	keys = tuple(json.dumps(unit.to_json(), sort_keys=True, separators=(",", ":")) for unit in units)
	if len(set(keys)) != len(keys):
		raise ContractError(f"Duplicate {name} work unit")


@dataclass(frozen=True)
class ClearScope:
	raw: tuple[WorkUnit, ...] = ()
	repairs: tuple[WorkUnit, ...] = ()
	deltas: tuple[WorkUnit, ...] = ()
	zscores: tuple[WorkUnit, ...] = ()
	completion: tuple[WorkUnit, ...] = ()

	def __post_init__(self) -> None:
		for name in ("raw", "repairs", "deltas", "zscores", "completion"):
			units = getattr(self, name)
			if not isinstance(units, tuple) or not all(isinstance(unit, WorkUnit) for unit in units):
				raise ContractError(f"{name} clear scope must contain work units")
			_unique_units(units, name)

	def to_json(self) -> dict[str, object]:
		return {name: [unit.to_json() for unit in getattr(self, name)] for name in ("raw", "repairs", "deltas", "zscores", "completion")}

	@classmethod
	def from_json(cls, value: object) -> "ClearScope":
		item = _object(value, "clear scope")
		_names = ("raw", "repairs", "deltas", "zscores", "completion")
		_exact_fields(item, set(_names), "clear scope")
		return cls(**{name: tuple(WorkUnit.from_json(unit) for unit in _array(item[name], f"clear.{name}")) for name in _names})


@dataclass(frozen=True)
class CacheInvalidation:
	revision_scope: str
	session_dates: tuple[date, ...]

	def __post_init__(self) -> None:
		_text(self.revision_scope, "revision_scope")
		if not isinstance(self.session_dates, tuple) or not all(isinstance(value, date) and not isinstance(value, datetime) for value in self.session_dates):
			raise ContractError("session_dates must contain dates")
		if len(set(self.session_dates)) != len(self.session_dates):
			raise ContractError("Duplicate cache invalidation date")

	def to_json(self) -> dict[str, object]:
		return {"revision_scope": self.revision_scope, "session_dates": [_date_text(value, "session_date") for value in self.session_dates]}

	@classmethod
	def from_json(cls, value: object) -> "CacheInvalidation":
		item = _object(value, "cache invalidation")
		_exact_fields(item, {"revision_scope", "session_dates"}, "cache invalidation")
		try:
			return cls(_text(item["revision_scope"], "revision_scope"), tuple(date.fromisoformat(_text(value, "session_date")) for value in _array(item["session_dates"], "session_dates")))
		except (TypeError, ValueError):
			raise ContractError("Invalid cache invalidation") from None


@dataclass(frozen=True)
class PendingScope:
	scope_version: int
	operation_kind: OperationKind
	configuration_key: str
	calendar_id: str
	requested_at: datetime
	cutoff: datetime
	payloads: tuple[PreparedPayload, ...]
	clear: ClearScope
	rebuild: tuple[WorkUnit, ...]
	cache_invalidations: tuple[CacheInvalidation, ...]

	def __post_init__(self) -> None:
		if type(self.scope_version) is not int or self.scope_version != SCOPE_VERSION:
			raise ContractError("Unsupported pending-scope version")
		if not isinstance(self.operation_kind, OperationKind):
			raise ContractError("Invalid operation kind")
		_sha256(self.configuration_key, "configuration_key")
		_text(self.calendar_id, "calendar_id")
		_utc(self.requested_at, "requested_at")
		_utc(self.cutoff, "cutoff")
		if not isinstance(self.payloads, tuple) or not all(isinstance(value, PreparedPayload) for value in self.payloads):
			raise ContractError("payloads must contain prepared payloads")
		if len({value.payload_id for value in self.payloads}) != len(self.payloads):
			raise ContractError("Duplicate payload_id")
		if not isinstance(self.clear, ClearScope):
			raise ContractError("clear must be a ClearScope")
		if not isinstance(self.rebuild, tuple) or not all(isinstance(value, WorkUnit) for value in self.rebuild):
			raise ContractError("rebuild must contain work units")
		_unique_units(self.rebuild, "rebuild")
		if not isinstance(self.cache_invalidations, tuple) or not all(isinstance(value, CacheInvalidation) for value in self.cache_invalidations):
			raise ContractError("cache_invalidations must contain cache invalidations")

	def to_json_object(self) -> dict[str, object]:
		return {
			"scope_version": self.scope_version,
			"operation_kind": self.operation_kind.value,
			"configuration_key": self.configuration_key,
			"calendar_id": self.calendar_id,
			"requested_at": _datetime_text(self.requested_at, "requested_at"),
			"cutoff": _datetime_text(self.cutoff, "cutoff"),
			"payloads": [value.to_json() for value in self.payloads],
			"clear": self.clear.to_json(),
			"rebuild": [value.to_json() for value in self.rebuild],
			"cache_invalidations": [value.to_json() for value in self.cache_invalidations],
		}

	def to_json(self) -> str:
		return json.dumps(self.to_json_object(), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)

	@classmethod
	def from_json(cls, value: str | bytes | bytearray | object) -> "PendingScope":
		try:
			decoded = json.loads(value, object_pairs_hook=_reject_duplicate_json_fields) if isinstance(value, (str, bytes, bytearray)) else value
			item = _object(decoded, "pending scope")
			_exact_fields(item, {"scope_version", "operation_kind", "configuration_key", "calendar_id", "requested_at", "cutoff", "payloads", "clear", "rebuild", "cache_invalidations"}, "pending scope")
			return cls(
				item["scope_version"], OperationKind(_text(item["operation_kind"], "operation_kind")), _sha256(item["configuration_key"], "configuration_key"),
				_text(item["calendar_id"], "calendar_id"), _parse_datetime(item["requested_at"], "requested_at"), _parse_datetime(item["cutoff"], "cutoff"),
				tuple(PreparedPayload.from_json(value) for value in _array(item["payloads"], "payloads")), ClearScope.from_json(item["clear"]),
				tuple(WorkUnit.from_json(value) for value in _array(item["rebuild"], "rebuild")),
				tuple(CacheInvalidation.from_json(value) for value in _array(item["cache_invalidations"], "cache_invalidations")),
			)
		except (TypeError, ValueError, json.JSONDecodeError):
			raise ContractError("Invalid pending scope") from None


def _reject_duplicate_json_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
	result: dict[str, Any] = {}
	for key, value in pairs:
		if key in result:
			raise ContractError("Duplicate JSON object field")
		result[key] = value
	return result


@dataclass(frozen=True)
class FailureDetail:
	code: str
	detail: str
	failed_at: datetime

	def __post_init__(self) -> None:
		_text(self.code, "failure code")
		_text(self.detail, "failure detail")
		_utc(self.failed_at, "failed_at")


@dataclass(frozen=True)
class PendingUpdate:
	operation_id: int
	scope: PendingScope
	created_at: datetime
	clearing_started_at: datetime | None
	affected_rows_cleared: bool
	failure: FailureDetail | None

	def __post_init__(self) -> None:
		_positive_id(self.operation_id, "operation_id")
		if not isinstance(self.scope, PendingScope):
			raise ContractError("scope must be a PendingScope")
		_utc(self.created_at, "created_at")
		if self.clearing_started_at is not None:
			_utc(self.clearing_started_at, "clearing_started_at")
		if type(self.affected_rows_cleared) is not bool:
			raise ContractError("affected_rows_cleared must be boolean")
		if self.failure is not None and not isinstance(self.failure, FailureDetail):
			raise ContractError("failure must be FailureDetail or None")


@dataclass(frozen=True)
class SessionCompletion:
	date_id: int
	transaction_date: date
	raw_complete: bool
	raw_available_at: datetime | None
	raw_completed_at: datetime | None
	raw_revision: str | None
	delta_complete: bool
	delta_completed_at: datetime | None

	def __post_init__(self) -> None:
		_positive_id(self.date_id, "date_id")
		_date(self.transaction_date, "transaction_date")
		for boolean_value, name in ((self.raw_complete, "raw_complete"), (self.delta_complete, "delta_complete")):
			if type(boolean_value) is not bool:
				raise ContractError(f"{name} must be boolean")
		timestamps: tuple[tuple[datetime | None, str], ...] = ((self.raw_available_at, "raw_available_at"), (self.raw_completed_at, "raw_completed_at"), (self.delta_completed_at, "delta_completed_at"))
		for timestamp_value, name in timestamps:
			if timestamp_value is not None:
				_utc(timestamp_value, name)
		if self.raw_revision is not None:
			_sha256(self.raw_revision, "raw_revision")
		if self.raw_complete and (self.raw_completed_at is None or self.raw_revision is None):
			raise ContractError("completed raw sessions require completion time and revision")
		if self.delta_complete and self.delta_completed_at is None:
			raise ContractError("completed delta sessions require completion time")


@dataclass(frozen=True)
class SourceConfigurationRecord:
	configuration_key: str
	source_key: str
	source_family: str
	endpoint: str
	market: str
	universe: str
	interval_code: str
	calendar_id: str
	raw_price_split_adjusted: bool
	raw_price_dividend_adjusted: bool
	raw_price_vendor_defined_adjustment: str | None
	created_at: datetime

	def __post_init__(self) -> None:
		_sha256(self.configuration_key, "configuration_key")
		_sha256(self.source_key, "source_key")
		for text_value, name in ((self.source_family, "source_family"), (self.endpoint, "endpoint"), (self.market, "market"), (self.universe, "universe"), (self.interval_code, "interval_code"), (self.calendar_id, "calendar_id")):
			_text(text_value, name)
		boolean_values: tuple[tuple[bool, str], ...] = ((self.raw_price_split_adjusted, "raw_price_split_adjusted"), (self.raw_price_dividend_adjusted, "raw_price_dividend_adjusted"))
		for boolean_value, name in boolean_values:
			if type(boolean_value) is not bool:
				raise ContractError(f"{name} must be boolean")
		if self.raw_price_vendor_defined_adjustment is not None:
			_text(self.raw_price_vendor_defined_adjustment, "raw_price_vendor_defined_adjustment")
		_utc(self.created_at, "created_at")


@dataclass(frozen=True)
class RepairMetadata:
	date_id: int
	ticker_id: int
	revision: str
	state: DataState
	original_values: tuple[Decimal | str | None, ...]
	effective_values: tuple[Decimal | None, ...] | None
	invalid_fields: tuple[str, ...]
	interpolation_method: str | None
	prior_date_id: int | None
	following_date_id: int | None
	endpoint_values: tuple[tuple[Decimal | None, ...], tuple[Decimal | None, ...]] | None
	observed_at: datetime | None
	available_at: datetime | None
	endpoints_available_at: datetime | None
	recorded_at: datetime

	def __post_init__(self) -> None:
		_positive_id(self.date_id, "date_id")
		_positive_id(self.ticker_id, "ticker_id")
		_sha256(self.revision, "revision")
		if not isinstance(self.state, DataState):
			raise ContractError("state must be a DataState")
		if len(self.original_values) != len(_REPAIR_FIELDS):
			raise ContractError("original_values must contain seven fields")
		if self.effective_values is not None and len(self.effective_values) != len(_REPAIR_FIELDS):
			raise ContractError("effective_values must contain seven fields")
		if any(field not in _REPAIR_FIELDS for field in self.invalid_fields) or len(set(self.invalid_fields)) != len(self.invalid_fields):
			raise ContractError("invalid_fields contains an unknown or duplicate field")
		if self.state is DataState.INTERPOLATED:
			if self.interpolation_method != "linear-trading-session-v1" or self.prior_date_id is None or self.following_date_id is None or self.endpoint_values is None:
				raise ContractError("interpolated repair metadata requires endpoints and method")
		else:
			if self.interpolation_method is not None or self.endpoint_values is not None:
				raise ContractError("only interpolated metadata may contain endpoint information")
		for id_value, name in ((self.prior_date_id, "prior_date_id"), (self.following_date_id, "following_date_id")):
			if id_value is not None:
				_positive_id(id_value, name)
		if self.endpoint_values is not None and any(len(values) != len(_REPAIR_FIELDS) for values in self.endpoint_values):
			raise ContractError("endpoint_values must contain two seven-field arrays")
		timestamps: tuple[tuple[datetime | None, str], ...] = ((self.observed_at, "observed_at"), (self.available_at, "available_at"), (self.endpoints_available_at, "endpoints_available_at"), (self.recorded_at, "recorded_at"))
		for timestamp_value, name in timestamps:
			if timestamp_value is not None:
				_utc(timestamp_value, name)

	def original_json(self) -> list[str | None]:
		return [None if value is None else str(value) for value in self.original_values]

	def effective_json(self) -> list[str | None] | None:
		return None if self.effective_values is None else [None if value is None else str(value) for value in self.effective_values]

	def endpoint_json(self) -> list[list[str | None]] | None:
		return None if self.endpoint_values is None else [[None if value is None else str(value) for value in values] for values in self.endpoint_values]
