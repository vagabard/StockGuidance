"""Stage 5 coordination, durable clearing, and read guards.

This module owns the part of an update that is safe to run before the later
raw/derived rebuild stages.  It never downloads source data and it has no
numerical rebuild implementation.  A pending scope is validated, persisted,
cleared in retryable chunks, and only then exposed to an injected rebuild
runner.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import ContextManager, Protocol, cast

from stockguidance.config.source import SourceConfig
from stockguidance.database.dependency_planner import DatabaseSnapshot, DependencyPlannerProtocol, UpdateRequest
from stockguidance.database.protocols import Cursor
from stockguidance.database.repositories.simplified import PendingUpdateRepository, PendingUpdateRepositoryProtocol
from stockguidance.database.simplified_contract import (
	CacheInvalidation,
	ClearScope,
	FailureDetail,
	PendingScope,
	PendingUpdate,
	PreparedPayload,
	UnitKind,
	WorkUnit,
)
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory
from stockguidance.workflows.market_lock import MaintenanceWriterLock


class RecoveryError(RuntimeError):
	"""Raised when an operation cannot safely progress or resume."""


class PayloadValidationError(RecoveryError):
	"""Raised before clearing when a prepared reconstruction payload is unusable."""


class ClearingError(RecoveryError):
	"""Raised when a clear chunk fails; the operation remains uncleared."""


class RebuildRequiredError(RecoveryError):
	"""Raised when Stage 5 has cleared work but no later rebuild runner exists."""


class ReadinessState(StrEnum):
	READY = "ready"
	UNFINISHED_OPERATION = "unfinished_operation"


@dataclass(frozen=True)
class ClearResult:
	"""Evidence returned after database chunks and cache invalidations complete."""

	completed_units: tuple[WorkUnit, ...]
	invalidated_caches: tuple[CacheInvalidation, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.completed_units, tuple) or any(not isinstance(value, WorkUnit) for value in self.completed_units):
			raise ValueError("completed_units must contain WorkUnit values")
		if not isinstance(self.invalidated_caches, tuple) or any(not isinstance(value, CacheInvalidation) for value in self.invalidated_caches):
			raise ValueError("invalidated_caches must contain CacheInvalidation values")


@dataclass(frozen=True)
class RebuildResult:
	"""Interchange result reserved for a later stage's injected rebuild runner."""

	committed_units: tuple[WorkUnit, ...] = ()
	explicit_non_ready: tuple[WorkUnit, ...] = ()
	deferred_units: tuple[WorkUnit, ...] = ()
	failed_units: tuple[WorkUnit, ...] = ()


@dataclass(frozen=True)
class ReadScope:
	"""The exact work units a read transaction intends to consume.

	When ``units`` is empty, ``date_ids`` is a conservative date-level scope.
	That form is useful for existing readers that do not yet expose their
	complete ticker/series dependency set; it never uses a missing row as a
	guard.
	"""

	units: tuple[WorkUnit, ...] = ()
	date_ids: tuple[int, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.units, tuple) or any(not isinstance(value, WorkUnit) for value in self.units):
			raise ValueError("ReadScope.units must contain WorkUnit values")
		if len(set(self.units)) != len(self.units):
			raise ValueError("ReadScope.units must not contain duplicates")
		if not isinstance(self.date_ids, tuple) or any(type(value) is not int or value <= 0 for value in self.date_ids):
			raise ValueError("ReadScope.date_ids must contain positive IDs")
		if len(set(self.date_ids)) != len(self.date_ids):
			raise ValueError("ReadScope.date_ids must not contain duplicates")
		if self.units and self.date_ids:
			raise ValueError("Use either explicit read units or date-level IDs")


@dataclass(frozen=True)
class Readiness:
	status: ReadinessState
	operation_id: int | None = None
	phase: str | None = None
	date_id: int | None = None
	ticker_id: int | None = None
	series_key: str | None = None
	reason: str | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.status, ReadinessState):
			raise ValueError("Invalid readiness state")
		if self.status is ReadinessState.READY and any(value is not None for value in (self.operation_id, self.phase, self.date_id, self.ticker_id, self.series_key, self.reason)):
			raise ValueError("Ready result cannot contain unfinished-operation detail")
		if self.status is ReadinessState.UNFINISHED_OPERATION and (self.operation_id is None or self.reason is None):
			raise ValueError("Unfinished operation requires operation detail")

	@property
	def ready(self) -> bool:
		return self.status is ReadinessState.READY


