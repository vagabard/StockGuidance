"""Stage 7 numerical rebuilds for the simplified operation scope.

The runner in this module is deliberately an adapter around the existing
derived-row and z-score kernels.  It owns only target-schema I/O, dependency
evidence, and operation ordering; it does not introduce a second formula
implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from typing import Callable, cast

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.database.operation_recovery import RebuildResult
from stockguidance.database.raw_rebuild import RawRebuildRunner
from stockguidance.database.protocols import Cursor
from stockguidance.database.simplified_contract import (
	CalculationStatus,
	DataState,
	PendingUpdate,
	UnitKind,
	WorkUnit,
)
from stockguidance.database.simplified_schema import require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnection
from stockguidance.market_data.identity import content_id, data_configuration_key
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, legacy_rows
from stockguidance.market_data.quality import QualityFlag, QualityPolicy
from stockguidance.market_data.records import DailyBar, count_value, decimal_price, decimal_quantity, decimal_vwap
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import InputState, TickerSeries, ZScoreInput, ZScoreObservation
from stockguidance.zscores.results import ZScoreRow, ZScoreStatus
from stockguidance.zscores.specification import ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.storage import ZScoreSeriesDefinition, rows_content_id


_RAW_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "transactions")
_STATUS_PRIORITY: dict[str, int] = {
	CalculationStatus.READY.value: 0,
	CalculationStatus.INSUFFICIENT_HISTORY.value: 1,
	CalculationStatus.MISSING_INPUT.value: 2,
	CalculationStatus.NOT_YET_AVAILABLE.value: 3,
	CalculationStatus.INVALID_INPUT.value: 4,
}


class NumericalRebuildError(RuntimeError):
	"""Raised when target-schema numerical evidence is unsafe to rebuild."""


@dataclass(frozen=True)
class _Session:
	date_id: int
	transaction_date: date
	sequence: int
	raw_complete: bool
	raw_available_at: datetime | None
	raw_revision: str | None
	delta_complete: bool


@dataclass(frozen=True)
class _Raw:
	date_id: int
	ticker_id: int
	label: str
	state: DataState
	values: tuple[object | None, ...]
	observed_at: datetime | None
	available_at: datetime | None
	raw_revision: str | None
	repair_revision: str | None
	prior_date_id: int | None
	following_date_id: int | None


@dataclass(frozen=True)
class _Repair:
	revision: str
	prior_date_id: int | None
	following_date_id: int | None
	available_at: datetime | None


@dataclass(frozen=True)
class _DeltaRow:
	ticker_id: int
	values: tuple[float | None, ...]
	status: str
	quality: QualityFlag
	gap_length: int
	provenance: tuple[str, ...]


def _placeholders(values: Sequence[object]) -> str:
	if not values:
		raise NumericalRebuildError("A bounded numerical query requires values")
	return ",".join("%s" for _ in values)


def _db_id(value: object, name: str) -> int:
	if isinstance(value, bytes):
		value = value.decode("ascii")
	if isinstance(value, str) and value.isdecimal():
		value = int(value)
	if type(value) is not int or value <= 0:
		raise NumericalRebuildError(f"Invalid database {name}")
	return value


def _db_date(value: object, name: str = "date") -> date:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	if isinstance(value, str):
		try:
			return date.fromisoformat(value)
		except ValueError:
			pass
	raise NumericalRebuildError(f"Invalid database {name}")


def _db_datetime(value: object | None, name: str) -> datetime | None:
	if value is None:
		return None
	if isinstance(value, datetime):
		parsed = value
	elif isinstance(value, str):
		try:
			parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
		except ValueError as error:
			raise NumericalRebuildError(f"Invalid database {name}") from error
	else:
		raise NumericalRebuildError(f"Invalid database {name}")
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=timezone.utc)
	return parsed.astimezone(timezone.utc)


def _db_bool(value: object, name: str) -> bool:
	if value in (0, False):
		return False
	if value in (1, True):
		return True
	raise NumericalRebuildError(f"Invalid database {name}")


def _state(value: object) -> DataState:
	identifier = _db_id(value, "state_id")
	for candidate, state_id in ((DataState.GOOD, 1), (DataState.BAD, 2), (DataState.MISSING, 3), (DataState.INTERPOLATED, 4)):
		if identifier == state_id:
			return candidate
	raise NumericalRebuildError(f"Unknown database state: {identifier}")


def _float(value: object | None, name: str) -> float | None:
	if value is None or isinstance(value, bool):
		return None
	try:
		result = float(str(value))
	except (TypeError, ValueError, OverflowError) as error:
		raise NumericalRebuildError(f"Invalid numerical value in {name}") from error
	return result if math.isfinite(result) else None


def _integer(value: object, name: str) -> int:
	if isinstance(value, bool):
		raise NumericalRebuildError(f"Invalid integer in {name}")
	try:
		result = int(str(value))
	except (TypeError, ValueError, OverflowError) as error:
		raise NumericalRebuildError(f"Invalid integer in {name}") from error
	return result


def _json(value: object, name: str) -> object:
	if isinstance(value, bytes):
		value = value.decode("utf-8")
	if isinstance(value, str):
		try:
			return json.loads(value)
		except json.JSONDecodeError as error:
			raise NumericalRebuildError(f"Invalid JSON in {name}") from error
	return value


def _provenance_values(value: object, name: str) -> tuple[str, ...]:
	decoded = _json(value, name)
	if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
		raise NumericalRebuildError(f"{name} must be a JSON string array")
	return tuple(sorted(set(cast(list[str], decoded))))


def _decimal(value: object | None, name: str) -> Decimal | None:
	if value is None:
		return None
	try:
		result = value if isinstance(value, Decimal) else Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError) as error:
		raise NumericalRebuildError(f"Invalid decimal in {name}") from error
	return result if result.is_finite() else None


def _bar(raw: _Raw, cutoff: datetime) -> DailyBar | None:
	if raw.state not in (DataState.GOOD, DataState.INTERPOLATED) or raw.available_at is None or raw.available_at > cutoff:
		return None
	if len(raw.values) != 7:
		return None
	try:
		open_value, high, low, close, volume, vwap, transactions = raw.values
		if any(value is None for value in (open_value, high, low, close, volume)):
			return None
		return DailyBar(
			raw.label,
			decimal_price(open_value),
			decimal_price(high),
			decimal_price(low),
			decimal_price(close),
			decimal_quantity(volume),
			None if vwap is None else decimal_vwap(vwap),
			None if transactions is None else count_value(transactions),
		)
	except (TypeError, ValueError, InvalidOperation):
		return None


def _quality(raw: _Raw, sessions: Mapping[int, _Session]) -> tuple[QualityFlag, int, tuple[str, ...]]:
	if raw.state is DataState.GOOD:
		flags = QualityFlag.CLEAN
		gap = 0
	else:
		flags = QualityFlag.BAD
		gap = 0
		if raw.state is DataState.INTERPOLATED:
			prior = sessions.get(raw.prior_date_id or 0)
			following = sessions.get(raw.following_date_id or 0)
			if prior is not None and following is not None:
				gap = max(1, following.sequence - prior.sequence - 1)
			flags |= QualityFlag.INTERPOLATED_SINGLE if gap <= 1 else QualityFlag.INTERPOLATED_GAP
	parts = [f"raw:{raw.date_id}:{raw.ticker_id}:{raw.state.value}"]
	if raw.raw_revision is not None:
		parts.append(f"source_revision:{raw.raw_revision}")
	if raw.repair_revision is not None:
		parts.append(f"repair_revision:{raw.repair_revision}")
	if raw.available_at is not None:
		parts.append(f"available_at:{raw.available_at.isoformat()}")
	return flags, gap, tuple(parts)


def _status_for_raw(raw: _Raw | None, cutoff: datetime, *, absent: str) -> str:
	if raw is None:
		return absent
	if raw.state is DataState.BAD:
		return CalculationStatus.INVALID_INPUT.value
	if raw.state is DataState.MISSING:
		return CalculationStatus.MISSING_INPUT.value
	if raw.available_at is None or raw.available_at > cutoff:
		return CalculationStatus.NOT_YET_AVAILABLE.value
	if raw.state is DataState.INTERPOLATED and _bar(raw, cutoff) is None:
		return CalculationStatus.INVALID_INPUT.value
	return CalculationStatus.READY.value


def _combine_status(values: Iterable[str]) -> str:
	selected = CalculationStatus.READY.value
	for value in values:
		if _STATUS_PRIORITY[value] > _STATUS_PRIORITY[selected]:
			selected = value
	return selected


def _safe_ratio(value: Decimal | None, denominator: Decimal | None) -> float | None:
	if value is None or denominator in (None, Decimal(0)):
		return None
	result = value / denominator
	return float(result) if result.is_finite() else None


def _repair_map(cursor: Cursor, date_ids: tuple[int, ...], table_names: TableNames) -> dict[tuple[int, int], _Repair]:
	if not date_ids:
		return {}
	table = quote_identifier(table_names.table(TableRole.REPAIR_METADATA))
	cursor.execute(
		f"SELECT date_id, ticker_id, revision, prior_date_id, following_date_id, available_at FROM {table} WHERE date_id IN ({_placeholders(date_ids)}) ORDER BY date_id, ticker_id, recorded_at, id",
		date_ids,
	)
	result: dict[tuple[int, int], _Repair] = {}
	for row in cursor.fetchall():
		if len(row) != 6:
			raise NumericalRebuildError("Invalid repair metadata row")
		key = (_db_id(row[0], "repair date_id"), _db_id(row[1], "repair ticker_id"))
		if key in result:
			continue
		result[key] = _Repair(
			str(row[2]),
			None if row[3] is None else _db_id(row[3], "prior_date_id"),
			None if row[4] is None else _db_id(row[4], "following_date_id"),
			_db_datetime(row[5], "repair available_at"),
		)
	return result


class SimplifiedNumericalRebuildRunner:
	"""Compose raw rebuilding with atomic delta and z-score unit writers."""

	def __init__(
		self,
		source: SourceConfig,
		calendar: SessionCalendar,
		raw_runner: RawRebuildRunner | None = None,
		*,
		table_names: TableNames | None = None,
		quality_policy: QualityPolicy = QualityPolicy(),
		clock: Callable[[], datetime] | None = None,
	) -> None:
		if calendar.calendar_id != source.calendar_id:
			raise ValueError("Numerical rebuild calendar does not match source configuration")
		self.source = source
		self.calendar = calendar
		self.table_names = table_names or load_table_names()
		self.raw_runner = raw_runner or RawRebuildRunner(source, calendar, table_names=self.table_names)
		self.quality_policy = quality_policy
		self.clock = clock or (lambda: datetime.now(timezone.utc))

	def rebuild(self, operation: PendingUpdate, connection: WriteConnection) -> RebuildResult:
		if not isinstance(operation, PendingUpdate):
			raise ValueError("operation must be a PendingUpdate")
		if not operation.affected_rows_cleared:
			raise NumericalRebuildError("Numerical rebuilding is unreachable before affected_rows_cleared")
		schema_cursor = self._connection_cursor(connection)
		try:
			require_simplified_schema(schema_cursor, self.table_names)
		finally:
			schema_cursor.close()
		raw_result = self.raw_runner.rebuild(operation, connection)
		committed = list(raw_result.committed_units)
		non_ready = list(raw_result.explicit_non_ready)
		for unit in operation.scope.rebuild:
			kind = UnitKind(unit.unit_kind)
			if kind is UnitKind.DELTA_SESSION:
				if self._rebuild_delta(unit, operation, connection):
					committed.append(unit)
					if self._delta_is_non_ready(unit, operation, connection):
						non_ready.append(unit)
		for unit in operation.scope.rebuild:
			if UnitKind(unit.unit_kind) is UnitKind.ZSCORE_SESSION:
				row = self._rebuild_zscore(unit, operation, connection)
				if row is not None:
					committed.append(unit)
					if row.status is not ZScoreStatus.READY:
						non_ready.append(unit)
		return RebuildResult(_unique_units(committed), _unique_units(non_ready), (), ())

	def _rebuild_delta(self, unit: WorkUnit, operation: PendingUpdate, connection: WriteConnection) -> bool:
		return self._write_delta_unit(unit, operation, connection) is not None

	def _rebuild_zscore(self, unit: WorkUnit, operation: PendingUpdate, connection: WriteConnection) -> ZScoreRow | None:
		return self._zscore_row_for_unit(unit, operation, connection)

	def _connection_cursor(self, connection: WriteConnection) -> Cursor:
		return cast(Cursor, connection.cursor())

	def _load_sessions(self, cursor: Cursor, dates: Sequence[date]) -> dict[date, _Session]:
		if not dates:
			return {}
		table = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		unique_dates = tuple(dict.fromkeys(dates))
		cursor.execute(
			f"SELECT id, transaction_date, raw_complete, raw_available_at, raw_revision, delta_complete FROM {table} WHERE transaction_date IN ({_placeholders(unique_dates)}) ORDER BY transaction_date, id",
			unique_dates,
		)
		result: dict[date, _Session] = {}
		for row in cursor.fetchall():
			if len(row) != 6:
				raise NumericalRebuildError("Invalid transaction-date completion row")
			session_date = _db_date(row[1], "transaction_date")
			if session_date in result:
				raise NumericalRebuildError("Duplicate transaction-date mapping")
			session = self._session(session_date)
			result[session_date] = _Session(
				_db_id(row[0], "date_id"), session_date, session.sequence,
				_db_bool(row[2], "raw_complete"), _db_datetime(row[3], "raw_available_at"),
				None if row[4] is None else str(row[4]), _db_bool(row[5], "delta_complete"),
			)
		return result

	def _load_raw(
		self,
		cursor: Cursor,
		date_ids: Sequence[int],
		sessions_by_id: Mapping[int, _Session],
	) -> dict[tuple[int, int], _Raw]:
		if not date_ids:
			return {}
		table = quote_identifier(self.table_names.table(TableRole.PRICES))
		tickers = quote_identifier(self.table_names.table(TableRole.TICKERS))
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		ids = tuple(dict.fromkeys(date_ids))
		cursor.execute(
			f"SELECT r.date_id, r.ticker_id, t.label, r.state_id, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions, r.observed_at, s.raw_available_at, s.raw_revision FROM {table} r JOIN {tickers} t ON t.id = r.ticker_id JOIN {sessions} s ON s.id = r.date_id WHERE r.date_id IN ({_placeholders(ids)}) ORDER BY r.date_id, r.ticker_id",
			ids,
		)
		raw_rows = tuple(cursor.fetchall())
		repair = _repair_map(cursor, ids, self.table_names)
		result: dict[tuple[int, int], _Raw] = {}
		for row in raw_rows:
			if len(row) != 14:
				raise NumericalRebuildError("Invalid raw numerical input row")
			date_id, ticker_id = _db_id(row[0], "date_id"), _db_id(row[1], "ticker_id")
			if date_id not in sessions_by_id:
				raise NumericalRebuildError("Raw row is outside the requested session scope")
			if not isinstance(row[2], str) or (date_id, ticker_id) in result:
				raise NumericalRebuildError("Invalid or duplicate raw numerical key")
			metadata = repair.get((date_id, ticker_id))
			available_at = _db_datetime(row[12], "raw_available_at")
			if metadata is not None and _state(row[3]) is DataState.INTERPOLATED and metadata.available_at is not None:
				available_at = metadata.available_at
			result[(date_id, ticker_id)] = _Raw(
				date_id, ticker_id, row[2], _state(row[3]), tuple(row[4:11]),
				_db_datetime(row[11], "observed_at"), available_at,
				None if row[13] is None else str(row[13]), None if metadata is None else metadata.revision,
				None if metadata is None else metadata.prior_date_id, None if metadata is None else metadata.following_date_id,
			)
		return result

	def _session(self, session_date: date) -> TradingSession:
		try:
			return self.calendar.session(session_date)
		except Exception as error:
			raise NumericalRebuildError(f"Session is not in the configured calendar: {session_date}") from error

	def _neighbor(self, session: TradingSession, offset: int) -> TradingSession | None:
		try:
			candidate = self.calendar.shift(session, offset)
			return candidate if candidate.sequence == session.sequence + offset else None
		except Exception:
			return None

	def _delta_unit_sessions(self, unit: WorkUnit) -> tuple[TradingSession, TradingSession | None, TradingSession | None]:
		if unit.session_date is None:
			raise NumericalRebuildError("Delta work unit is missing its session date")
		current = self._session(unit.session_date)
		return current, self._neighbor(current, -1), self._neighbor(current, 1)

	def _delta_complete(self, cursor: Cursor, unit: WorkUnit) -> bool:
		if unit.date_id is None or unit.session_date is None:
			raise NumericalRebuildError("Delta work unit is incomplete")
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT transaction_date, delta_complete FROM {sessions} WHERE id = %s", (unit.date_id,))
		rows = cursor.fetchall()
		if len(rows) != 1 or _db_date(rows[0][0], "transaction_date") != unit.session_date:
			raise NumericalRebuildError("Delta work unit date ID does not resolve to its session")
		return _db_bool(rows[0][1], "delta_complete")

	def _load_context(
		self,
		cursor: Cursor,
		current: TradingSession,
		prior: TradingSession | None,
		following: TradingSession | None,
	) -> tuple[dict[date, _Session], dict[tuple[int, int], _Raw]]:
		dates = tuple(session.session_date for session in (prior, current, following) if session is not None)
		sessions = self._load_sessions(cursor, dates)
		by_id = {item.date_id: item for item in sessions.values()}
		return sessions, self._load_raw(cursor, tuple(by_id), by_id)

	def _delta_rows(
		self,
		current: TradingSession,
		prior: TradingSession | None,
		following: TradingSession | None,
		sessions: Mapping[date, _Session],
		raw: Mapping[tuple[int, int], _Raw],
		cutoff: datetime,
	) -> tuple[_DeltaRow, ...]:
		current_db = sessions.get(current.session_date)
		if current_db is None:
			raise NumericalRebuildError("Delta session has no database date mapping")
		prior_db = None if prior is None else sessions.get(prior.session_date)
		following_db = None if following is None else sessions.get(following.session_date)
		current_records = tuple(item for (date_id, _), item in raw.items() if date_id == current_db.date_id)
		prior_records = {} if prior_db is None else {ticker_id: item for (date_id, ticker_id), item in raw.items() if date_id == prior_db.date_id}
		following_records = {} if following_db is None else {ticker_id: item for (date_id, ticker_id), item in raw.items() if date_id == following_db.date_id}
		current_bars = {item.ticker_id: _bar(item, cutoff) for item in current_records}
		prior_bars = {item.label: value for item in prior_records.values() if (value := _bar(item, cutoff)) is not None}
		following_bars = {item.label: value for item in following_records.values() if (value := _bar(item, cutoff)) is not None}
		calculated = legacy_rows(
			tuple(prior_bars.values()),
			tuple(value for value in current_bars.values() if value is not None),
			tuple(following_bars.values()),
		)
		by_label = {item.ticker: item.values for item in calculated}
		result: list[_DeltaRow] = []
		for item in sorted(current_records, key=lambda value: value.ticker_id):
			current_status = _status_for_raw(item, cutoff, absent=CalculationStatus.MISSING_INPUT.value)
			if current_status != CalculationStatus.READY.value:
				values: tuple[float | None, ...] = (None,) * len(DERIVED_COLUMNS)
			else:
				values = by_label.get(item.label, (None,) * len(DERIVED_COLUMNS))
			statuses = [current_status]
			if current_status == CalculationStatus.READY.value:
				if prior is None:
					statuses.append(CalculationStatus.INSUFFICIENT_HISTORY.value)
				else:
					statuses.append(_status_for_raw(prior_records.get(item.ticker_id), cutoff, absent=CalculationStatus.MISSING_INPUT.value))
				if following is None:
					statuses.append(CalculationStatus.NOT_YET_AVAILABLE.value)
				else:
					statuses.append(_status_for_raw(following_records.get(item.ticker_id), cutoff, absent=CalculationStatus.MISSING_INPUT.value))
			status = _combine_status(statuses)
			quality_inputs = [item]
			if item.ticker_id in prior_records:
				quality_inputs.append(prior_records[item.ticker_id])
			if item.ticker_id in following_records:
				quality_inputs.append(following_records[item.ticker_id])
			# The ranking columns consume the eligible following-session population.
			if following is not None:
				quality_inputs.extend(item for item in following_records.values() if _bar(item, cutoff) is not None)
			flags = QualityFlag.CLEAN
			gap = 0
			provenance: set[str] = set()
			for evidence in quality_inputs:
				item_flags, item_gap, item_provenance = _quality(evidence, {value.date_id: value for value in sessions.values()})
				flags |= item_flags
				gap = max(gap, item_gap)
				provenance.update(item_provenance)
			result.append(_DeltaRow(item.ticker_id, values, status, flags, gap, tuple(sorted(provenance))))
		return tuple(result)

	def _write_delta_unit(self, unit: WorkUnit, operation: PendingUpdate, connection: WriteConnection) -> tuple[_DeltaRow, ...]:
		cursor = self._connection_cursor(connection)
		try:
			if self._delta_complete(cursor, unit):
				return ()
			current, prior, following = self._delta_unit_sessions(unit)
			cutoff = operation.scope.cutoff
			sessions, raw = self._load_context(cursor, current, prior, following)
			rows = self._delta_rows(current, prior, following, sessions, raw, cutoff)
			deltas = quote_identifier(self.table_names.table(TableRole.DELTAS))
			session_table = quote_identifier(self.table_names.table(TableRole.SESSIONS))
			try:
				cursor.execute("START TRANSACTION", ())
				for row in rows:
					cursor.execute(f"DELETE FROM {deltas} WHERE date_id = %s AND ticker_id = %s", (unit.date_id, row.ticker_id))
					columns = ", ".join(DERIVED_COLUMNS)
					placeholders = ", ".join("%s" for _ in range(len(DERIVED_COLUMNS) + 7))
					cursor.execute(
						f"INSERT INTO {deltas} (ticker_id, date_id, {columns}, calculation_status, quality_flags, gap_length, provenance, completed_at) VALUES ({placeholders})",
						(row.ticker_id, unit.date_id, *row.values, row.status, int(row.quality), row.gap_length, json.dumps(row.provenance, separators=(",", ":")), self._now()),
					)
				cursor.execute(f"UPDATE {session_table} SET delta_complete = 1, delta_completed_at = %s WHERE id = %s", (self._now(), unit.date_id))
				connection.commit()
			except Exception:
				connection.rollback()
				raise
			return rows
		finally:
			cursor.close()

	def _delta_is_non_ready(self, unit: WorkUnit, operation: PendingUpdate, connection: WriteConnection) -> bool:
		cursor = self._connection_cursor(connection)
		try:
			deltas = quote_identifier(self.table_names.table(TableRole.DELTAS))
			cursor.execute(f"SELECT calculation_status FROM {deltas} WHERE date_id = %s", (unit.date_id,))
			return any(row and str(row[0]) != CalculationStatus.READY.value for row in cursor.fetchall())
		finally:
			cursor.close()

	def _now(self) -> datetime:
		value = self.clock()
		if not isinstance(value, datetime) or value.tzinfo is None:
			raise NumericalRebuildError("Numerical rebuild clock must return an aware datetime")
		return value.astimezone(timezone.utc)

	def _zscore_complete(self, cursor: Cursor, unit: WorkUnit, table: str) -> bool:
		metadata = quote_identifier(self.table_names.table(TableRole.ZSCORE_SESSIONS))
		cursor.execute(f"SELECT row_count FROM {metadata} WHERE series_key = %s AND date_id = %s", (unit.series_key, unit.date_id))
		rows = cursor.fetchall()
		if len(rows) != 1:
			return False
		row_count = _integer(rows[0][0], "z-score row_count")
		if row_count < 0:
			raise NumericalRebuildError("Negative z-score row count")
		cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE date_id = %s", (unit.date_id,))
		counts = cursor.fetchall()
		return len(counts) == 1 and _integer(counts[0][0], "z-score stored row count") == row_count

	def _series_definition(self, cursor: Cursor, unit: WorkUnit) -> ZScoreSeriesDefinition:
		if unit.series_key is None or unit.window is None:
			raise NumericalRebuildError("Z-score work unit is incomplete")
		series_table = quote_identifier(self.table_names.table(TableRole.ZSCORE_SERIES))
		cursor.execute(f"SELECT series_key, schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy FROM {series_table} WHERE series_key = %s", (unit.series_key,))
		rows = cursor.fetchall()
		if len(rows) > 1:
			raise NumericalRebuildError("Duplicate z-score series metadata")
		if rows:
			row = rows[0]
			try:
				definition = ZScoreSeriesDefinition(str(row[2]), str(row[3]), ZScoreFamily(str(row[4])), _integer(row[5], "z-score window"), str(row[6]), str(row[8]), str(row[7]), str(row[9]), _integer(row[1], "z-score schema version"))
			except (TypeError, ValueError) as error:
				raise NumericalRebuildError("Invalid z-score series metadata") from error
			if definition.series_key != unit.series_key or definition.window != unit.window:
				raise NumericalRebuildError("Z-score work unit does not match series metadata")
			return definition
		family = ZScoreFamily.PRICE if unit.series_key == ZScoreSeriesDefinition(data_configuration_key(self.source.data_configuration), self.source.calendar_id, ZScoreFamily.PRICE, unit.window, "rolling-population-zscore-v1", "float32", "not_applicable").series_key else ZScoreFamily.PRICE_DELTA
		definition = ZScoreSeriesDefinition(data_configuration_key(self.source.data_configuration), self.source.calendar_id, family, unit.window, "rolling-population-zscore-v1", "float32", "not_applicable" if family is ZScoreFamily.PRICE else "close_ratio_to_prior_minus_one")
		if definition.series_key != unit.series_key:
			raise NumericalRebuildError("Unknown z-score series definition")
		return definition

	def _zscore_sessions(self, target: TradingSession, family: ZScoreFamily, window: int) -> tuple[TradingSession, ...]:
		count = window + (1 if family is ZScoreFamily.PRICE_DELTA else 0)
		try:
			available = self.calendar.sessions(self.calendar.start, target.session_date)
		except Exception as error:
			raise NumericalRebuildError("Cannot load z-score calendar dependencies") from error
		return tuple(available[-count:])

	def _raw_observation(
		self,
		raw: _Raw | None,
		cutoff: datetime,
		*,
		value: object | None,
		fallback: InputState = InputState.MISSING,
		sessions: Mapping[int, _Session] | None = None,
	) -> ZScoreObservation:
		if raw is None:
			return ZScoreObservation(state=fallback)
		flags, gap, provenance = _quality(raw, sessions or {})
		if raw.state is DataState.BAD:
			return ZScoreObservation(value=value, state=InputState.INVALID, quality=flags, gap_length=gap, provenance=provenance)
		if raw.state is DataState.MISSING:
			return ZScoreObservation(value=value, state=InputState.MISSING, quality=flags, gap_length=gap, provenance=provenance)
		if raw.available_at is None or raw.available_at > cutoff:
			return ZScoreObservation(value=value, state=InputState.NOT_YET_AVAILABLE, actual_available=False, quality=flags, gap_length=gap, provenance=provenance)
		return ZScoreObservation(value=value, quality=flags, gap_length=gap, provenance=provenance)

	def _delta_observation(
		self,
		current: _Raw | None,
		prior: _Raw | None,
		stored: tuple[object | None, str, QualityFlag, int, tuple[str, ...]] | None,
		cutoff: datetime,
		sessions: Mapping[int, _Session] | None = None,
	) -> ZScoreObservation:
		current_close = None if current is None else _decimal(current.values[3], "current close")
		prior_close = None if prior is None else _decimal(prior.values[3], "prior close")
		current_observation = self._raw_observation(current, cutoff, value=current_close, sessions=sessions)
		prior_observation = self._raw_observation(prior, cutoff, value=prior_close, sessions=sessions)
		if current_observation.state is not InputState.AVAILABLE or prior_observation.state is not InputState.AVAILABLE:
			states = (current_observation.state, prior_observation.state)
			priority = {InputState.INVALID: 5, InputState.NOT_YET_AVAILABLE: 4, InputState.UNVERIFIED: 4, InputState.MISSING: 3, InputState.INSUFFICIENT_HISTORY: 2, InputState.AVAILABLE: 0}
			state = max(states, key=lambda item: priority[item])
			return ZScoreObservation(state=state, actual_available=all(item.actual_available for item in (current_observation, prior_observation)), quality=current_observation.quality | prior_observation.quality, gap_length=max(current_observation.gap_length, prior_observation.gap_length), provenance=tuple(sorted(set(current_observation.provenance) | set(prior_observation.provenance))))
		if stored is None or stored[0] is None:
			return ZScoreObservation(state=InputState.MISSING, quality=current_observation.quality | prior_observation.quality, gap_length=max(current_observation.gap_length, prior_observation.gap_length), provenance=tuple(sorted(set(current_observation.provenance) | set(prior_observation.provenance))))
		ratio = _float(stored[0], "stored close ratio")
		if ratio is None or ratio <= 0.0 or current_close is None or prior_close in (None, Decimal(0)):
			return ZScoreObservation.invalid(stored[0])
		expected = float(current_close / prior_close - Decimal(1))
		if not math.isclose(ratio - 1.0, expected, rel_tol=1e-10, abs_tol=1e-12):
			return ZScoreObservation.invalid(stored[0])
		return ZScoreObservation(ratio - 1.0, quality=current_observation.quality | prior_observation.quality | stored[2], gap_length=max(current_observation.gap_length, prior_observation.gap_length, stored[3]), provenance=tuple(sorted(set(current_observation.provenance) | set(prior_observation.provenance) | set(stored[4]))))

	def _load_deltas(self, cursor: Cursor, date_ids: Sequence[int], ticker_ids: Sequence[int]) -> dict[tuple[int, int], tuple[object | None, str, QualityFlag, int, tuple[str, ...]]]:
		if not date_ids or not ticker_ids:
			return {}
		table = quote_identifier(self.table_names.table(TableRole.DELTAS))
		parameters = tuple(date_ids) + tuple(ticker_ids)
		cursor.execute(f"SELECT date_id, ticker_id, close_ratio_to_prior, calculation_status, quality_flags, gap_length, provenance FROM {table} WHERE date_id IN ({_placeholders(date_ids)}) AND ticker_id IN ({_placeholders(ticker_ids)})", parameters)
		result: dict[tuple[int, int], tuple[object | None, str, QualityFlag, int, tuple[str, ...]]] = {}
		for row in cursor.fetchall():
			if len(row) != 7:
				raise NumericalRebuildError("Invalid delta input row")
			key = (_db_id(row[0], "delta date_id"), _db_id(row[1], "delta ticker_id"))
			if key in result:
				raise NumericalRebuildError("Duplicate delta input row")
			result[key] = (row[2], str(row[3]), QualityFlag(_integer(row[4], "delta quality flags")), _integer(row[5], "delta gap length"), _provenance_values(row[6], "delta provenance"))
		return result

	def _build_zscore_input(
		self,
		cursor: Cursor,
		target: TradingSession,
		definition: ZScoreSeriesDefinition,
		ticker_ids: tuple[int, ...],
		cutoff: datetime,
	) -> tuple[ZScoreInput, tuple[TradingSession, ...], dict[date, _Session], dict[tuple[int, int], _Raw]]:
		sessions = self._zscore_sessions(target, definition.family, definition.window)
		db_sessions = self._load_sessions(cursor, tuple(session.session_date for session in sessions))
		by_id = {item.date_id: item for item in db_sessions.values()}
		raw = self._load_raw(cursor, tuple(by_id), by_id)
		date_ids = tuple(item.date_id for item in db_sessions.values())
		completed_delta_dates = tuple(item.date_id for item in db_sessions.values() if item.delta_complete)
		deltas = self._load_deltas(cursor, completed_delta_dates, ticker_ids)
		observations: dict[int, tuple[ZScoreObservation, ...]] = {}
		for ticker_id in ticker_ids:
			values: list[ZScoreObservation] = []
			for index, session in enumerate(sessions):
				db = db_sessions.get(session.session_date)
				current = None if db is None else raw.get((db.date_id, ticker_id))
				if definition.family is ZScoreFamily.PRICE:
					close = None if current is None else _decimal(current.values[3], "raw close")
					values.append(self._raw_observation(current, cutoff, value=close, sessions=by_id))
					continue
				if index == 0:
					values.append(ZScoreObservation.insufficient_history())
					continue
				previous_session = sessions[index - 1]
				previous_db = db_sessions.get(previous_session.session_date)
				previous = None if previous_db is None else raw.get((previous_db.date_id, ticker_id))
				stored = None if db is None else deltas.get((db.date_id, ticker_id))
				values.append(self._delta_observation(current, previous, stored, cutoff, by_id))
			observations[ticker_id] = tuple(values)
		return ZScoreInput.from_values(definition.family, sessions, observations, dependency_sessions=sessions, scored_sessions=(target,)), sessions, db_sessions, raw

	def _zscore_row_for_unit(self, unit: WorkUnit, operation: PendingUpdate, connection: WriteConnection) -> ZScoreRow | None:
		if unit.date_id is None or unit.session_date is None or unit.series_key is None or unit.window is None:
			raise NumericalRebuildError("Z-score work unit is incomplete")
		cursor = self._connection_cursor(connection)
		try:
			definition = self._series_definition(cursor, unit)
			tables = self.table_names.resolve_zscore_tables((unit.window,))[unit.window]
			table = quote_identifier(tables.price if definition.family is ZScoreFamily.PRICE else tables.price_delta)
			if self._zscore_complete(cursor, unit, table):
				return None
			try:
				cursor.execute("START TRANSACTION", ())
				self._ensure_series(cursor, definition)
				target = self._session(unit.session_date)
				affected = tuple(sorted({item.ticker_id for item in operation.scope.clear.zscores if item.date_id == unit.date_id and item.series_key == unit.series_key and item.window == unit.window and item.ticker_id is not None}))
				cursor.execute(f"SELECT ticker_id, zscore, status, quality_flags, gap_length, provenance FROM {table} WHERE date_id = %s ORDER BY ticker_id", (unit.date_id,))
				stored_rows: dict[int, ZScoreRow] = {}
				for stored_row in cursor.fetchall():
					if len(stored_row) != 6:
						raise NumericalRebuildError("Invalid stored z-score row")
					ticker_id = _db_id(stored_row[0], "z-score ticker_id")
					status = ZScoreStatus(_integer(stored_row[2], "z-score status"))
					score = None if stored_row[1] is None else _float(stored_row[1], "stored z-score")
					if status is ZScoreStatus.READY and score is None:
						raise NumericalRebuildError("Ready z-score row has no finite value")
					if status is not ZScoreStatus.READY and score is not None:
						raise NumericalRebuildError("Non-ready z-score row has a numeric value")
					stored_rows[ticker_id] = ZScoreRow(unit.window, target, ticker_id, score, status, "stored", QualityFlag(_integer(stored_row[3], "z-score quality flags")), _integer(stored_row[4], "z-score gap length"), _provenance_values(stored_row[5], "z-score provenance"))
				if not affected:
					affected = tuple(sorted(stored_rows))
				if not affected:
					raise NumericalRebuildError("Z-score completion has no explicit ticker scope")
				input_data, input_sessions, db_sessions, raw = self._build_zscore_input(cursor, target, definition, affected, operation.scope.cutoff)
				result = calculate_zscores(ZScoreSpecification(definition.family, (definition.window,), definition.calculation_version.split(":quality:", 1)[0], definition.precision, definition.delta_definition, self.quality_policy), input_data)
				calculated = {row.ticker_id: row for row in result.rows if row.session == target and row.window == definition.window}
				if set(calculated) != set(affected):
					raise NumericalRebuildError("Z-score kernel returned an incomplete ticker set")
				merged = {ticker_id: row for ticker_id, row in stored_rows.items() if ticker_id not in affected}
				merged.update(calculated)
				for ticker_id in affected:
					cursor.execute(f"DELETE FROM {table} WHERE date_id = %s AND ticker_id = %s", (unit.date_id, ticker_id))
					calculated_row = calculated[ticker_id]
					cursor.execute(f"INSERT INTO {table} (date_id, ticker_id, zscore, status, quality_flags, gap_length, provenance) VALUES (%s,%s,%s,%s,%s,%s,%s)", (unit.date_id, ticker_id, calculated_row.score, int(calculated_row.status), int(calculated_row.quality), calculated_row.gap_length, json.dumps(calculated_row.provenance, separators=(",", ":"))))
				metadata = quote_identifier(self.table_names.table(TableRole.ZSCORE_SESSIONS))
				all_rows = tuple(sorted(merged.values(), key=lambda row: row.ticker_id))
				dependency_payload = self._zscore_digest_payload(definition, target, input_sessions, affected, db_sessions, raw, operation.scope.cutoff)
				dependency_digest = content_id(dependency_payload)
				availability_digest = content_id({"states": dependency_payload["states"], "cutoff": operation.scope.cutoff.isoformat()})
				status_counts = {status: sum(row.status is status for row in all_rows) for status in ZScoreStatus}
				serialized = json.dumps({str(int(status)): status_counts[status] for status in ZScoreStatus}, separators=(",", ":"), sort_keys=True)
				cursor.execute(f"DELETE FROM {metadata} WHERE series_key = %s AND date_id = %s", (unit.series_key, unit.date_id))
				actual_input_available = all(observation.state not in (InputState.MISSING, InputState.INVALID, InputState.NOT_YET_AVAILABLE, InputState.UNVERIFIED) and observation.actual_available for ticker in input_data.tickers for observation in ticker.observations)
				cursor.execute(f"INSERT INTO {metadata} (series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (unit.series_key, unit.date_id, dependency_digest, availability_digest, int(actual_input_available), self._now(), len(all_rows), status_counts[ZScoreStatus.READY], serialized, rows_content_id(all_rows)))
				connection.commit()
			except Exception:
				connection.rollback()
				raise
			return next(iter(calculated.values()))
		finally:
			cursor.close()

	def _ensure_series(self, cursor: Cursor, definition: ZScoreSeriesDefinition) -> None:
		table = quote_identifier(self.table_names.table(TableRole.ZSCORE_SERIES))
		cursor.execute(f"SELECT schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy FROM {table} WHERE series_key = %s", (definition.series_key,))
		rows = cursor.fetchall()
		if rows:
			if len(rows) != 1 or tuple(rows[0]) != (definition.schema_version, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy):
				raise NumericalRebuildError("Conflicting z-score series definition")
			return
		cursor.execute(
			f"INSERT INTO {table} (series_key, schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
			(definition.series_key, definition.schema_version, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy, self._now()),
		)

	def _zscore_digest_payload(
		self,
		definition: ZScoreSeriesDefinition,
		target: TradingSession,
		input_sessions: tuple[TradingSession, ...],
		ticker_ids: tuple[int, ...],
		db_sessions: Mapping[date, _Session],
		raw: Mapping[tuple[int, int], _Raw],
		cutoff: datetime,
	) -> dict[str, object]:
		states: list[object] = []
		for session in input_sessions:
			db = db_sessions.get(session.session_date)
			for ticker_id in ticker_ids:
				item = None if db is None else raw.get((db.date_id, ticker_id))
				state = InputState.MISSING.value if item is None else _status_for_raw(item, cutoff, absent=InputState.MISSING.value)
				states.append((session.session_date.isoformat(), session.sequence, ticker_id, state, None if item is None else item.raw_revision, None if item is None or item.available_at is None else item.available_at.isoformat()))
		return {"family": definition.family.value, "window": definition.window, "target": target.session_date.isoformat(), "sessions": [(item.session_date.isoformat(), item.sequence, None if (mapped := db_sessions.get(item.session_date)) is None else mapped.date_id) for item in input_sessions], "states": states}


NumericalRebuildRunner = SimplifiedNumericalRebuildRunner


def _unique_units(units: Iterable[WorkUnit]) -> tuple[WorkUnit, ...]:
	result: list[WorkUnit] = []
	seen: set[WorkUnit] = set()
	for unit in units:
		if unit not in seen:
			seen.add(unit)
			result.append(unit)
	return tuple(result)
