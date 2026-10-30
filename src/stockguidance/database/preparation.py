"""Standalone, bounded preparation of versioned volume z-score materializations.

The operation is intentionally independent from training and provider code.  Its
pure calculation consumes an exported, typed input document; database apply is
guarded by the existing pending-operation and maintenance-lock contracts.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import Any, ContextManager, Mapping, cast
from uuid import uuid4

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.database.operation_recovery import DatabasePendingClearer, DatabaseReadinessGuard, ReadScope, ReadinessState, consistent_read_transaction
from stockguidance.database.repositories.simplified import PendingUpdateRepository
from stockguidance.database.simplified_contract import CacheInvalidation, ClearScope, OperationKind, PendingScope, UnitKind, WorkUnit
from stockguidance.database.simplified_schema import require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory
from stockguidance.market_data.identity import content_id, data_configuration_key, source_key
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import InputState, TickerSeries, ZScoreInput, ZScoreObservation
from stockguidance.zscores.results import ZScoreRow, ZScoreStatus
from stockguidance.zscores.specification import FLOAT32_PRECISION, ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.storage import ZScoreSeriesDefinition
from stockguidance.workflows.market_lock import MaintenanceWriterLock


PREPARATION_SCHEMA = "stockguidance-preparation-1"
VOLUME_CALCULATION_VERSION = "rolling-population-volume-zscore-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PreparationError(RuntimeError):
	"""A bounded preparation operation could not safely progress."""


class PreparationKind(StrEnum):
	VOLUME_ZSCORE = "volume_zscore"


class PreparationRoute(StrEnum):
	CODE = "code"
	SQL_INSPECTION = "sql_inspection"


class PreparationStatus(StrEnum):
	PLANNED = "planned"
	EXPORTED = "exported"
	CALCULATED = "calculated"
	APPLYING = "applying"
	APPLIED = "applied"
	VALIDATED = "validated"
	FAILED = "failed"
	INTERRUPTED = "interrupted"


def _sha(value: object, name: str) -> str:
	if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
		raise ValueError(f"{name} must be a lowercase SHA-256 digest")
	return value


def _utc(value: datetime, name: str) -> datetime:
	if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
		raise ValueError(f"{name} must be timezone-aware UTC")
	return value.astimezone(timezone.utc)


def _datetime_text(value: datetime) -> str:
	return _utc(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")


def _database_timestamp(value: datetime) -> datetime:
	"""Return a timezone-normalized naive value accepted by MySQL DATETIME."""
	return _utc(value, "timestamp").replace(tzinfo=None)


def _parse_datetime(value: object) -> datetime:
	if not isinstance(value, str) or not value.endswith("Z"):
		raise ValueError("Preparation timestamps must be RFC 3339 UTC")
	return _utc(datetime.fromisoformat(value[:-1] + "+00:00"), "timestamp")


def _positive_ids(values: Iterable[int], name: str) -> tuple[int, ...]:
	result = tuple(values)
	if not result or any(type(value) is not int or value < 1 for value in result) or len(set(result)) != len(result):
		raise ValueError(f"{name} must contain unique positive integers")
	return result


def _windows(values: Iterable[int]) -> tuple[int, ...]:
	result = tuple(values)
	if not result or any(type(value) is not int or value < 2 for value in result) or len(set(result)) != len(result):
		raise ValueError("Preparation windows must be distinct integers of at least 2")
	return result


@dataclass(frozen=True)
class VolumePreparationRequest:
	source_scope: str
	configuration_key: str
	calendar_id: str
	start_date: date
	end_date: date
	ticker_ids: tuple[int, ...]
	windows: tuple[int, ...] = (20, 60)
	batch_size: int = 1000
	route: PreparationRoute = PreparationRoute.CODE

	def __post_init__(self) -> None:
		_sha(self.source_scope, "source_scope")
		_sha(self.configuration_key, "configuration_key")
		if not self.calendar_id or not isinstance(self.start_date, date) or not isinstance(self.end_date, date) or self.start_date > self.end_date:
			raise ValueError("Invalid preparation calendar scope")
		_positive_ids(self.ticker_ids, "ticker_ids")
		_windows(self.windows)
		if type(self.batch_size) is not int or self.batch_size < 1:
			raise ValueError("batch_size must be positive")
		if not isinstance(self.route, PreparationRoute):
			raise ValueError("Invalid preparation route")

	def payload(self) -> dict[str, object]:
		return {
			"kind": PreparationKind.VOLUME_ZSCORE.value,
			"source_scope": self.source_scope,
			"configuration_key": self.configuration_key,
			"calendar_id": self.calendar_id,
			"start_date": self.start_date.isoformat(),
			"end_date": self.end_date.isoformat(),
			"ticker_ids": list(self.ticker_ids),
			"windows": list(self.windows),
			"batch_size": self.batch_size,
			"route": self.route.value,
		}

	@classmethod
	def from_payload(cls, value: object) -> "VolumePreparationRequest":
		if not isinstance(value, dict):
			raise ValueError("Preparation request must be an object")
		try:
			return cls(
				str(value["source_scope"]), str(value["configuration_key"]), str(value["calendar_id"]),
				date.fromisoformat(str(value["start_date"])), date.fromisoformat(str(value["end_date"])),
				tuple(int(cast(str | int, item)) for item in cast(list[object], value["ticker_ids"])),
				tuple(int(cast(str | int, item)) for item in cast(list[object], value["windows"])), int(cast(str | int, value["batch_size"])),
				PreparationRoute(str(value.get("route", PreparationRoute.CODE.value))),
			)
		except (KeyError, TypeError, ValueError):
			raise ValueError("Invalid preparation request") from None


@dataclass(frozen=True)
class VolumePreparationPlan:
	request: VolumePreparationRequest
	source_sessions: tuple[TradingSession, ...]
	dependency_sessions: tuple[TradingSession, ...]
	write_sessions: tuple[TradingSession, ...]
	volume_tables: tuple[tuple[int, str], ...]
	registry_digest: str
	plan_digest: str
	registry_tables: tuple[tuple[str, str], ...] = ()

	def __post_init__(self) -> None:
		if not self.source_sessions or not self.dependency_sessions or not self.write_sessions:
			raise ValueError("Preparation plan requires nonempty session scopes")
		if self.write_sessions[0].session_date != self.request.start_date or self.write_sessions[-1].session_date != self.request.end_date:
			raise ValueError("Preparation plan write scope does not match request")
		if any(session.calendar_id != self.request.calendar_id for session in (*self.source_sessions, *self.dependency_sessions, *self.write_sessions)):
			raise ValueError("Preparation sessions use the wrong calendar")
		if tuple(window for window, _ in self.volume_tables) != self.request.windows:
			raise ValueError("Preparation table windows do not match request")
		_sha(self.registry_digest, "registry_digest")
		_sha(self.plan_digest, "plan_digest")

	def payload(self) -> dict[str, object]:
		return {
			"request": self.request.payload(),
			"source_sessions": [_session_payload(item) for item in self.source_sessions],
			"dependency_sessions": [_session_payload(item) for item in self.dependency_sessions],
			"write_sessions": [_session_payload(item) for item in self.write_sessions],
			"volume_tables": [[window, table] for window, table in self.volume_tables],
			"registry_digest": self.registry_digest,
			"plan_digest": self.plan_digest,
			"registry_tables": [[role, table] for role, table in self.registry_tables],
		}

@dataclass(frozen=True)
class VolumeInputRow:
	date_id: int
	session_date: date
	ticker_id: int
	volume: Decimal | None
	state: InputState
	raw_complete: bool
	raw_revision: str | None

	def __post_init__(self) -> None:
		if self.date_id < 1 or self.ticker_id < 1 or self.session_date is None:
			raise ValueError("Invalid volume input identity")
		if self.volume is not None and (not self.volume.is_finite() or self.volume < 0):
			raise ValueError("Volume must be a finite nonnegative Decimal")
		if not isinstance(self.state, InputState) or type(self.raw_complete) is not bool:
			raise ValueError("Invalid volume input state")
		if self.raw_revision is not None:
			_sha(self.raw_revision, "raw_revision")

	def payload(self) -> dict[str, object]:
		return {
			"date_id": self.date_id, "session_date": self.session_date.isoformat(), "ticker_id": self.ticker_id,
			"volume": None if self.volume is None else str(self.volume), "state": self.state.value,
			"raw_complete": self.raw_complete, "raw_revision": self.raw_revision,
		}

	@classmethod
	def from_payload(cls, value: object) -> "VolumeInputRow":
		if not isinstance(value, dict):
			raise ValueError("Volume input row must be an object")
		try:
			volume = None if value["volume"] is None else Decimal(str(value["volume"]))
			return cls(int(value["date_id"]), date.fromisoformat(str(value["session_date"])), int(value["ticker_id"]), volume, InputState(str(value["state"])), bool(value["raw_complete"]), None if value["raw_revision"] is None else str(value["raw_revision"]))
		except (KeyError, TypeError, ValueError, InvalidOperation):
			raise ValueError("Invalid volume input row") from None


@dataclass(frozen=True)
class VolumeResultRow:
	series_key: str
	window: int
	date_id: int
	ticker_id: int
	session_date: date
	row: ZScoreRow
	dependency_digest: str
	availability_digest: str
	actual_input_available: bool
	computed_at: datetime

	def __post_init__(self) -> None:
		_sha(self.series_key, "series_key")
		if self.row.window != self.window or self.row.ticker_id != self.ticker_id or self.row.session.session_date != self.session_date:
			raise ValueError("Volume result row identity does not match its z-score row")
		_sha(self.dependency_digest, "dependency_digest")
		_sha(self.availability_digest, "availability_digest")
		_utc(self.computed_at, "computed_at")

	def payload(self) -> dict[str, object]:
		return {
			"series_key": self.series_key, "window": self.window, "date_id": self.date_id,
			"ticker_id": self.ticker_id, "session_date": self.session_date.isoformat(),
			"score": self.row.score, "status": int(self.row.status), "reason": self.row.reason,
			"dependency_digest": self.dependency_digest, "availability_digest": self.availability_digest,
			"actual_input_available": self.actual_input_available, "computed_at": _datetime_text(self.computed_at),
		}

	@classmethod
	def from_payload(cls, value: object, sessions: Mapping[date, TradingSession]) -> "VolumeResultRow":
		if not isinstance(value, dict):
			raise ValueError("Volume result row must be an object")
		try:
			day = date.fromisoformat(str(value["session_date"]))
			session = sessions[day]
			status = ZScoreStatus(int(value["status"]))
			score = None if value["score"] is None else float(value["score"])
			row = ZScoreRow(int(value["window"]), session, int(value["ticker_id"]), score, status, str(value["reason"]))
			return cls(str(value["series_key"]), int(value["window"]), int(value["date_id"]), int(value["ticker_id"]), day, row, str(value["dependency_digest"]), str(value["availability_digest"]), bool(value["actual_input_available"]), _parse_datetime(value["computed_at"]))
		except (KeyError, TypeError, ValueError):
			raise ValueError("Invalid volume result row") from None


@dataclass(frozen=True)
class VolumeCalculationResult:
	plan_digest: str
	input_digest: str
	rows: tuple[VolumeResultRow, ...]

	def __post_init__(self) -> None:
		_sha(self.plan_digest, "plan_digest")
		_sha(self.input_digest, "input_digest")
		if not self.rows:
			raise ValueError("Volume calculation produced no rows")

	def payload(self) -> dict[str, object]:
		return {"schema": "stockguidance-volume-zscore-results-1", "plan_digest": self.plan_digest, "input_digest": self.input_digest, "rows": [row.payload() for row in self.rows]}


def _session_payload(session: TradingSession) -> dict[str, object]:
	return {"calendar_id": session.calendar_id, "session_date": session.session_date.isoformat(), "sequence": session.sequence, "opens_at": _datetime_text(session.opens_at), "closes_at": _datetime_text(session.closes_at)}


def _session_from_payload(value: object) -> TradingSession:
	if not isinstance(value, dict):
		raise ValueError("Invalid preparation session")
	return TradingSession(str(value["calendar_id"]), date.fromisoformat(str(value["session_date"])), int(value["sequence"]), _parse_datetime(value["opens_at"]), _parse_datetime(value["closes_at"]))


def _registry_digest(table_names: TableNames) -> str:
	return content_id({"tables": table_names.target_tables(), "zscore_templates": table_names.templates(), "preparation_templates": table_names.preparation_templates()})


def plan_volume_zscore(request: VolumePreparationRequest, calendar: SessionCalendar, table_names: TableNames | None = None) -> VolumePreparationPlan:
	names = table_names or load_table_names()
	if request.calendar_id != calendar.calendar_id:
		raise PreparationError("Preparation calendar does not match the request")
	try:
		write_sessions = calendar.sessions(request.start_date, request.end_date)
		dependency_start = calendar.shift(write_sessions[0], -(max(request.windows) - 1))
		dependency_sessions = calendar.sessions(dependency_start.session_date, write_sessions[-1].session_date)
	except (IndexError, ValueError) as error:
		raise PreparationError("Preparation dependency scope exceeds the loaded exchange calendar") from error
	volume_tables = tuple((window, names.resolve_zscore_tables((window,))[window].volume) for window in request.windows)
	plan_payload = {"request": request.payload(), "dependency_dates": [item.session_date.isoformat() for item in dependency_sessions], "write_dates": [item.session_date.isoformat() for item in write_sessions], "volume_tables": volume_tables, "registry_digest": _registry_digest(names)}
	registry_tables = tuple(names.target_tables().items()) + (("volume_template", names.volume_template),) + tuple((f"volume_{window}", table) for window, table in volume_tables)
	return VolumePreparationPlan(request, dependency_sessions, dependency_sessions, write_sessions, volume_tables, _registry_digest(names), content_id(plan_payload), registry_tables)


def _input_digest(rows: Sequence[VolumeInputRow]) -> str:
	return content_id([row.payload() for row in sorted(rows, key=lambda item: (item.session_date, item.ticker_id))])


def calculate_volume_zscores(plan: VolumePreparationPlan, rows: Sequence[VolumeInputRow], computed_at: datetime) -> VolumeCalculationResult:
	if not isinstance(plan, VolumePreparationPlan):
		raise ValueError("Volume calculation requires a preparation plan")
	computed_at = _utc(computed_at, "computed_at")
	expected = {(session.session_date, ticker_id) for session in plan.dependency_sessions for ticker_id in plan.request.ticker_ids}
	by_key = {(row.session_date, row.ticker_id): row for row in rows}
	if set(by_key) != expected or len(by_key) != len(rows):
		raise PreparationError("Exported volume inputs do not cover the exact dependency scope")
	input_data = ZScoreInput(
		ZScoreFamily.VOLUME,
		plan.dependency_sessions,
		tuple(TickerSeries(ticker_id, tuple(_observation(by_key[(session.session_date, ticker_id)]) for session in plan.dependency_sessions)) for ticker_id in plan.request.ticker_ids),
		dependency_sessions=plan.dependency_sessions,
		scored_sessions=plan.write_sessions,
	)
	specification = ZScoreSpecification(ZScoreFamily.VOLUME, plan.request.windows, calculation_version=VOLUME_CALCULATION_VERSION, precision=FLOAT32_PRECISION)
	result = calculate_zscores(specification, input_data)
	date_ids = {row.session_date: row.date_id for row in rows}
	by_window: dict[int, ZScoreSeriesDefinition] = {window: ZScoreSeriesDefinition(plan.request.source_scope, plan.request.calendar_id, ZScoreFamily.VOLUME, window, VOLUME_CALCULATION_VERSION, FLOAT32_PRECISION, "not_applicable") for window in plan.request.windows}
	output: list[VolumeResultRow] = []
	for zscore_row in result.rows:
		window_inputs = tuple(by_key[(session.session_date, zscore_row.ticker_id)] for session in plan.dependency_sessions if zscore_row.session.sequence - window_offset(zscore_row.window) <= session.sequence <= zscore_row.session.sequence)
		if len(window_inputs) != zscore_row.window:
			# The calendar/session plan should make this unreachable; keep the failure explicit.
			raise PreparationError("Volume z-score dependency window is incomplete")
		dependency_digest = content_id([item.payload() for item in window_inputs])
		availability_digest = content_id([(item.session_date.isoformat(), item.ticker_id, item.state.value, item.raw_revision) for item in window_inputs])
		output.append(VolumeResultRow(by_window[zscore_row.window].series_key, zscore_row.window, date_ids[zscore_row.session.session_date], zscore_row.ticker_id, zscore_row.session.session_date, zscore_row, dependency_digest, availability_digest, all(item.state is InputState.AVAILABLE for item in window_inputs), computed_at))
	return VolumeCalculationResult(plan.plan_digest, _input_digest(rows), tuple(output))


def window_offset(window: int) -> int:
	return window - 1


def _observation(row: VolumeInputRow) -> ZScoreObservation:
	if row.state is InputState.AVAILABLE:
		return ZScoreObservation.ready(row.volume)
	if row.state is InputState.MISSING:
		return ZScoreObservation.missing()
	if row.state is InputState.UNVERIFIED:
		return ZScoreObservation.unverified(row.volume)
	if row.state is InputState.INVALID:
		return ZScoreObservation.invalid(row.volume)
	return ZScoreObservation.not_yet_available(row.volume)


def _db_text(value: object) -> str:
	return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _db_date(value: object) -> date:
	return value.date() if isinstance(value, datetime) else value if isinstance(value, date) else date.fromisoformat(_db_text(value))


def _db_bool(value: object) -> bool:
	if value in (0, False):
		return False
	if value in (1, True):
		return True
	raise PreparationError("Database returned an invalid completion flag")


def _db_int(value: object, name: str) -> int:
	try:
		result = int(str(value))
	except (TypeError, ValueError):
		raise PreparationError(f"Database returned an invalid {name}") from None
	if result < 1:
		raise PreparationError(f"Database returned an invalid {name}")
	return result


def _placeholders(values: Sequence[object]) -> str:
	return ",".join("%s" for _ in values)


def _assert_source_matches_plan(source: SourceConfig, plan: VolumePreparationPlan) -> None:
	if source.calendar_id != plan.request.calendar_id or source_key(source.identity) != plan.request.source_scope or data_configuration_key(source.data_configuration) != plan.request.configuration_key:
		raise PreparationError("Source identity or configuration does not match the persisted preparation plan")


def select_volume_inputs(source: SourceConfig, connect: WriteConnectionFactory, plan: VolumePreparationPlan, table_names: TableNames | None = None) -> tuple[VolumeInputRow, ...]:
	names = table_names or load_table_names()
	_assert_source_matches_plan(source, plan)
	connection = connect(source.database_config)
	cursor = connection.cursor()
	try:
		require_simplified_schema(cursor, names)
		sessions_table = quote_identifier(names.table(TableRole.SESSIONS))
		prices_table = quote_identifier(names.table(TableRole.PRICES))
		date_values = tuple(item.session_date for item in plan.dependency_sessions)
		cursor.execute(f"SELECT id, transaction_date, raw_complete, raw_revision FROM {sessions_table} WHERE transaction_date IN ({_placeholders(date_values)}) ORDER BY transaction_date", date_values)
		session_rows = cursor.fetchall()
		by_date: dict[date, tuple[int, bool, str | None]] = {}
		for row in session_rows:
			if len(row) != 4:
				raise PreparationError("Database returned an invalid session completion row")
			by_date[_db_date(row[1])] = (_db_int(row[0], "date_id"), _db_bool(row[2]), None if row[3] is None else _db_text(row[3]))
		if set(by_date) != set(date_values):
			raise PreparationError("Preparation dependency scope has missing session inventory")
		date_ids = tuple(value[0] for value in by_date.values())
		ticker_ids = plan.request.ticker_ids
		cursor.execute(f"SELECT date_id, ticker_id, volume, state_id FROM {prices_table} WHERE date_id IN ({_placeholders(date_ids)}) AND ticker_id IN ({_placeholders(ticker_ids)}) ORDER BY date_id, ticker_id", date_ids + ticker_ids)
		price_rows: dict[tuple[int, int], tuple[Decimal | None, int]] = {}
		for row in cursor.fetchall():
			if len(row) != 4:
				raise PreparationError("Database returned an invalid raw-volume row")
			volume = None if row[2] is None else Decimal(str(row[2]))
			price_rows[(_db_int(row[0], "date_id"), _db_int(row[1], "ticker_id"))] = (volume, _db_int(row[3], "state_id"))
		output: list[VolumeInputRow] = []
		for session in plan.dependency_sessions:
			date_id, raw_complete, revision = by_date[session.session_date]
			for ticker_id in ticker_ids:
				value = price_rows.get((date_id, ticker_id))
				if not raw_complete:
					state = InputState.UNVERIFIED
					volume = None if value is None else value[0]
				elif value is None or value[1] == 3:
					state, volume = InputState.MISSING, None if value is None else value[0]
				elif value[1] in (1, 4) and value[0] is not None:
					state, volume = InputState.AVAILABLE, value[0]
				else:
					state, volume = InputState.INVALID, None if value is None else value[0]
				output.append(VolumeInputRow(date_id, session.session_date, ticker_id, volume, state, raw_complete, revision))
		return tuple(output)
	except PreparationError:
		raise
	except Exception as error:
		raise PreparationError("Bounded volume input selection failed") from error
	finally:
		connection.rollback()
		cursor.close()
		connection.close()


def _atomic_json(path: Path, value: object) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
	temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False), encoding="utf-8")
	temporary.replace(path)


class PreparationStore:
	"""Atomic filesystem state for inspectable, resumable preparation attempts."""

	def __init__(self, root: Path = Path("database-preparations")) -> None:
		self.root = root

	def create(self, plan: VolumePreparationPlan, created_at: datetime) -> str:
		self.root.mkdir(parents=True, exist_ok=True)
		for _ in range(10):
			operation_id = f"prep-{uuid4().hex}"
			path = self.root / operation_id
			try:
				path.mkdir()
			except FileExistsError:
				continue
			manifest = {"schema": PREPARATION_SCHEMA, "operation_id": operation_id, "status": PreparationStatus.PLANNED.value, "created_at": _datetime_text(created_at), "updated_at": _datetime_text(created_at), "plan": plan.payload(), "source_revision": None, "input_digest": None, "result_digest": None, "failure": None}
			_atomic_json(path / "manifest.json", manifest)
			return operation_id
		raise PreparationError("Could not allocate a collision-free preparation operation")

	def path(self, operation_id: str) -> Path:
		if not re.fullmatch(r"prep-[0-9a-f]{32}", operation_id):
			raise PreparationError("Invalid preparation operation ID")
		return self.root / operation_id

	def read(self, operation_id: str) -> dict[str, Any]:
		path = self.path(operation_id) / "manifest.json"
		try:
			value = json.loads(path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			raise PreparationError("Preparation manifest is unavailable or corrupt") from None
		if not isinstance(value, dict) or value.get("schema") != PREPARATION_SCHEMA or value.get("operation_id") != operation_id:
			raise PreparationError("Preparation manifest is incompatible")
		return cast(dict[str, Any], value)

	def update(self, operation_id: str, **changes: object) -> dict[str, Any]:
		manifest = self.read(operation_id)
		manifest.update(changes)
		manifest["updated_at"] = _datetime_text(datetime.now(timezone.utc))
		_atomic_json(self.path(operation_id) / "manifest.json", manifest)
		return manifest


def _plan_from_payload(value: object) -> VolumePreparationPlan:
	if not isinstance(value, dict):
		raise ValueError("Invalid volume preparation plan")
	request = VolumePreparationRequest.from_payload(value["request"])
	try:
		return VolumePreparationPlan(request, tuple(_session_from_payload(item) for item in cast(list[object], value["source_sessions"])), tuple(_session_from_payload(item) for item in cast(list[object], value["dependency_sessions"])), tuple(_session_from_payload(item) for item in cast(list[object], value["write_sessions"])), tuple((int(cast(str | int, pair[0])), str(pair[1])) for pair in cast(list[list[object]], value["volume_tables"])), str(value["registry_digest"]), str(value["plan_digest"]), tuple((str(pair[0]), str(pair[1])) for pair in cast(list[list[object]], value.get("registry_tables", []))))
	except (KeyError, TypeError, ValueError):
		raise ValueError("Invalid volume preparation plan") from None


def _result_from_payload(value: object, plan: VolumePreparationPlan) -> VolumeCalculationResult:
	if not isinstance(value, dict) or value.get("schema") != "stockguidance-volume-zscore-results-1":
		raise ValueError("Invalid volume result document")
	sessions = {item.session_date: item for item in plan.dependency_sessions}
	return VolumeCalculationResult(str(value["plan_digest"]), str(value["input_digest"]), tuple(VolumeResultRow.from_payload(item, sessions) for item in cast(list[object], value["rows"])))


def write_inputs(path: Path, plan: VolumePreparationPlan, rows: Sequence[VolumeInputRow]) -> str:
	payload = {"schema": "stockguidance-volume-zscore-inputs-1", "plan_digest": plan.plan_digest, "columns": ["date_id", "session_date", "ticker_id", "volume", "state", "raw_complete", "raw_revision"], "rows": [row.payload() for row in rows]}
	_atomic_json(path, payload)
	return content_id(payload)


def read_inputs(path: Path, plan: VolumePreparationPlan) -> tuple[VolumeInputRow, ...]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
		if not isinstance(value, dict) or value.get("schema") != "stockguidance-volume-zscore-inputs-1" or value.get("plan_digest") != plan.plan_digest:
			raise ValueError
		return tuple(VolumeInputRow.from_payload(item) for item in cast(list[object], value["rows"]))
	except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
		raise PreparationError("Preparation input export is unavailable or incompatible") from None


def write_result(path: Path, result: VolumeCalculationResult) -> str:
	payload = result.payload()
	_atomic_json(path, payload)
	return content_id(payload)


def read_result(path: Path, plan: VolumePreparationPlan) -> VolumeCalculationResult:
	try:
		return _result_from_payload(json.loads(path.read_text(encoding="utf-8")), plan)
	except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
		raise PreparationError("Preparation result is unavailable or incompatible") from None


def _pending_scope(result: VolumeCalculationResult, plan: VolumePreparationPlan, requested_at: datetime) -> PendingScope:
	by_result: dict[tuple[str, int, int], list[VolumeResultRow]] = defaultdict(list)
	for row in result.rows:
		by_result[(row.series_key, row.window, row.date_id)].append(row)
	clear_rows = tuple(WorkUnit(UnitKind.ZSCORE_SESSION.value, row.date_id, row.ticker_id, row.session_date, row.series_key, row.window) for row in result.rows)
	completion = tuple(WorkUnit(UnitKind.ZSCORE_SESSION.value, date_id, None, rows[0].session_date, series_key, window) for (series_key, window, date_id), rows in sorted(by_result.items()))
	return PendingScope(1, OperationKind.DERIVED_REPAIR, plan.request.configuration_key, plan.request.calendar_id, requested_at, requested_at, (), ClearScope(zscores=clear_rows, completion=completion), completion, (CacheInvalidation("volume-zscore", tuple(sorted({row.session_date for row in result.rows}))),))


def _upsert(cursor: Any, table: str, key_fields: tuple[str, ...], fields: tuple[str, ...], values: tuple[object, ...]) -> None:
	key = " AND ".join(f"{quote_identifier(field)} = %s" for field in key_fields)
	cursor.execute(f"SELECT 1 FROM {quote_identifier(table)} WHERE {key}", tuple(values[fields.index(field)] for field in key_fields))
	if cursor.fetchall():
		assignments = ", ".join(f"{quote_identifier(field)} = %s" for field in fields if field not in key_fields)
		parameters = tuple(values[fields.index(field)] for field in fields if field not in key_fields) + tuple(values[fields.index(field)] for field in key_fields)
		cursor.execute(f"UPDATE {quote_identifier(table)} SET {assignments} WHERE {key}", parameters)
	else:
		columns = ", ".join(quote_identifier(field) for field in fields)
		cursor.execute(f"INSERT INTO {quote_identifier(table)} ({columns}) VALUES ({_placeholders(values)})", values)


def _apply_results(source: SourceConfig, connect: WriteConnectionFactory, plan: VolumePreparationPlan, result: VolumeCalculationResult, names: TableNames, computed_at: datetime) -> None:
	connection = connect(source.database_config)
	cursor = connection.cursor()
	try:
		require_simplified_schema(cursor, names)
		for window, table in plan.volume_tables:
			definition = ZScoreSeriesDefinition(plan.request.source_scope, plan.request.calendar_id, ZScoreFamily.VOLUME, window, VOLUME_CALCULATION_VERSION, FLOAT32_PRECISION, "not_applicable")
			series_table = names.table(TableRole.ZSCORE_SERIES)
			series_fields = ("series_key", "schema_version", "source_scope", "calendar_id", "family", "window", "calculation_version", "delta_definition", "precision", "missing_value_policy", "created_at")
			series_values = (definition.series_key, definition.schema_version, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy, _database_timestamp(computed_at))
			_upsert(cursor, series_table, ("series_key",), series_fields, series_values)
			for row in sorted((item for item in result.rows if item.window == window), key=lambda item: (item.date_id, item.ticker_id)):
				fields: tuple[str, ...] = ("date_id", "ticker_id", "zscore", "status", "quality_flags", "gap_length", "provenance")
				values: tuple[object, ...] = (row.date_id, row.ticker_id, row.row.score, int(row.row.status), int(row.row.quality), row.row.gap_length, json.dumps(list(row.row.provenance), separators=(",", ":")))
				_upsert(cursor, table, ("date_id", "ticker_id"), fields, values)
			by_date: dict[int, list[VolumeResultRow]] = defaultdict(list)
			for row in (item for item in result.rows if item.window == window):
				by_date[row.date_id].append(row)
			metadata_table = names.table(TableRole.ZSCORE_SESSIONS)
			for date_id, session_rows in by_date.items():
				status_counts = {str(int(status)): sum(item.row.status is status for item in session_rows) for status in ZScoreStatus}
				first = session_rows[0]
				fields = ("series_key", "date_id", "dependency_digest", "availability_digest", "actual_input_available", "completed_at", "row_count", "ready_count", "status_counts", "content_digest")
				metadata_values: tuple[object, ...] = (first.series_key, date_id, content_id(sorted(item.dependency_digest for item in session_rows)), content_id(sorted(item.availability_digest for item in session_rows)), all(item.actual_input_available for item in session_rows), _database_timestamp(computed_at), len(session_rows), sum(item.row.status is ZScoreStatus.READY for item in session_rows), json.dumps(status_counts, sort_keys=True, separators=(",", ":")), content_id([(item.ticker_id, item.row.score, int(item.row.status)) for item in sorted(session_rows, key=lambda item: item.ticker_id)]))
				_upsert(cursor, metadata_table, ("series_key", "date_id"), fields, metadata_values)
		connection.commit()
	except Exception:
		connection.rollback()
		raise
	finally:
		cursor.close()
		connection.close()


def _verify_source_unchanged(source: SourceConfig, connect: WriteConnectionFactory, plan: VolumePreparationPlan, expected: str, names: TableNames) -> None:
	current = select_volume_inputs(source, connect, plan, names)
	if _input_digest(current) != expected:
		raise PreparationError("Source changed between bounded export and apply; export again")


def apply_volume_result(source: SourceConfig, connect: WriteConnectionFactory, plan: VolumePreparationPlan, result: VolumeCalculationResult, *, table_names: TableNames | None = None, lock_factory: Callable[[], ContextManager[object]] | None = None, interrupt_after_clear: bool = False) -> None:
	names = table_names or load_table_names()
	_assert_source_matches_plan(source, plan)
	if result.plan_digest != plan.plan_digest:
		raise PreparationError("Preparation result belongs to a different plan")
	_verify_source_unchanged(source, connect, plan, result.input_digest, names)
	scope = _pending_scope(result, plan, datetime.now(timezone.utc))
	pending = PendingUpdateRepository(source, connect, names)
	clearer = DatabasePendingClearer(names)
	lock = lock_factory or (lambda: MaintenanceWriterLock(source, connect, timeout_seconds=60))
	with lock() as held:
		assert_held = getattr(held, "assert_held", None)
		if callable(assert_held):
			assert_held()
		active = pending.load_active()
		if active is not None:
			if set(active.scope.rebuild) != set(scope.rebuild) or active.scope.configuration_key != scope.configuration_key:
				raise PreparationError("Another pending database operation overlaps this preparation")
			operation = active
		else:
			operation = pending.register(scope, datetime.now(timezone.utc))
		if not operation.affected_rows_cleared:
			pending.begin_clearing(operation.operation_id, datetime.now(timezone.utc))
			connection = connect(source.database_config)
			try:
				if callable(assert_held):
					clearer.set_lock_checker(cast(Callable[[], None], assert_held))
				clearer.clear(operation, connection)
				pending.mark_cleared(operation.operation_id, datetime.now(timezone.utc))
			finally:
				clearer.set_lock_checker(None)
				connection.close()
		if interrupt_after_clear:
			raise PreparationError("Preparation was interrupted after durable clear; rerun resume")
		_apply_results(source, connect, plan, result, names, datetime.now(timezone.utc))
		pending.delete_completed(operation.operation_id, datetime.now(timezone.utc))


@dataclass(frozen=True)
class PreparationValidation:
	rows_checked: int
	metadata_checked: int
	ready_rows: int
	result_digest: str


def validate_volume_result(source: SourceConfig, connect: WriteConnectionFactory, plan: VolumePreparationPlan, result: VolumeCalculationResult, *, table_names: TableNames | None = None) -> PreparationValidation:
	names = table_names or load_table_names()
	if result.plan_digest != plan.plan_digest:
		raise PreparationError("Preparation result belongs to a different plan")
	units = tuple(WorkUnit(UnitKind.ZSCORE_SESSION.value, row.date_id, row.ticker_id, row.session_date, row.series_key, row.window) for row in result.rows)
	with consistent_read_transaction(source, connect) as transaction:
		guard = DatabaseReadinessGuard(names).check(ReadScope(units=units), transaction)
		if guard.status is ReadinessState.UNFINISHED_OPERATION:
			raise PreparationError("Validation is blocked by an overlapping pending database operation")
		cursor = transaction.cursor()
		expected_rows = {(row.window, row.date_id, row.ticker_id): row for row in result.rows}
		actual_rows: dict[tuple[int, int, int], tuple[float | None, int]] = {}
		for window, table in plan.volume_tables:
			date_ids = tuple(sorted({row.date_id for row in result.rows if row.window == window}))
			if not date_ids:
				continue
			cursor.execute(f"SELECT date_id, ticker_id, zscore, status FROM {quote_identifier(table)} WHERE date_id IN ({_placeholders(date_ids)}) AND ticker_id IN ({_placeholders(plan.request.ticker_ids)}) ORDER BY date_id, ticker_id", date_ids + plan.request.ticker_ids)
			for row in cursor.fetchall():
				key = (window, _db_int(row[0], "date_id"), _db_int(row[1], "ticker_id"))
				if key in actual_rows:
					raise PreparationError("Validation found duplicate volume z-score output")
				actual_rows[key] = (None if row[2] is None else float(str(row[2])), int(str(row[3])))
		if set(actual_rows) != set(expected_rows):
			raise PreparationError("Validation found missing or out-of-scope volume z-score rows")
		for key, expected in expected_rows.items():
			actual = actual_rows[key]
			if actual[1] != int(expected.row.status) or (expected.row.score is None and actual[0] is not None) or (expected.row.score is not None and (actual[0] is None or abs(actual[0] - expected.row.score) > 1e-6)):
				raise PreparationError("Volume z-score output does not match the reviewed calculation")
		metadata = quote_identifier(names.table(TableRole.ZSCORE_SESSIONS))
		metadata_keys = {(row.series_key, row.date_id) for row in result.rows}
		series_keys = tuple(sorted({key[0] for key in metadata_keys}))
		date_ids = tuple(sorted({key[1] for key in metadata_keys}))
		cursor.execute(f"SELECT series_key, date_id, row_count, ready_count, status_counts, content_digest FROM {metadata} WHERE series_key IN ({_placeholders(series_keys)}) AND date_id IN ({_placeholders(date_ids)})", series_keys + date_ids)
		metadata_rows = cursor.fetchall()
		if len(metadata_rows) != len(metadata_keys):
			raise PreparationError("Validation found missing or duplicate volume completion metadata")
		for row in metadata_rows:
			metadata_key = (_db_text(row[0]), _db_int(row[1], "date_id"))
			expected_for_key = tuple(item for item in result.rows if (item.series_key, item.date_id) == metadata_key)
			if metadata_key not in metadata_keys or int(str(row[2])) != len(expected_for_key) or int(str(row[3])) != sum(item.row.status is ZScoreStatus.READY for item in expected_for_key):
				raise PreparationError("Volume completion metadata does not match output rows")
		return PreparationValidation(len(actual_rows), len(metadata_rows), sum(row.row.status is ZScoreStatus.READY for row in result.rows), content_id([(key, actual_rows[key]) for key in sorted(actual_rows)]))


@dataclass(frozen=True)
class RenderedPreparationSql:
	step: str
	script: str
	result_columns: tuple[tuple[str, str], ...]
	read_only: bool


def render_volume_sql(plan: VolumePreparationPlan, step: str = "select", table_names: TableNames | None = None) -> RenderedPreparationSql:
	if step not in {"plan", "select", "calculate", "apply", "validate"}:
		raise ValueError("Unknown preparation SQL step")
	request = plan.request
	volume_tables = dict(plan.volume_tables)
	names = table_names or load_table_names()
	registry = dict(plan.registry_tables) or {**names.target_tables(), "volume_template": names.volume_template, **{f"volume_{window}": table for window, table in plan.volume_tables}}
	if table_names is not None:
		registry = {**table_names.target_tables(), "volume_template": table_names.volume_template, **{f"volume_{window}": table for window, table in plan.volume_tables}}
	sessions_name = registry["sessions"]
	tickers_name = registry["tickers"]
	prices_name = registry["prices"]
	ids = ", ".join(str(value) for value in request.ticker_ids)
	select_columns = "s.id AS date_id, s.transaction_date, t.id AS ticker_id, p.volume, p.state_id, s.raw_complete, s.raw_revision"
	scope = f"s.transaction_date BETWEEN '{plan.dependency_sessions[0].session_date.isoformat()}' AND '{plan.dependency_sessions[-1].session_date.isoformat()}' AND t.id IN ({ids})"
	base = f"FROM {quote_identifier(sessions_name)} s CROSS JOIN {quote_identifier(tickers_name)} t LEFT JOIN {quote_identifier(prices_name)} p ON p.date_id = s.id AND p.ticker_id = t.id WHERE {scope} ORDER BY s.transaction_date, t.id"
	header = "-- Rendered only: this command writes no database state.\n-- Edit only the bounded dates, ticker IDs, and result-file path.\n"
	if step == "plan":
		body = f"-- operation=volume_zscore plan={plan.plan_digest}\n-- dependency_start={plan.dependency_sessions[0].session_date.isoformat()} dependency_end={plan.dependency_sessions[-1].session_date.isoformat()}\n-- write_start={request.start_date.isoformat()} write_end={request.end_date.isoformat()} windows={','.join(str(item) for item in request.windows)} batch_size={request.batch_size}\nEXPLAIN SELECT {select_columns} {base};\n"
		return RenderedPreparationSql(step, header + body, (), True)
	if step == "select":
		body = f"SELECT {select_columns} {base};\n"
		return RenderedPreparationSql(step, header + body, (("date_id", "INT"), ("transaction_date", "DATE"), ("ticker_id", "INT"), ("volume", "DECIMAL"), ("state_id", "SMALLINT"), ("raw_complete", "BOOLEAN"), ("raw_revision", "CHAR(64)")), True)
	if step == "calculate":
		queries: list[str] = []
		for window in request.windows:
			queries.append(f"SELECT date_id, transaction_date, ticker_id, {window} AS `window`, CASE WHEN raw_complete = 0 THEN NULL WHEN valid_count < {window} THEN NULL WHEN invalid_count > 0 THEN NULL WHEN missing_count > 0 THEN NULL WHEN variance = 0 THEN NULL ELSE (volume - mean_value) / SQRT(variance) END AS zscore, CASE WHEN raw_complete = 0 THEN 3 WHEN valid_count < {window} THEN 1 WHEN invalid_count > 0 THEN 4 WHEN missing_count > 0 THEN 2 WHEN variance = 0 THEN 5 ELSE 0 END AS status FROM (SELECT scoped.*, COUNT(CASE WHEN raw_complete = 1 AND state_id IN (1,4) AND volume IS NOT NULL THEN 1 END) OVER (PARTITION BY ticker_id ORDER BY transaction_date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) AS valid_count, SUM(CASE WHEN raw_complete = 1 AND state_id = 2 THEN 1 ELSE 0 END) OVER (PARTITION BY ticker_id ORDER BY transaction_date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) AS invalid_count, SUM(CASE WHEN raw_complete = 1 AND (state_id = 3 OR state_id IS NULL) THEN 1 ELSE 0 END) OVER (PARTITION BY ticker_id ORDER BY transaction_date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) AS missing_count, AVG(CASE WHEN raw_complete = 1 AND state_id IN (1,4) THEN volume END) OVER (PARTITION BY ticker_id ORDER BY transaction_date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) AS mean_value, AVG(CASE WHEN raw_complete = 1 AND state_id IN (1,4) THEN volume * volume END) OVER (PARTITION BY ticker_id ORDER BY transaction_date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) - AVG(CASE WHEN raw_complete = 1 AND state_id IN (1,4) THEN volume END) OVER (PARTITION BY ticker_id ORDER BY transaction_date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) * AVG(CASE WHEN raw_complete = 1 AND state_id IN (1,4) THEN volume END) OVER (PARTITION BY ticker_id ORDER BY transaction_date ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW) AS variance FROM (SELECT {select_columns} {base}) scoped) calculated WHERE transaction_date BETWEEN '{request.start_date.isoformat()}' AND '{request.end_date.isoformat()}'" )
		return RenderedPreparationSql(step, header + "\nUNION ALL\n".join(queries) + ";\n", (("date_id", "INT"), ("transaction_date", "DATE"), ("ticker_id", "INT"), ("window", "INT"), ("zscore", "FLOAT NULL"), ("status", "TINYINT")), True)
	if step == "apply":
		body = "-- Calculation and status assignment remain in the pure Python reference path.\n-- The CSV is a reviewed export of results.json with the columns below.\n-- Run this import/upsert only inside the pending-operation/writer-lock wrapper\n-- described in docs/database_preparation.md; direct UPDATEs are not a valid\n-- completion path. The CLI apply/resume command supplies that wrapper.\nSET @reviewed_result_file = 'reviewed-volume-results.csv';\nCREATE TEMPORARY TABLE `stockguidance_volume_result_import` (series_key CHAR(64), date_id INT, ticker_id INT, window INT, zscore DOUBLE NULL, status TINYINT, dependency_digest CHAR(64), availability_digest CHAR(64), actual_input_available TINYINT, completed_at DATETIME(6), row_count INT, ready_count INT, status_counts JSON, content_digest CHAR(64));\n-- Replace the literal path below if the database interface does not permit\n-- LOAD DATA LOCAL INFILE variables.\nLOAD DATA LOCAL INFILE 'reviewed-volume-results.csv' INTO TABLE `stockguidance_volume_result_import` FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' IGNORE 1 LINES;\n"
		for window, table in volume_tables.items():
			body += f"INSERT INTO {quote_identifier(table)} (date_id, ticker_id, zscore, status, quality_flags, gap_length, provenance) SELECT date_id, ticker_id, zscore, status, 0, 0, '[]' FROM `stockguidance_volume_result_import` WHERE window = {window} ON DUPLICATE KEY UPDATE zscore = VALUES(zscore), status = VALUES(status), quality_flags = VALUES(quality_flags), gap_length = VALUES(gap_length), provenance = VALUES(provenance);\n"
		body += f"INSERT INTO {quote_identifier(registry['zscore_sessions'])} (series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest) SELECT series_key, date_id, MAX(dependency_digest), MAX(availability_digest), MIN(actual_input_available), MAX(completed_at), MAX(row_count), MAX(ready_count), MAX(status_counts), MAX(content_digest) FROM `stockguidance_volume_result_import` GROUP BY series_key, date_id ON DUPLICATE KEY UPDATE dependency_digest = VALUES(dependency_digest), availability_digest = VALUES(availability_digest), actual_input_available = VALUES(actual_input_available), completed_at = VALUES(completed_at), row_count = VALUES(row_count), ready_count = VALUES(ready_count), status_counts = VALUES(status_counts), content_digest = VALUES(content_digest);\n"
		body += "SELECT 'Lifecycle wrapper required before COMMIT' AS required_wrapper;\n"
		return RenderedPreparationSql(step, header + body, (("series_key", "CHAR(64)"), ("date_id", "INT"), ("ticker_id", "INT"), ("window", "INT"), ("zscore", "DOUBLE NULL"), ("status", "TINYINT")), False)
	body = f"SELECT series_key, date_id, row_count, ready_count, status_counts, content_digest FROM {quote_identifier(registry['zscore_sessions'])} WHERE date_id IN (SELECT id FROM {quote_identifier(sessions_name)} WHERE transaction_date BETWEEN '{plan.request.start_date.isoformat()}' AND '{plan.request.end_date.isoformat()}');\n"
	return RenderedPreparationSql(step, header + body, (), True)


__all__ = [
	"PREPARATION_SCHEMA", "VOLUME_CALCULATION_VERSION", "PreparationError", "PreparationKind", "PreparationRoute", "PreparationStatus", "PreparationStore", "RenderedPreparationSql", "VolumeCalculationResult", "VolumeInputRow", "VolumePreparationPlan", "VolumePreparationRequest", "VolumeResultRow", "apply_volume_result", "calculate_volume_zscores", "plan_volume_zscore", "read_inputs", "read_result", "render_volume_sql", "select_volume_inputs", "write_inputs", "write_result",
]