class CacheInvalidator(Protocol):
	def invalidate(self, invalidation: CacheInvalidation) -> None: ...


class PendingClearer(Protocol):
	def clear(self, operation: PendingUpdate, connection: WriteConnection) -> ClearResult: ...


class RebuildRunner(Protocol):
	def rebuild(self, operation: PendingUpdate, connection: WriteConnection) -> RebuildResult: ...


class ReadOnlyTransaction(Protocol):
	def cursor(self) -> Cursor: ...


class ReadinessGuard(Protocol):
	def check(self, query: ReadScope, transaction: ReadOnlyTransaction) -> Readiness: ...


class _NoopCacheInvalidator:
	def invalidate(self, invalidation: CacheInvalidation) -> None:
		return None


def validate_prepared_payloads(scope: PendingScope) -> None:
	"""Validate every prepared file before any database row can be deleted."""

	for payload in scope.payloads:
		path = payload.temporary_path
		if not path.is_file():
			raise PayloadValidationError(f"Prepared payload is missing: {path}")
		digest = sha256()
		try:
			with path.open("rb") as stream:
				for chunk in iter(lambda: stream.read(1024 * 1024), b""):
					digest.update(chunk)
		except OSError as error:
			raise PayloadValidationError(f"Prepared payload cannot be read: {path}") from error
		if digest.hexdigest() != payload.sha256:
			raise PayloadValidationError(f"Prepared payload checksum does not match: {path}")


def _chunks(values: Sequence[WorkUnit], size: int) -> Iterator[tuple[WorkUnit, ...]]:
	for offset in range(0, len(values), size):
		yield tuple(values[offset:offset + size])


def _placeholders(values: Sequence[object]) -> str:
	return ",".join("%s" for _ in values)


def _pair_clause(pairs: Sequence[tuple[int, int]]) -> tuple[str, tuple[object, ...]]:
	clauses = " OR ".join("(date_id = %s AND ticker_id = %s)" for _ in pairs)
	return clauses, tuple(value for pair in pairs for value in pair)


def _unit_kind(unit: WorkUnit) -> UnitKind:
	return UnitKind(unit.unit_kind)


def _all_clear_units(scope: ClearScope) -> tuple[WorkUnit, ...]:
	return (*scope.raw, *scope.repairs, *scope.deltas, *scope.zscores, *scope.completion)


def _validate_clear_scope(scope: PendingScope) -> None:
	"""Reject a scope whose clear buckets do not match their declared units."""

	seen: set[WorkUnit] = set()
	for name, units, expected in (
		("raw", scope.clear.raw, UnitKind.RAW_SESSION),
		("repairs", scope.clear.repairs, UnitKind.REPAIR_ROW),
		("deltas", scope.clear.deltas, UnitKind.DELTA_SESSION),
		("zscores", scope.clear.zscores, UnitKind.ZSCORE_SESSION),
	):
		for unit in units:
			if _unit_kind(unit) is not expected:
				raise ClearingError(f"{name} clear bucket contains {unit.unit_kind}")
			if expected is UnitKind.RAW_SESSION and unit.ticker_id is not None:
				raise ClearingError("raw clear unit must address the complete session")
			if expected in (UnitKind.REPAIR_ROW, UnitKind.DELTA_SESSION, UnitKind.ZSCORE_SESSION) and unit.ticker_id is None:
				raise ClearingError(f"{name} clear unit must address a ticker")
			if unit in seen:
				raise ClearingError("Pending clear scope contains a duplicate work unit")
			seen.add(unit)
	# A raw/delta completion unit intentionally repeats the date-level unit in
	# its data-row bucket; it clears different evidence and is not a duplicate.
	seen.clear()
	for unit in scope.clear.completion:
		if _unit_kind(unit) not in (UnitKind.RAW_SESSION, UnitKind.DELTA_SESSION, UnitKind.ZSCORE_SESSION) or unit.ticker_id is not None:
			raise ClearingError("completion clear bucket contains an invalid work unit")
		if unit in seen:
			raise ClearingError("Pending clear scope contains a duplicate work unit")
		seen.add(unit)


