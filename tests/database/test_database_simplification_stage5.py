from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256

import pytest

from support.ingestion import FakeDatabase

from stockguidance.database.dependency_planner import DependencyPlanner
from stockguidance.database.operation_recovery import (
	DatabasePendingClearer,
	DatabaseReadinessGuard,
	PayloadValidationError,
	PendingOperationCoordinator,
	ReadScope,
	ReadinessState,
	RebuildRequiredError,
	consistent_read_transaction,
)
from stockguidance.database.repositories.simplified import PendingUpdateRepository

from stockguidance.database.simplified_contract import (
	CacheInvalidation,
	ClearScope,
	OperationKind,
	PendingScope,
	PreparedPayload,
	UnitKind,
	WorkUnit,
)
from stockguidance.database.simplified_schema import initialize_simplified_schema
from stockguidance.market_data.identity import data_configuration_key


NOW = datetime(2024, 7, 8, 19, 30, tzinfo=timezone.utc)
SERIES = "a" * 64


@dataclass
class CacheSpy:
	invalidations: list[CacheInvalidation]

	def invalidate(self, invalidation: CacheInvalidation) -> None:
		self.invalidations.append(invalidation)


def _scope(source, *, payloads=(), rebuild=()) -> PendingScope:
	date_value = date(2024, 7, 1)
	return PendingScope(
		1,
		OperationKind.FORCED_REPLACEMENT,
		data_configuration_key(source.data_configuration),
		source.calendar_id,
		NOW,
		NOW,
		tuple(payloads),
		ClearScope(
			raw=(WorkUnit(UnitKind.RAW_SESSION.value, 1, None, date_value),),
			repairs=(WorkUnit(UnitKind.REPAIR_ROW.value, 1, 1, date_value),),
			deltas=(WorkUnit(UnitKind.DELTA_SESSION.value, 1, 1, date_value),),
			zscores=(WorkUnit(UnitKind.ZSCORE_SESSION.value, 1, 1, date_value, SERIES, 20),),
			completion=(
				WorkUnit(UnitKind.RAW_SESSION.value, 1, None, date_value),
				WorkUnit(UnitKind.DELTA_SESSION.value, 1, None, date_value),
				WorkUnit(UnitKind.ZSCORE_SESSION.value, 1, None, date_value, SERIES, 20),
			),
		),
		tuple(rebuild),
		(CacheInvalidation("training-dataset", (date_value,)),),
	)


def _seed_database(source, database: FakeDatabase) -> None:
	initialize_simplified_schema(source, database, windows=(20,))
	database.sql("INSERT INTO transaction_dates (transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision, delta_complete, delta_completed_at) VALUES (?, 1, ?, ?, ?, 1, ?)", (date(2024, 7, 1).isoformat(), NOW.isoformat(), NOW.isoformat(), "b" * 64, NOW.isoformat()))
	database.sql("INSERT INTO ticker_symbol (label) VALUES (?)", ("AAA",))
	database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, open, close, state_id) VALUES (1, 1, ?, ?, 1)", ("10.00000000", "11.00000000"))
	database.sql("INSERT INTO daily_delta_stock_data (ticker_id, date_id, calculation_status, quality_flags, gap_length, provenance, completed_at) VALUES (1, 1, ?, 0, 0, ?, ?)", ("ready", "{}", NOW.isoformat()))
	database.sql("INSERT INTO stockguidance_repair_metadata (date_id, ticker_id, revision, state_id, original_values, invalid_fields, recorded_at) VALUES (1, 1, ?, 2, ?, ?, ?)", ("c" * 64, "[\"10\",null,null,null,null,null,null]", "[\"close\"]", NOW.isoformat()))
	database.sql("INSERT INTO stockguidance_zscore_series (series_key, schema_version, source_scope, calendar_id, family, window, calculation_version, delta_definition, precision, missing_value_policy, created_at) VALUES (?, 1, ?, ?, ?, 20, ?, ?, ?, ?, ?)", (SERIES, "d" * 64, "XNYS", "price", "rolling-population-zscore-v1", "not_applicable", "float32", "null_status_v1", NOW.isoformat()))
	database.sql("INSERT INTO zscores_price_20 (date_id, ticker_id, zscore, status, provenance) VALUES (1, 1, 1.25, 0, ?)", ("{}",))
	database.sql("INSERT INTO stockguidance_zscore_sessions (series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest) VALUES (?, 1, ?, ?, 1, ?, 1, 1, ?, ?)", (SERIES, "e" * 64, "f" * 64, NOW.isoformat(), "{}", "0" * 64))


