"""Independent repositories for the explicit Stage 3 schema.

These repositories are intentionally not wired into the current maintenance
entrypoints.  They provide the durable contracts that the later coordinator
will use and refuse to operate on an old or partially initialized database.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Iterator, Protocol, Sequence

from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.simplified_contract import (
	DATA_STATE_BY_ID,
	DATA_STATE_IDS,
	DataState,
	FailureDetail,
	PendingScope,
	PendingUpdate,
	RepairMetadata,
	SessionCompletion,
	SourceConfigurationRecord,
	UnitKind,
)
from stockguidance.database.simplified_schema import require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory
from stockguidance.market_data.identity import data_configuration_key, source_key


class IncompatibleImportError(ValueError):
	"""Raised when an import does not match the database singleton."""


class ActiveOperationError(ValueError):
	"""Raised when a second pending operation is registered."""


class _Repository:
	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None) -> None:
		self.source = source
		self.connect = connect
		self.table_names = table_names or load_table_names()

	@contextmanager
	def _transaction(self, *, write: bool) -> Iterator[tuple[WriteConnection, Cursor]]:
		connection = self.connect(self.source.database_config)
		cursor = connection.cursor()
		try:
			cursor.execute("START TRANSACTION", ())
			require_simplified_schema(cursor, self.table_names)
			yield connection, cursor
			if write:
				connection.commit()
			else:
				connection.rollback()
		except Exception:
			connection.rollback()
			raise
		finally:
			cursor.close()
			connection.close()


def _utc(value: object, name: str) -> datetime:
	if isinstance(value, datetime):
		return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
	if isinstance(value, str):
		try:
			parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
		except ValueError:
			raise ValueError(f"Invalid {name}") from None
		return _utc(parsed, name)
	raise ValueError(f"Invalid {name}")


def _text(value: object, name: str) -> str:
	if isinstance(value, bytes):
		value = value.decode("ascii")
	if not isinstance(value, str) or not value:
		raise ValueError(f"Invalid {name}")
	return value


def _db_bool(value: object, name: str) -> bool:
	if value in (0, False):
		return False
	if value in (1, True):
		return True
	raise ValueError(f"Invalid database boolean for {name}")


def _db_id(value: object, name: str) -> int:
	if isinstance(value, bytes):
		value = value.decode("ascii")
	if isinstance(value, str) and value.isdecimal():
		value = int(value)
	if type(value) is not int or value <= 0:
		raise ValueError(f"Invalid database identifier for {name}")
	return value


def _db_json(value: object, name: str) -> object:
	if isinstance(value, bytes):
		value = value.decode("utf-8")
	if isinstance(value, str):
		try:
			return json.loads(value)
		except json.JSONDecodeError:
			raise ValueError(f"Invalid JSON in {name}") from None
	return value


def _hash(value: object, name: str) -> str:
	result = _text(value, name)
	if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
		raise ValueError(f"Invalid {name}")
	return result


class SourceConfigurationRepository(_Repository):
	"""Read and enforce the one immutable source configuration row."""

	def load(self) -> SourceConfigurationRecord:
		with self._transaction(write=False) as (_, cursor):
			cursor.execute(
				f"SELECT configuration_key, source_key, source_family, endpoint, market, universe, interval_code, calendar_id, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment, created_at FROM {quote_identifier(self.table_names.table(TableRole.SOURCE))} ORDER BY id",
				(),
			)
			rows = cursor.fetchall()
		return self._record(rows)

	def ensure(self, created_at: datetime | None = None) -> SourceConfigurationRecord:
		when = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
		configuration = self.source.data_configuration
		expected = SourceConfigurationRecord(
			data_configuration_key(configuration), source_key(self.source.identity), configuration.source_family,
			configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value,
			configuration.calendar_id, configuration.raw_price_split_adjusted, configuration.raw_price_dividend_adjusted,
			configuration.raw_price_vendor_defined_adjustment, when,
		)
		with self._transaction(write=True) as (_, cursor):
			cursor.execute(f"SELECT configuration_key, source_key, source_family, endpoint, market, universe, interval_code, calendar_id, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment, created_at FROM {quote_identifier(self.table_names.table(TableRole.SOURCE))} ORDER BY id", ())
			rows = cursor.fetchall()
			if rows:
				actual = self._record(rows)
				self._assert_match(actual, expected)
				return actual
			cursor.execute(
				f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.SOURCE))} (id, configuration_key, source_key, source_family, endpoint, market, universe, interval_code, calendar_id, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment, created_at) VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
				(expected.configuration_key, expected.source_key, expected.source_family, expected.endpoint, expected.market, expected.universe, expected.interval_code, expected.calendar_id, expected.raw_price_split_adjusted, expected.raw_price_dividend_adjusted, expected.raw_price_vendor_defined_adjustment, expected.created_at),
			)
			return expected

	def assert_compatible(self) -> None:
		actual = self.load()
		configuration = self.source.data_configuration
		expected = SourceConfigurationRecord(
			data_configuration_key(configuration), source_key(self.source.identity), configuration.source_family,
			configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value,
			configuration.calendar_id, configuration.raw_price_split_adjusted, configuration.raw_price_dividend_adjusted,
			configuration.raw_price_vendor_defined_adjustment, actual.created_at,
		)
		self._assert_match(actual, expected)

	@staticmethod
	def _record(rows: Sequence[Sequence[object]]) -> SourceConfigurationRecord:
		if len(rows) != 1 or len(rows[0]) != 12:
			raise IncompatibleImportError("Database must contain exactly one source configuration row")
		row = rows[0]
		return SourceConfigurationRecord(
			_hash(row[0], "configuration_key"), _hash(row[1], "source_key"), _text(row[2], "source_family"), _text(row[3], "endpoint"),
			_text(row[4], "market"), _text(row[5], "universe"), _text(row[6], "interval_code"), _text(row[7], "calendar_id"),
			_db_bool(row[8], "raw_price_split_adjusted"), _db_bool(row[9], "raw_price_dividend_adjusted"),
			None if row[10] is None else _text(row[10], "raw_price_vendor_defined_adjustment"), _utc(row[11], "created_at"),
		)

	@staticmethod
	def _assert_match(actual: SourceConfigurationRecord, expected: SourceConfigurationRecord) -> None:
		if (
			actual.configuration_key != expected.configuration_key or actual.source_key != expected.source_key
			or actual.source_family != expected.source_family or actual.endpoint != expected.endpoint
			or actual.market != expected.market or actual.universe != expected.universe
			or actual.interval_code != expected.interval_code or actual.calendar_id != expected.calendar_id
			or actual.raw_price_split_adjusted != expected.raw_price_split_adjusted
			or actual.raw_price_dividend_adjusted != expected.raw_price_dividend_adjusted
			or actual.raw_price_vendor_defined_adjustment != expected.raw_price_vendor_defined_adjustment
		):
			raise IncompatibleImportError("Import source configuration does not match the database singleton")


class DataStateRepository(_Repository):
	"""Resolve the immutable source-state dictionary without inventing IDs."""

	def load(self) -> dict[int, DataState]:
		with self._transaction(write=False) as (_, cursor):
			cursor.execute(f"SELECT id, code FROM {quote_identifier(self.table_names.table(TableRole.DATA_STATES))} ORDER BY id", ())
			rows = cursor.fetchall()
		result: dict[int, DataState] = {}
		for row in rows:
			if len(row) != 2:
				raise ValueError("Invalid data-state row")
			state_id = _db_id(row[0], "state_id")
			try:
				state = DataState(_text(row[1], "state code"))
			except ValueError:
				raise ValueError("Unknown data-state code") from None
			if state_id in result or state_id not in DATA_STATE_BY_ID or DATA_STATE_BY_ID[state_id] is not state:
				raise ValueError("Data-state IDs and codes are incompatible")
			result[state_id] = state
		if result != dict(DATA_STATE_BY_ID):
			raise ValueError("Database data-state dictionary is incomplete")
		return result

	def require(self, state: DataState | int) -> DataState:
		state_id = DATA_STATE_IDS[state] if isinstance(state, DataState) else _db_id(state, "state_id")
		with self._transaction(write=False) as (_, cursor):
			cursor.execute(f"SELECT code FROM {quote_identifier(self.table_names.table(TableRole.DATA_STATES))} WHERE id = %s", (state_id,))
			rows = cursor.fetchall()
		if len(rows) != 1 or len(rows[0]) != 1:
			raise ValueError("Unknown data-state ID")
		try:
			actual = DataState(_text(rows[0][0], "state code"))
		except ValueError:
			raise ValueError("Unknown data-state code") from None
		if actual is not DATA_STATE_BY_ID.get(state_id):
			raise ValueError("Data-state ID has an incompatible code")
		return actual


class SessionCompletionRepository(_Repository):
	"""Persist raw and delta completion independently on transaction_dates."""

	def load(self, date_id: int) -> SessionCompletion:
		date_id = _db_id(date_id, "date_id")
		with self._transaction(write=False) as (_, cursor):
			cursor.execute(f"SELECT id, transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision, delta_complete, delta_completed_at FROM {quote_identifier(self.table_names.table(TableRole.SESSIONS))} WHERE id = %s", (date_id,))
			rows = cursor.fetchall()
		return self._completion(rows)

	def set_raw_complete(self, date_id: int, revision: str, available_at: datetime, completed_at: datetime) -> None:
		date_id = _db_id(date_id, "date_id")
		revision = _hash(revision, "raw_revision")
		available_at, completed_at = _utc(available_at, "raw_available_at"), _utc(completed_at, "raw_completed_at")
		with self._transaction(write=True) as (_, cursor):
			self._require_date(cursor, date_id)
			cursor.execute(f"UPDATE {quote_identifier(self.table_names.table(TableRole.SESSIONS))} SET raw_complete = 1, raw_available_at = %s, raw_completed_at = %s, raw_revision = %s WHERE id = %s", (available_at, completed_at, revision, date_id))

	def clear_raw(self, date_id: int) -> None:
		self._update_clear(date_id, "raw_complete = 0, raw_available_at = NULL, raw_completed_at = NULL, raw_revision = NULL")

	def set_delta_complete(self, date_id: int, completed_at: datetime) -> None:
		date_id = _db_id(date_id, "date_id")
		completed_at = _utc(completed_at, "delta_completed_at")
		with self._transaction(write=True) as (_, cursor):
			self._require_date(cursor, date_id)
			cursor.execute(f"UPDATE {quote_identifier(self.table_names.table(TableRole.SESSIONS))} SET delta_complete = 1, delta_completed_at = %s WHERE id = %s", (completed_at, date_id))

	def clear_delta(self, date_id: int) -> None:
		self._update_clear(date_id, "delta_complete = 0, delta_completed_at = NULL")

	def _update_clear(self, date_id: int, assignments: str) -> None:
		date_id = _db_id(date_id, "date_id")
		with self._transaction(write=True) as (_, cursor):
			self._require_date(cursor, date_id)
			cursor.execute(f"UPDATE {quote_identifier(self.table_names.table(TableRole.SESSIONS))} SET {assignments} WHERE id = %s", (date_id,))

	def _require_date(self, cursor: Cursor, date_id: int) -> None:
		cursor.execute(f"SELECT id FROM {quote_identifier(self.table_names.table(TableRole.SESSIONS))} WHERE id = %s", (date_id,))
		if len(cursor.fetchall()) != 1:
			raise ValueError("Unknown transaction date ID")

	@staticmethod
	def _completion(rows: Sequence[Sequence[object]]) -> SessionCompletion:
		if len(rows) != 1 or len(rows[0]) != 8:
			raise ValueError("Unknown or invalid transaction date completion")
		row = rows[0]
		transaction_date = row[1]
		if isinstance(transaction_date, datetime):
			transaction_date = transaction_date.date()
		if not isinstance(transaction_date, date):
			try:
				transaction_date = date.fromisoformat(_text(transaction_date, "transaction_date"))
			except ValueError:
				raise ValueError("Invalid transaction date") from None
		return SessionCompletion(
			_db_id(row[0], "date_id"), transaction_date, _db_bool(row[2], "raw_complete"),
			None if row[3] is None else _utc(row[3], "raw_available_at"), None if row[4] is None else _utc(row[4], "raw_completed_at"),
			None if row[5] is None else _hash(row[5], "raw_revision"), _db_bool(row[6], "delta_complete"),
			None if row[7] is None else _utc(row[7], "delta_completed_at"),
		)


class RepairMetadataRepository(_Repository):
	"""Store exceptional observations while preserving exact decimal strings."""

	def save(self, record: RepairMetadata) -> int:
		state_id = DATA_STATE_IDS[record.state]
		with self._transaction(write=True) as (_, cursor):
			cursor.execute(f"SELECT id FROM {quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))} WHERE date_id = %s AND ticker_id = %s AND revision = %s", (record.date_id, record.ticker_id, record.revision))
			rows = cursor.fetchall()
			if rows:
				return _db_id(rows[0][0], "repair_metadata_id")
			cursor.execute(
				f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))} (date_id, ticker_id, revision, state_id, original_values, effective_values, invalid_fields, interpolation_method, prior_date_id, following_date_id, endpoint_values, observed_at, available_at, endpoints_available_at, recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
				(record.date_id, record.ticker_id, record.revision, state_id, json.dumps(record.original_json(), separators=(",", ":")), None if record.effective_json() is None else json.dumps(record.effective_json(), separators=(",", ":")), json.dumps(list(record.invalid_fields), separators=(",", ":")), record.interpolation_method, record.prior_date_id, record.following_date_id, None if record.endpoint_json() is None else json.dumps(record.endpoint_json(), separators=(",", ":")), record.observed_at, record.available_at, record.endpoints_available_at, record.recorded_at),
			)
			cursor.execute(f"SELECT id FROM {quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))} WHERE date_id = %s AND ticker_id = %s AND revision = %s", (record.date_id, record.ticker_id, record.revision))
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise ValueError("Repair metadata insert did not resolve one row")
			return _db_id(rows[0][0], "repair_metadata_id")

	def load_current(self, date_id: int, ticker_id: int) -> RepairMetadata | None:
		with self._transaction(write=False) as (_, cursor):
			cursor.execute(f"SELECT date_id, ticker_id, revision, state_id, original_values, effective_values, invalid_fields, interpolation_method, prior_date_id, following_date_id, endpoint_values, observed_at, available_at, endpoints_available_at, recorded_at FROM {quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))} WHERE date_id = %s AND ticker_id = %s ORDER BY recorded_at DESC, id DESC LIMIT 1", (_db_id(date_id, "date_id"), _db_id(ticker_id, "ticker_id")))
			rows = cursor.fetchall()
		return None if not rows else self._record(rows[0])

	@staticmethod
	def _decimal_array(value: object, name: str) -> tuple[Decimal | None, ...]:
		if not isinstance(value, list) or len(value) != 7:
			raise ValueError(f"Invalid {name}")
		result: list[Decimal | None] = []
		for item in value:
			if item is None:
				result.append(None)
				continue
			if not isinstance(item, str):
				raise ValueError(f"Invalid {name} decimal")
			try:
				result.append(Decimal(item))
			except InvalidOperation:
				raise ValueError(f"Invalid {name} decimal") from None
		return tuple(result)

	@staticmethod
	def _original_array(value: object, name: str) -> tuple[Decimal | str | None, ...]:
		if not isinstance(value, list) or len(value) != 7:
			raise ValueError(f"Invalid {name}")
		result: list[Decimal | str | None] = []
		for item in value:
			if item is None:
				result.append(None)
			elif not isinstance(item, str):
				raise ValueError(f"Invalid {name} value")
			else:
				try:
					result.append(Decimal(item))
				except InvalidOperation:
					result.append(item)
		return tuple(result)

	@classmethod
	def _record(cls, row: Sequence[object]) -> RepairMetadata:
		if len(row) != 15:
			raise ValueError("Invalid repair metadata row")
		state_id = _db_id(row[3], "state_id")
		state = DATA_STATE_BY_ID.get(state_id)
		if state is None:
			raise ValueError("Unknown repair state ID")
		invalid = _db_json(row[6], "invalid_fields")
		if not isinstance(invalid, list) or not all(isinstance(value, str) for value in invalid):
			raise ValueError("Invalid repair field list")
		effective = None if row[5] is None else cls._decimal_array(_db_json(row[5], "effective_values"), "effective_values")
		endpoint_raw = None if row[10] is None else _db_json(row[10], "endpoint_values")
		endpoint = None
		if endpoint_raw is not None:
			if not isinstance(endpoint_raw, list) or len(endpoint_raw) != 2:
				raise ValueError("Invalid endpoint values")
			endpoint = (cls._decimal_array(endpoint_raw[0], "endpoint_values"), cls._decimal_array(endpoint_raw[1], "endpoint_values"))
		return RepairMetadata(
			_db_id(row[0], "date_id"), _db_id(row[1], "ticker_id"), _hash(row[2], "revision"), state,
			cls._original_array(_db_json(row[4], "original_values"), "original_values"), effective, tuple(invalid),
			None if row[7] is None else _text(row[7], "interpolation_method"), None if row[8] is None else _db_id(row[8], "prior_date_id"),
			None if row[9] is None else _db_id(row[9], "following_date_id"), endpoint, None if row[11] is None else _utc(row[11], "observed_at"),
			None if row[12] is None else _utc(row[12], "available_at"), None if row[13] is None else _utc(row[13], "endpoints_available_at"), _utc(row[14], "recorded_at"),
		)


class PendingUpdateRepositoryProtocol(Protocol):
	def register(self, scope: PendingScope, created_at: datetime) -> PendingUpdate: ...
	def load_active(self) -> PendingUpdate | None: ...
	def begin_clearing(self, operation_id: int, started_at: datetime) -> None: ...
	def record_failure(self, operation_id: int, failure: FailureDetail, failed_at: datetime) -> None: ...
	def mark_cleared(self, operation_id: int, cleared_at: datetime) -> None: ...
	def delete_completed(self, operation_id: int, completed_at: datetime) -> None: ...


def _assert_clear_scope_is_empty(cursor: Cursor, scope: PendingScope, table_names: TableNames) -> None:
	"""Verify the durable clear evidence before setting its committed flag."""

	prices = quote_identifier(table_names.table(TableRole.PRICES))
	repairs = quote_identifier(table_names.table(TableRole.REPAIR_METADATA))
	deltas = quote_identifier(table_names.table(TableRole.DELTAS))
	sessions = quote_identifier(table_names.table(TableRole.SESSIONS))
	for unit in scope.clear.raw:
		cursor.execute(f"SELECT 1 FROM {prices} WHERE date_id = %s LIMIT 1", (unit.date_id,))
		if cursor.fetchall():
			raise ValueError("Cannot commit clearing while raw rows remain")
	for unit in scope.clear.repairs:
		cursor.execute(f"SELECT 1 FROM {repairs} WHERE date_id = %s AND ticker_id = %s LIMIT 1", (unit.date_id, unit.ticker_id))
		if cursor.fetchall():
			raise ValueError("Cannot commit clearing while repair metadata remains")
	for unit in scope.clear.deltas:
		cursor.execute(f"SELECT 1 FROM {deltas} WHERE date_id = %s AND ticker_id = %s LIMIT 1", (unit.date_id, unit.ticker_id))
		if cursor.fetchall():
			raise ValueError("Cannot commit clearing while delta rows remain")
	for unit in scope.clear.zscores:
		if unit.series_key is None or unit.window is None:
			raise ValueError("Invalid z-score clear unit")
		series = quote_identifier(table_names.table(TableRole.ZSCORE_SERIES))
		cursor.execute(f"SELECT family FROM {series} WHERE series_key = %s", (unit.series_key,))
		series_rows = cursor.fetchall()
		if len(series_rows) != 1:
			raise ValueError("Cannot verify a z-score clear without its series definition")
		family = series_rows[0][0].decode("ascii") if isinstance(series_rows[0][0], bytes) else str(series_rows[0][0])
		resolved = table_names.resolve_zscore_tables((unit.window,))[unit.window]
		if family == "price":
			table = resolved.price
		elif family == "price_delta":
			table = resolved.price_delta
		else:
			raise ValueError("Unknown z-score series family")
		cursor.execute(f"SELECT 1 FROM {quote_identifier(table)} WHERE date_id = %s AND ticker_id = %s LIMIT 1", (unit.date_id, unit.ticker_id))
		if cursor.fetchall():
			raise ValueError("Cannot commit clearing while z-score rows remain")
	for unit in scope.clear.completion:
		if unit.unit_kind == UnitKind.RAW_SESSION.value:
			cursor.execute(f"SELECT raw_complete, raw_available_at, raw_completed_at, raw_revision FROM {sessions} WHERE id = %s", (unit.date_id,))
			rows = cursor.fetchall()
			if len(rows) != 1 or rows[0][0] not in (0, False) or any(value is not None for value in rows[0][1:]):
				raise ValueError("Cannot commit clearing while raw completion evidence remains")
		elif unit.unit_kind == UnitKind.DELTA_SESSION.value:
			cursor.execute(f"SELECT delta_complete, delta_completed_at FROM {sessions} WHERE id = %s", (unit.date_id,))
			rows = cursor.fetchall()
			if len(rows) != 1 or rows[0][0] not in (0, False) or rows[0][1] is not None:
				raise ValueError("Cannot commit clearing while delta completion evidence remains")
		elif unit.unit_kind == UnitKind.ZSCORE_SESSION.value:
			cursor.execute(f"SELECT 1 FROM {quote_identifier(table_names.table(TableRole.ZSCORE_SESSIONS))} WHERE series_key = %s AND date_id = %s LIMIT 1", (unit.series_key, unit.date_id))
			if cursor.fetchall():
				raise ValueError("Cannot commit clearing while z-score completion evidence remains")
		else:
			raise ValueError("Unknown completion clear unit")


class PendingUpdateRepository(_Repository, PendingUpdateRepositoryProtocol):
	"""Persist one immutable scope and only its mutable recovery fields."""

	def register(self, scope: PendingScope, created_at: datetime) -> PendingUpdate:
		if scope.configuration_key != data_configuration_key(self.source.data_configuration):
			raise IncompatibleImportError("Pending scope configuration does not match the database source")
		# Registration is an import boundary: do not persist an operation against
		# a database whose singleton was changed or duplicated outside this repo.
		SourceConfigurationRepository(self.source, self.connect, self.table_names).assert_compatible()
		created_at = _utc(created_at, "created_at")
		with self._transaction(write=True) as (_, cursor):
			cursor.execute(f"SELECT id FROM {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} ORDER BY id FOR UPDATE", ())
			if cursor.fetchall():
				raise ActiveOperationError("An unfinished database operation already exists")
			cursor.execute(f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} (operation_kind, scope_version, scope, created_at, affected_rows_cleared) VALUES (%s,%s,%s,%s,0)", (scope.operation_kind.value, scope.scope_version, scope.to_json(), created_at))
			cursor.execute(f"SELECT id, operation_kind, scope_version, scope, created_at, clearing_started_at, affected_rows_cleared, failure_code, failure_detail, failed_at FROM {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} ORDER BY id DESC LIMIT 1", ())
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise ValueError("Pending operation insert did not resolve one row")
			return self._pending(rows[0])

	def load_active(self) -> PendingUpdate | None:
		with self._transaction(write=False) as (_, cursor):
			cursor.execute(f"SELECT id, operation_kind, scope_version, scope, created_at, clearing_started_at, affected_rows_cleared, failure_code, failure_detail, failed_at FROM {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} ORDER BY id", ())
			rows = cursor.fetchall()
		if not rows:
			return None
		if len(rows) != 1:
			raise ValueError("Pending update table contains more than one active operation")
		return self._pending(rows[0])

	def begin_clearing(self, operation_id: int, started_at: datetime) -> None:
		operation_id = _db_id(operation_id, "operation_id")
		started_at = _utc(started_at, "clearing_started_at")
		with self._transaction(write=True) as (_, cursor):
			self._require_operation(cursor, operation_id)
			cursor.execute(f"UPDATE {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} SET clearing_started_at = COALESCE(clearing_started_at, %s) WHERE id = %s", (started_at, operation_id))

	def record_failure(self, operation_id: int, failure: FailureDetail, failed_at: datetime) -> None:
		operation_id = _db_id(operation_id, "operation_id")
		failed_at = _utc(failed_at, "failed_at")
		with self._transaction(write=True) as (_, cursor):
			self._require_operation(cursor, operation_id)
			cursor.execute(f"UPDATE {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} SET failure_code = %s, failure_detail = %s, failed_at = %s WHERE id = %s", (failure.code, failure.detail, failed_at, operation_id))

	def mark_cleared(self, operation_id: int, cleared_at: datetime) -> None:
		operation_id = _db_id(operation_id, "operation_id")
		cleared_at = _utc(cleared_at, "cleared_at")
		with self._transaction(write=True) as (_, cursor):
			cursor.execute(f"SELECT scope, affected_rows_cleared FROM {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} WHERE id = %s FOR UPDATE", (operation_id,))
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise ValueError("Unknown pending operation")
			if _db_bool(rows[0][1], "affected_rows_cleared"):
				return
			scope = PendingScope.from_json(_db_json(rows[0][0], "pending scope"))
			_assert_clear_scope_is_empty(cursor, scope, self.table_names)
			cursor.execute(f"UPDATE {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} SET affected_rows_cleared = 1, clearing_started_at = COALESCE(clearing_started_at, %s) WHERE id = %s", (cleared_at, operation_id))

	def delete_completed(self, operation_id: int, completed_at: datetime) -> None:
		operation_id = _db_id(operation_id, "operation_id")
		_utc(completed_at, "completed_at")
		with self._transaction(write=True) as (_, cursor):
			cursor.execute(f"SELECT affected_rows_cleared FROM {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} WHERE id = %s FOR UPDATE", (operation_id,))
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise ValueError("Unknown pending operation")
			if not _db_bool(rows[0][0], "affected_rows_cleared"):
				raise ValueError("Cannot delete a pending operation before clearing is committed")
			cursor.execute(f"DELETE FROM {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} WHERE id = %s", (operation_id,))

	def _require_operation(self, cursor: Cursor, operation_id: int) -> None:
		cursor.execute(f"SELECT id FROM {quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))} WHERE id = %s FOR UPDATE", (operation_id,))
		if len(cursor.fetchall()) != 1:
			raise ValueError("Unknown pending operation")

	@staticmethod
	def _pending(row: Sequence[object]) -> PendingUpdate:
		if len(row) != 10:
			raise ValueError("Invalid pending operation row")
		failure = None
		if row[7] is not None or row[8] is not None or row[9] is not None:
			if row[7] is None or row[8] is None or row[9] is None:
				raise ValueError("Incomplete pending failure detail")
			failure = FailureDetail(_text(row[7], "failure_code"), _text(row[8], "failure_detail"), _utc(row[9], "failed_at"))
		scope = PendingScope.from_json(row[3])
		if scope.scope_version != row[2] or scope.operation_kind.value != _text(row[1], "operation_kind"):
			raise ValueError("Pending operation scope/header mismatch")
		return PendingUpdate(
			_db_id(row[0], "operation_id"), scope, _utc(row[4], "created_at"), None if row[5] is None else _utc(row[5], "clearing_started_at"),
			_db_bool(row[6], "affected_rows_cleared"), failure,
		)


MySQLSourceConfigurationRepository = SourceConfigurationRepository
MySQLDataStateRepository = DataStateRepository
MySQLSessionCompletionRepository = SessionCompletionRepository
MySQLRepairMetadataRepository = RepairMetadataRepository
MySQLPendingUpdateRepository = PendingUpdateRepository