class DatabasePendingClearer:
	"""Clear only the durable scope, committing bounded idempotent chunks."""

	def __init__(
		self,
		table_names: TableNames | None = None,
		*,
		batch_size: int = 128,
		cache_invalidator: CacheInvalidator | None = None,
	) -> None:
		if type(batch_size) is not int or batch_size < 1:
			raise ValueError("batch_size must be positive")
		self.table_names = table_names or load_table_names()
		self.batch_size = batch_size
		self.cache_invalidator = cache_invalidator or _NoopCacheInvalidator()
		self._lock_checker: Callable[[], None] | None = None

	def set_lock_checker(self, checker: Callable[[], None] | None) -> None:
		"""Install the coordinator's advisory-lock heartbeat for each chunk."""
		self._lock_checker = checker

	def _check_lock(self) -> None:
		if self._lock_checker is not None:
			self._lock_checker()

	def clear(self, operation: PendingUpdate, connection: WriteConnection) -> ClearResult:
		if not isinstance(operation, PendingUpdate) or operation.affected_rows_cleared:
			return ClearResult((), ())
		_validate_clear_scope(operation.scope)
		self._check_lock()
		cursor = connection.cursor()
		completed: list[WorkUnit] = []
		try:
			self._verify_pending_scope(cursor, connection, operation)
			for units in (
				operation.scope.clear.raw,
				operation.scope.clear.repairs,
				operation.scope.clear.deltas,
				operation.scope.clear.zscores,
			):
				for chunk in _chunks(units, self.batch_size):
					self._check_lock()
					cursor.execute("START TRANSACTION", ())
					try:
						self._delete_chunk(cursor, chunk)
						connection.commit()
					except Exception:
						connection.rollback()
						raise
					completed.extend(chunk)
			for kind in (UnitKind.RAW_SESSION, UnitKind.DELTA_SESSION, UnitKind.ZSCORE_SESSION):
				units = tuple(unit for unit in operation.scope.clear.completion if _unit_kind(unit) is kind)
				for chunk in _chunks(units, self.batch_size):
					self._check_lock()
					cursor.execute("START TRANSACTION", ())
					try:
						self._delete_chunk(cursor, chunk, completion=True)
						connection.commit()
					except Exception:
						connection.rollback()
						raise
					completed.extend(chunk)
		finally:
			cursor.close()

		# File/cache invalidation is deliberately after all database chunks.  A
		# failure leaves the durable clear flag false, so a restart repeats both
		# harmless deletes and this idempotent invalidation.
		self._check_lock()
		for invalidation in operation.scope.cache_invalidations:
			self.cache_invalidator.invalidate(invalidation)
		return ClearResult(tuple(completed), operation.scope.cache_invalidations)

	def _verify_pending_scope(self, cursor: Cursor, connection: WriteConnection, operation: PendingUpdate) -> None:
		cursor.execute("START TRANSACTION", ())
		try:
			pending = quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))
			cursor.execute(f"SELECT scope, affected_rows_cleared FROM {pending} WHERE id = %s FOR UPDATE", (operation.operation_id,))
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise ClearingError("Pending operation disappeared before clearing")
			stored_scope = rows[0][0]
			if isinstance(stored_scope, bytes):
				stored_scope = stored_scope.decode("utf-8")
			if PendingScope.from_json(stored_scope) != operation.scope:
				raise ClearingError("Pending operation scope changed before clearing")
			if rows[0][1] not in (0, False):
				raise ClearingError("Pending operation was cleared concurrently")
		finally:
			connection.rollback()

	def _delete_chunk(self, cursor: Cursor, units: Sequence[WorkUnit], *, completion: bool = False) -> None:
		if not units:
			return
		by_kind = {_unit_kind(unit) for unit in units}
		if len(by_kind) != 1:
			raise ClearingError("A clear chunk must contain one work-unit kind")
		kind = next(iter(by_kind))
		if completion:
			self._clear_completion(cursor, units)
		elif kind is UnitKind.RAW_SESSION:
			date_ids = tuple(unit.date_id for unit in units)
			if any(value is None for value in date_ids):
				raise ClearingError("Raw clear unit has no date ID")
			table = quote_identifier(self.table_names.table(TableRole.PRICES))
			cursor.execute(f"DELETE FROM {table} WHERE date_id IN ({_placeholders(date_ids)})", cast(tuple[object, ...], date_ids))
		elif kind is UnitKind.REPAIR_ROW:
			pairs = tuple((cast(int, unit.date_id), cast(int, unit.ticker_id)) for unit in units)
			clause, parameters = _pair_clause(pairs)
			cursor.execute(f"DELETE FROM {quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))} WHERE {clause}", parameters)
		elif kind is UnitKind.DELTA_SESSION:
			pairs = tuple((cast(int, unit.date_id), cast(int, unit.ticker_id)) for unit in units)
			clause, parameters = _pair_clause(pairs)
			cursor.execute(f"DELETE FROM {quote_identifier(self.table_names.table(TableRole.DELTAS))} WHERE {clause}", parameters)
		elif kind is UnitKind.ZSCORE_SESSION:
			self._delete_zscore_rows(cursor, units)
		else:
			self._clear_completion(cursor, units)

	def _delete_zscore_rows(self, cursor: Cursor, units: Sequence[WorkUnit]) -> None:
		groups: dict[str, list[tuple[int, int]]] = {}
		for unit in units:
			if unit.series_key is None or unit.window is None or unit.date_id is None or unit.ticker_id is None:
				raise ClearingError("Z-score clear unit is incomplete")
			cursor.execute(
				f"SELECT family FROM {quote_identifier(self.table_names.table(TableRole.ZSCORE_SERIES))} WHERE series_key = %s",
				(unit.series_key,),
			)
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise ClearingError("Z-score series definition is missing")
			family = rows[0][0].decode("ascii") if isinstance(rows[0][0], bytes) else str(rows[0][0])
			resolved = self.table_names.resolve_zscore_tables((unit.window,))[unit.window]
			if family == "price":
				table = resolved.price
			elif family == "price_delta":
				table = resolved.price_delta
			else:
				raise ClearingError("Unknown z-score series family")
			groups.setdefault(table, []).append((unit.date_id, unit.ticker_id))
		for table, pairs in groups.items():
			clause, parameters = _pair_clause(pairs)
			cursor.execute(f"DELETE FROM {quote_identifier(table)} WHERE {clause}", parameters)

	def _clear_completion(self, cursor: Cursor, units: Sequence[WorkUnit]) -> None:
		raw_ids = tuple(cast(int, unit.date_id) for unit in units if _unit_kind(unit) is UnitKind.RAW_SESSION)
		delta_ids = tuple(cast(int, unit.date_id) for unit in units if _unit_kind(unit) is UnitKind.DELTA_SESSION)
		zscore_units = tuple(unit for unit in units if _unit_kind(unit) is UnitKind.ZSCORE_SESSION)
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		if raw_ids:
			cursor.execute(f"UPDATE {sessions} SET raw_complete = 0, raw_available_at = NULL, raw_completed_at = NULL, raw_revision = NULL WHERE id IN ({_placeholders(raw_ids)})", cast(tuple[object, ...], raw_ids))
		if delta_ids:
			cursor.execute(f"UPDATE {sessions} SET delta_complete = 0, delta_completed_at = NULL WHERE id IN ({_placeholders(delta_ids)})", cast(tuple[object, ...], delta_ids))
		if zscore_units:
			metadata = quote_identifier(self.table_names.table(TableRole.ZSCORE_SESSIONS))
			pairs = tuple((cast(str, unit.series_key), cast(int, unit.date_id)) for unit in zscore_units)
			clause = " OR ".join("(series_key = %s AND date_id = %s)" for _ in pairs)
			parameters = tuple(value for pair in pairs for value in pair)
			cursor.execute(f"DELETE FROM {metadata} WHERE {clause}", parameters)