def _connection(database: FakeDatabase):
	return database(database.path)


def test_bounded_clear_is_idempotent_and_preserves_unrelated_rows(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "clear.sqlite")
	_seed_database(source_config, database)
	# A second date/ticker is deliberately outside the durable scope.
	database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (date(2024, 7, 2).isoformat(),))
	database.sql("INSERT INTO ticker_symbol (label) VALUES (?)", ("BBB",))
	database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, state_id) VALUES (2, 2, 1)")
	repository = PendingUpdateRepository(source_config, database)
	operation = repository.register(_scope(source_config), NOW)
	repository.begin_clearing(operation.operation_id, NOW)
	cache = CacheSpy([])
	clearer = DatabasePendingClearer(batch_size=1, cache_invalidator=cache)
	connection = _connection(database)
	try:
		result = clearer.clear(operation, connection)
	finally:
		connection.close()
	assert len(result.completed_units) == 7
	assert cache.invalidations == list(operation.scope.cache_invalidations)
	assert repository.load_active() is not None and not repository.load_active().affected_rows_cleared
	repository.mark_cleared(operation.operation_id, NOW)
	assert repository.load_active().affected_rows_cleared
	assert database.sql("SELECT COUNT(*) FROM raw_stock_data WHERE date_id = 1") == [(0,)]
	assert database.sql("SELECT COUNT(*) FROM daily_delta_stock_data WHERE date_id = 1") == [(0,)]
	assert database.sql("SELECT COUNT(*) FROM stockguidance_repair_metadata WHERE date_id = 1") == [(0,)]
	assert database.sql("SELECT COUNT(*) FROM zscores_price_20 WHERE date_id = 1") == [(0,)]
	assert database.sql("SELECT raw_complete, delta_complete FROM transaction_dates WHERE id = 1") == [(0, 0)]
	assert database.sql("SELECT COUNT(*) FROM raw_stock_data WHERE date_id = 2") == [(1,)]
	assert database.sql("SELECT COUNT(*) FROM stockguidance_source") == [(1,)]
	assert database.sql("SELECT COUNT(*) FROM stockguidance_data_states") == [(4,)]
	# A second clear after the flag transition is a harmless no-op.
	connection = _connection(database)
	try:
		assert clearer.clear(repository.load_active(), connection).completed_units == ()
	finally:
		connection.close()


def test_interrupted_chunk_leaves_false_and_restart_repeats_safely(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "interrupt.sqlite")
	_seed_database(source_config, database)
	repository = PendingUpdateRepository(source_config, database)
	operation = repository.register(_scope(source_config), NOW)
	repository.begin_clearing(operation.operation_id, NOW)
	database.fail_on = "daily_delta_stock_data"
	connection = _connection(database)
	try:
		with pytest.raises(RuntimeError):
			DatabasePendingClearer(batch_size=1).clear(operation, connection)
	finally:
		connection.close()
	active = repository.load_active()
	assert active is not None and not active.affected_rows_cleared
	assert database.sql("SELECT COUNT(*) FROM raw_stock_data WHERE date_id = 1") == [(0,)]
	assert database.sql("SELECT COUNT(*) FROM daily_delta_stock_data WHERE date_id = 1") == [(1,)]
	connection = _connection(database)
	try:
		DatabasePendingClearer(batch_size=1).clear(active, connection)
	finally:
		connection.close()
	repository.mark_cleared(operation.operation_id, NOW)
	assert repository.load_active().affected_rows_cleared