class DatabaseReadinessGuard:
	"""Check pending overlap inside the caller's already-open read transaction."""

	def __init__(self, table_names: TableNames | None = None) -> None:
		self.table_names = table_names or load_table_names()

	def check(self, query: ReadScope, transaction: ReadOnlyTransaction) -> Readiness:
		if not isinstance(query, ReadScope):
			raise ValueError("query must be a ReadScope")
		cursor = transaction.cursor()
		pending = quote_identifier(self.table_names.table(TableRole.PENDING_UPDATES))
		cursor.execute(f"SELECT id, operation_kind, scope_version, scope, created_at, clearing_started_at, affected_rows_cleared, failure_code, failure_detail, failed_at FROM {pending} ORDER BY id", ())
		rows = cursor.fetchall()
		if not rows:
			return Readiness(ReadinessState.READY)
		if len(rows) != 1:
			raise RecoveryError("Pending update table contains more than one active operation")
		operation = PendingUpdateRepository._pending(rows[0])
		if not _read_overlaps(query, operation.scope):
			return Readiness(ReadinessState.READY)
		unit = _first_overlapping_unit(query, operation.scope)
		return Readiness(
			ReadinessState.UNFINISHED_OPERATION,
			operation.operation_id,
			unit.unit_kind if unit is not None else "operation",
			unit.date_id if unit is not None else None,
			unit.ticker_id if unit is not None else None,
			unit.series_key if unit is not None else None,
			"An unfinished database operation overlaps this read",
		)


def _operation_units(scope: PendingScope) -> tuple[WorkUnit, ...]:
	return _all_clear_units(scope.clear) + scope.rebuild


def _unit_matches(query: WorkUnit, affected: WorkUnit) -> bool:
	if query.unit_kind != affected.unit_kind:
		return False
	if query.date_id is not None and query.date_id != affected.date_id:
		return False
	if query.ticker_id is not None and query.ticker_id != affected.ticker_id:
		return False
	if query.series_key is not None and query.series_key != affected.series_key:
		return False
	if query.window is not None and query.window != affected.window:
		return False
	return True


def _read_overlaps(query: ReadScope, scope: PendingScope) -> bool:
	units = _operation_units(scope)
	if query.units:
		return any(_unit_matches(requested, affected) for requested in query.units for affected in units)
	return any(unit.date_id in query.date_ids for unit in units if unit.date_id is not None)


def _first_overlapping_unit(query: ReadScope, scope: PendingScope) -> WorkUnit | None:
	units = _operation_units(scope)
	if query.units:
		return next((affected for requested in query.units for affected in units if _unit_matches(requested, affected)), None)
	return next((unit for unit in units if unit.date_id in query.date_ids), None)


@dataclass
class _ReadTransaction:
	_cursor: Cursor

	def cursor(self) -> Cursor:
		return self._cursor


@contextmanager
def consistent_read_transaction(source: SourceConfig, connect: WriteConnectionFactory) -> Iterator[ReadOnlyTransaction]:
	"""Open a repeatable-read snapshot for guard plus dependent reads."""

	connection = connect(source.database_config)
	cursor = connection.cursor()
	sqlite_compatible = type(getattr(connection, "connection", None)).__module__ == "sqlite3"
	try:
		if not sqlite_compatible:
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())
		cursor.execute("START TRANSACTION", ())
		yield _ReadTransaction(cursor)
		connection.rollback()
	except Exception:
		connection.rollback()
		raise
	finally:
		cursor.close()
		connection.close()