def test_failure_before_clear_flag_commit_is_recoverable(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "flag.sqlite")
	_seed_database(source_config, database)
	repository = PendingUpdateRepository(source_config, database)
	operation = repository.register(_scope(source_config), NOW)
	repository.begin_clearing(operation.operation_id, NOW)
	connection = _connection(database)
	try:
		DatabasePendingClearer(batch_size=1).clear(operation, connection)
	finally:
		connection.close()
	# Fail the flag UPDATE after all row deletes have committed.
	database.fail_on = "SET affected_rows_cleared"
	with pytest.raises(RuntimeError):
		repository.mark_cleared(operation.operation_id, NOW)
	active = repository.load_active()
	assert active is not None and not active.affected_rows_cleared
	connection = _connection(database)
	try:
		DatabasePendingClearer(batch_size=1).clear(active, connection)
	finally:
		connection.close()
	repository.mark_cleared(operation.operation_id, NOW)
	assert repository.load_active().affected_rows_cleared


def test_reader_guard_uses_same_transaction_and_allows_unrelated_date(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "reader.sqlite")
	_seed_database(source_config, database)
	database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (date(2024, 7, 2).isoformat(),))
	repository = PendingUpdateRepository(source_config, database)
	operation = repository.register(_scope(source_config), NOW)
	guard = DatabaseReadinessGuard()
	with consistent_read_transaction(source_config, database) as transaction:
		assert guard.check(ReadScope(date_ids=(1,)), transaction).status is ReadinessState.UNFINISHED_OPERATION
		assert guard.check(ReadScope(date_ids=(2,)), transaction).status is ReadinessState.READY


def test_payload_is_validated_before_registration(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "payload.sqlite")
	initialize_simplified_schema(source_config, database, windows=(20,))
	payload_path = tmp_path / "prepared.ndjson"
	payload_path.write_bytes(b"prepared payload")
	payload = PreparedPayload("payload-1", date(2024, 7, 1), payload_path, sha256(payload_path.read_bytes()).hexdigest(), "1" * 64, NOW, NOW)
	bad = PreparedPayload(payload.payload_id, payload.session_date, payload_path, "2" * 64, payload.raw_id, payload.available_at, payload.prepared_at)
	repository = PendingUpdateRepository(source_config, database)
	with pytest.raises(PayloadValidationError, match="checksum"):
		from stockguidance.database.operation_recovery import validate_prepared_payloads
		validate_prepared_payloads(_scope(source_config, payloads=(bad,)))
	assert repository.load_active() is None


def test_coordinator_does_not_rebuild_before_clear_and_blocks_next_request(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "coordinator.sqlite")
	_seed_database(source_config, database)
	repository = PendingUpdateRepository(source_config, database)
	operation = repository.register(_scope(source_config, rebuild=(WorkUnit(UnitKind.RAW_SESSION.value, 1, None, date(2024, 7, 1)),)), NOW)
	repository.begin_clearing(operation.operation_id, NOW)

	class Lock:
		def __enter__(self):
			return self
		def __exit__(self, *_args):
			return None
		def assert_held(self):
			return None

	class Rebuild:
		def __init__(self):
			self.calls = []
		def rebuild(self, operation, connection):
			self.calls.append(operation.affected_rows_cleared)
			return type("Result", (), {"failed_units": (), "deferred_units": ()})()

	rebuild = Rebuild()
	coordinator = PendingOperationCoordinator(source_config, database, DependencyPlanner(), DatabasePendingClearer(batch_size=1), pending=repository, rebuild_runner=rebuild, lock_factory=lambda: Lock())
	# Recovery clears first; the injected runner sees only the committed true flag.
	request = object()
	with pytest.raises(Exception):
		coordinator.accept(request, object())
	assert rebuild.calls == [True]


def test_lock_loss_stops_before_next_clear_chunk(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "lock-loss.sqlite")
	_seed_database(source_config, database)
	repository = PendingUpdateRepository(source_config, database)
	operation = repository.register(_scope(source_config), NOW)

	class LostLock:
		def __init__(self):
			self.calls = 0
		def __enter__(self):
			return self
		def __exit__(self, *_args):
			return None
		def assert_held(self):
			self.calls += 1
			if self.calls > 2:
				raise RuntimeError("Maintenance writer lock was lost")

	lock = LostLock()
	coordinator = PendingOperationCoordinator(source_config, database, DependencyPlanner(), DatabasePendingClearer(batch_size=1), pending=repository, lock_factory=lambda: lock)
	with pytest.raises(RuntimeError, match="lock"):
		coordinator._clear_registered(operation, lock.assert_held)
	assert repository.load_active() is not None and not repository.load_active().affected_rows_cleared