class PendingOperationCoordinator:
	"""Serialize, recover, register, and clear operations before rebuild."""

	def __init__(
		self,
		source: SourceConfig,
		connect: WriteConnectionFactory,
		planner: DependencyPlannerProtocol,
		clearer: PendingClearer,
		*,
		pending: PendingUpdateRepositoryProtocol | None = None,
		rebuild_runner: RebuildRunner | None = None,
		table_names: TableNames | None = None,
		payload_validator: Callable[[PendingScope], None] = validate_prepared_payloads,
		clock: Callable[[], datetime] | None = None,
		lock_factory: Callable[[], ContextManager[object]] | None = None,
		lock_timeout_seconds: int = 60,
	) -> None:
		if type(lock_timeout_seconds) is not int or lock_timeout_seconds < 0:
			raise ValueError("lock_timeout_seconds must be a non-negative integer")
		self.source = source
		self.connect = connect
		self.planner = planner
		self.clearer = clearer
		self.pending = pending or PendingUpdateRepository(source, connect, table_names)
		self.rebuild_runner = rebuild_runner
		self.payload_validator = payload_validator
		self.clock = clock or (lambda: datetime.now(timezone.utc))
		self.lock_factory = lock_factory or (lambda: MaintenanceWriterLock(source, connect, timeout_seconds=lock_timeout_seconds))

	def accept(self, request: UpdateRequest, snapshot: DatabaseSnapshot | Callable[[], DatabaseSnapshot]) -> PendingUpdate:
		"""Recover the active operation, then register and clear one new scope.

		Without a rebuild runner this method intentionally returns a pending row
		with ``affected_rows_cleared=True``.  The next stage must supply the runner
		before a new update can be accepted; no recalculation is hidden here.
		"""

		with self.lock_factory() as lock:
			assert_lock = getattr(lock, "assert_held", None)
			if callable(assert_lock):
				assert_lock()
			active = self.pending.load_active()
			if active is not None:
				active = self._finish_active(active, assert_lock)
				if active is not None:
					if self.rebuild_runner is None:
						raise RebuildRequiredError("The cleared pending operation requires a later rebuild stage")
					active = self.pending.load_active()
					if active is not None:
						raise RecoveryError("Pending operation remained active after rebuild")

			assert_lock() if callable(assert_lock) else None
			current_snapshot = snapshot() if callable(snapshot) else snapshot
			scope = self.planner.plan(request, current_snapshot)
			self.payload_validator(scope)
			created_at = self.clock().astimezone(timezone.utc)
			operation = self.pending.register(scope, created_at)
			cleared = self._clear_registered(operation, assert_lock)
			if self.rebuild_runner is not None:
				self._run_rebuild(cleared, assert_lock)
			return cleared

	def _finish_active(self, operation: PendingUpdate, assert_lock: object) -> PendingUpdate | None:
		self.payload_validator(operation.scope)
		if not operation.affected_rows_cleared:
			operation = self._clear_registered(operation, assert_lock)
		if self.rebuild_runner is None:
			return operation
		self._run_rebuild(operation, assert_lock)
		return None

	def _clear_registered(self, operation: PendingUpdate, assert_lock: object) -> PendingUpdate:
		if operation.affected_rows_cleared:
			return operation
		self.pending.begin_clearing(operation.operation_id, self.clock().astimezone(timezone.utc))
		connection = self.connect(self.source.database_config)
		try:
			if callable(assert_lock):
				if hasattr(self.clearer, "set_lock_checker"):
					cast(Callable[[Callable[[], None] | None], None], getattr(self.clearer, "set_lock_checker"))(cast(Callable[[], None], assert_lock))
				assert_lock()
			self.clearer.clear(operation, connection)
			if callable(assert_lock):
				assert_lock()
			self.pending.mark_cleared(operation.operation_id, self.clock().astimezone(timezone.utc))
		except Exception as error:
			try:
				self.pending.record_failure(operation.operation_id, FailureDetail("clear_failed", "Clearing failed; the operation remains safely retryable", self.clock().astimezone(timezone.utc)), self.clock().astimezone(timezone.utc))
			except Exception:
				pass
			if isinstance(error, RecoveryError) or (isinstance(error, RuntimeError) and "lock" in str(error).lower()):
				raise
			raise ClearingError("Pending operation clearing failed; retry is safe") from error
		finally:
			if hasattr(self.clearer, "set_lock_checker"):
				cast(Callable[[Callable[[], None] | None], None], getattr(self.clearer, "set_lock_checker"))(None)
			connection.close()
		loaded = self.pending.load_active()
		if loaded is None:
			raise RecoveryError("Pending operation disappeared after clearing")
		return loaded

	def _run_rebuild(self, operation: PendingUpdate, assert_lock: object) -> None:
		if not operation.affected_rows_cleared:
			raise RecoveryError("Rebuild is unreachable before affected_rows_cleared")
		if callable(assert_lock):
			assert_lock()
		connection = self.connect(self.source.database_config)
		try:
			result = self.rebuild_runner.rebuild(operation, connection) if self.rebuild_runner is not None else None
			if result is None or result.failed_units or result.deferred_units:
				raise RecoveryError("Rebuild did not complete the persisted scope")
			completed = set(result.committed_units) | set(result.explicit_non_ready)
			if completed != set(operation.scope.rebuild):
				raise RecoveryError("Rebuild did not publish every persisted work unit")
		finally:
			connection.close()
		self.pending.delete_completed(operation.operation_id, self.clock().astimezone(timezone.utc))

