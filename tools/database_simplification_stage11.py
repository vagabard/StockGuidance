"""Run Stage 11 recovery and regression acceptance on disposable MySQL.

The parent owns one explicitly prefixed schema.  Worker subprocesses are
deliberately killed after a durable clear or rebuild commit, then restarted
against the same pending row.  The fixture is synthetic and never selects the
configured database.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, cast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
	sys.path.insert(0, str(SRC))

from stockguidance.calendar.sessions import TradingSession  # noqa: E402
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity  # noqa: E402
from stockguidance.database.configuration import load_database_configuration  # noqa: E402
from stockguidance.database.dependency_planner import DependencyPlanner  # noqa: E402
from stockguidance.database.numerical_rebuild import SimplifiedNumericalRebuildRunner  # noqa: E402
from stockguidance.database.operation_recovery import (  # noqa: E402
	DatabasePendingClearer,
	DatabaseReadinessGuard,
	PendingOperationCoordinator,
	ReadScope,
	ReadinessState,
	RebuildResult,
	consistent_read_transaction,
)
from stockguidance.database.repositories.simplified import PendingUpdateRepository  # noqa: E402
from stockguidance.database.simplified_contract import (  # noqa: E402
	ClearScope,
	OperationKind,
	PendingScope,
	PendingUpdate,
	UnitKind,
	WorkUnit,
)
from stockguidance.database.simplified_schema import initialize_simplified_schema  # noqa: E402
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory  # noqa: E402
from stockguidance.market_data.identity import data_configuration_key  # noqa: E402
from stockguidance.performance.database_simplification_stage2 import (  # noqa: E402
	_connector,
	_connect,
	_create_schema,
	_drop_schema,
	_quote,
)
from stockguidance.database.table_names import validate_identifier  # noqa: E402
from stockguidance.workflows.market_lock import MaintenanceWriterLock  # noqa: E402
from stockguidance.zscores.specification import ZScoreFamily  # noqa: E402
from stockguidance.zscores.storage import ZScoreSeriesDefinition  # noqa: E402


NOW = datetime(2026, 10, 26, 19, 30, tzinfo=timezone.utc)
SCHEMA_PREFIX = "stockguidance_disposable_"


@dataclass(frozen=True)
class FixtureCalendar:
	items: tuple[TradingSession, ...]

	calendar_id = "XNYS"
	version = "stage11-fixture"
	timezone = timezone.utc
	start = date(2024, 7, 1)
	end = date(2024, 7, 4)

	def session(self, session_date: date) -> TradingSession:
		return next(item for item in self.items if item.session_date == session_date)

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		return tuple(item for item in self.items if start <= item.session_date <= end)

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		index = self.items.index(session) + offset
		if index < 0 or index >= len(self.items):
			raise IndexError("stage11 fixture calendar boundary")
		return self.items[index]


def _session(day: date, sequence: int) -> TradingSession:
	return TradingSession("XNYS", day, sequence, datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc), datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc))


def _calendar() -> FixtureCalendar:
	days = (date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3), date(2024, 7, 4))
	return FixtureCalendar(tuple(_session(day, index) for index, day in enumerate(days, start=1)))


def _source(dbinfo: Path) -> SourceConfig:
	identity = SourceIdentity("fixture-provider", "grouped-daily", "us-stocks", "all-us-stocks", PriceAdjustment.UNADJUSTED)
	return SourceConfig(identity, ROOT / "data", dbinfo, "stage11-fixture")


def _validate_disposable_schema(schema: str, configured_database: str) -> None:
	if not schema.startswith(SCHEMA_PREFIX) or schema.casefold() == configured_database.casefold():
		raise SystemExit("--schema must be a distinct stockguidance_disposable_* schema")
	validate_identifier(schema, "disposable schema")


class _NoRawRunner:
	def rebuild(self, _operation: PendingUpdate, _connection: WriteConnection) -> RebuildResult:
		return RebuildResult()


class _EmptyPlanner:
	def __init__(self, source: SourceConfig) -> None:
		self.source = source

	def plan(self, _request: object, _snapshot: object) -> PendingScope:
		return PendingScope(
			1,
			OperationKind.DERIVED_REPAIR,
			data_configuration_key(self.source.data_configuration),
			self.source.calendar_id,
			NOW,
			NOW,
			(),
			ClearScope(),
			(),
			(),
		)


class _CommitBarrierConnection:
	"""Proxy that pauses after one already-committed transaction."""

	def __init__(self, connection: WriteConnection, event: Path, pause_after: int) -> None:
		self.connection = connection
		self.event = event
		self.pause_after = pause_after
		self.commits = 0

	def cursor(self) -> Any:
		return self.connection.cursor()

	def commit(self) -> None:
		self.connection.commit()
		self.commits += 1
		if self.pause_after > 0 and self.commits == self.pause_after:
			self.event.write_text(f"committed:{self.commits}\n", encoding="utf-8")
			while True:
				time.sleep(0.1)

	def rollback(self) -> None:
		self.connection.rollback()

	def close(self) -> None:
		self.connection.close()


def _scope_for_clear(source: SourceConfig) -> PendingScope:
	return PendingScope(
		1,
		OperationKind.FORCED_REPLACEMENT,
		data_configuration_key(source.data_configuration),
		source.calendar_id,
		NOW,
		NOW,
		(),
		ClearScope(
			raw=(
				WorkUnit(UnitKind.RAW_SESSION.value, 1, None, date(2024, 7, 1)),
				WorkUnit(UnitKind.RAW_SESSION.value, 2, None, date(2024, 7, 2)),
			),
			completion=(
				WorkUnit(UnitKind.RAW_SESSION.value, 1, None, date(2024, 7, 1)),
				WorkUnit(UnitKind.RAW_SESSION.value, 2, None, date(2024, 7, 2)),
			),
		),
		(),
		(),
	)


def _numerical_scope(source: SourceConfig, *, clear: bool) -> PendingScope:
	target = date(2024, 7, 4)
	definition = ZScoreSeriesDefinition(
		data_configuration_key(source.data_configuration),
		"XNYS",
		ZScoreFamily.PRICE,
		2,
		"rolling-population-zscore-v1",
		"float32",
		"not_applicable",
	)
	delta = WorkUnit(UnitKind.DELTA_SESSION.value, 4, 1, target)
	zscore = WorkUnit(UnitKind.ZSCORE_SESSION.value, 4, None, target, definition.series_key, 2)
	clear_scope = ClearScope(
		deltas=(delta,),
		# The ticker-scoped clear unit is also the durable affected-ticker
		# evidence used by the z-score writer when the operation starts empty.
		zscores=(WorkUnit(UnitKind.ZSCORE_SESSION.value, 4, 1, target, definition.series_key, 2),),
		completion=(
			WorkUnit(UnitKind.DELTA_SESSION.value, 4, None, target),
			WorkUnit(UnitKind.ZSCORE_SESSION.value, 4, None, target, definition.series_key, 2),
		) if clear else (),
	)
	return PendingScope(
		1,
		OperationKind.FORCED_REPLACEMENT,
		data_configuration_key(source.data_configuration),
		"XNYS",
		NOW,
		NOW,
		(),
		clear_scope,
		(delta, zscore),
		(),
	)


def _seed_fixture(source: SourceConfig, factory: WriteConnectionFactory, calendar: FixtureCalendar) -> None:
	connection = factory(Path("unused"))
	cursor = connection.cursor()
	try:
		for item in calendar.items:
			cursor.execute(
				"INSERT INTO transaction_dates (transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision) VALUES (%s,1,%s,%s,%s)",
				(item.session_date, NOW, NOW, "a" * 64),
			)
		cursor.execute("INSERT INTO ticker_symbol (label, exchange) VALUES (%s,%s)", ("AAA", "XNYS"))
		cursor.execute("INSERT INTO ticker_symbol (label, exchange) VALUES (%s,%s)", ("BBB", "XNYS"))
		definition = ZScoreSeriesDefinition(
			data_configuration_key(source.data_configuration),
			"XNYS",
			ZScoreFamily.PRICE,
			2,
			"rolling-population-zscore-v1",
			"float32",
			"not_applicable",
		)
		cursor.execute(
			"INSERT INTO stockguidance_zscore_series (series_key, schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy, created_at) VALUES (%s,1,%s,%s,%s,2,%s,%s,%s,%s,%s)",
			(definition.series_key, data_configuration_key(source.data_configuration), "XNYS", "price", "rolling-population-zscore-v1", "not_applicable", "float32", "null_status_v1", NOW),
		)
		for date_id, close in enumerate((Decimal("10.00000000"), Decimal("11.00000000"), Decimal("12.00000000"), Decimal("15.00000000")), start=1):
			for ticker_id, ticker_close in ((1, close), (2, close + Decimal("20.00000000"))):
				cursor.execute(
					"INSERT INTO raw_stock_data (ticker_id,date_id,open,high,low,close,volume,vwap,transactions,state_id,observed_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)",
					(ticker_id, date_id, ticker_close, ticker_close + 1, ticker_close - 1, ticker_close, Decimal("100.00000000"), ticker_close, 5, NOW),
				)
		connection.commit()
	finally:
		cursor.close()
		connection.close()


def _new_operation(source: SourceConfig, factory: WriteConnectionFactory, scope: PendingScope) -> PendingUpdate:
	repository = PendingUpdateRepository(source, factory)
	return repository.register(scope, NOW)


def _mark_empty_clear(source: SourceConfig, factory: WriteConnectionFactory, operation: PendingUpdate) -> None:
	repository = PendingUpdateRepository(source, factory)
	repository.begin_clearing(operation.operation_id, NOW)
	repository.mark_cleared(operation.operation_id, NOW)


def _run_worker(arguments: argparse.Namespace) -> int:
	configuration = load_database_configuration(arguments.dbinfo_file)
	_validate_disposable_schema(arguments.schema, configuration.database)
	connector, driver = _connector(configuration)
	source = _source(arguments.dbinfo_file)
	factory = cast(WriteConnectionFactory, cast(Callable[[Path], Any], lambda _path: _connect(configuration, connector, driver, schema=arguments.schema)))
	repository = PendingUpdateRepository(source, factory)
	operation = repository.load_active()
	if operation is None:
		raise RuntimeError("Stage 11 worker found no active pending operation")
	calendar = _calendar()
	event = arguments.event
	if arguments.worker == "clear":
		with MaintenanceWriterLock(source, factory, timeout_seconds=20) as lock:
			operation = repository.load_active()
			if operation is None:
				raise RuntimeError("Stage 11 clear worker lost the pending operation")
			if not operation.affected_rows_cleared:
				repository.begin_clearing(operation.operation_id, NOW)
				connection = _connect(configuration, connector, driver, schema=arguments.schema)
				wrapped = _CommitBarrierConnection(connection, event, arguments.pause_after) if event is not None else connection
				clearer = DatabasePendingClearer(batch_size=1)
				clearer.set_lock_checker(lock.assert_held)
				try:
					clearer.clear(operation, wrapped)
				finally:
					wrapped.close()
				repository.mark_cleared(operation.operation_id, NOW)
		if arguments.delete:
			repository.delete_completed(operation.operation_id, NOW)
		return 0
	if arguments.worker == "rebuild":
		if not operation.affected_rows_cleared:
			raise RuntimeError("Stage 11 rebuild worker was given an uncleared operation")
		runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]
		connection = _connect(configuration, connector, driver, schema=arguments.schema)
		wrapped = _CommitBarrierConnection(connection, event, arguments.pause_after) if event is not None else connection
		try:
			result = runner.rebuild(operation, wrapped)
		finally:
			wrapped.close()
		completed = set(result.committed_units) | set(result.explicit_non_ready)
		if result.failed_units or result.deferred_units or completed != set(operation.scope.rebuild):
			raise RuntimeError("Stage 11 rebuild did not cover its immutable scope")
		if arguments.delete:
			repository.delete_completed(operation.operation_id, NOW)
		return 0
	if arguments.worker == "queued":
		runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]
		coordinator = PendingOperationCoordinator(
			source,
			factory,
			_EmptyPlanner(source),
			DatabasePendingClearer(batch_size=1),
			pending=repository,
			rebuild_runner=runner,
			lock_timeout_seconds=20,
			clock=lambda: NOW,
		)
		coordinator.accept(object(), None)  # type: ignore[arg-type]
		return 0
	raise RuntimeError("Unknown Stage 11 worker")


def _spawn(arguments: argparse.Namespace, worker: str, *, event: Path | None = None, pause_after: int = 0, delete: bool = False) -> subprocess.Popen[str]:
	command = [sys.executable, str(Path(__file__).resolve()), "--dbinfo-file", str(arguments.dbinfo_file), "--schema", arguments.schema, "--worker", worker, "--pause-after", str(pause_after)]
	if event is not None:
		command.extend(("--event", str(event)))
	if delete:
		command.append("--delete")
	return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_marker(process: subprocess.Popen[str], marker: Path, label: str) -> None:
	deadline = time.monotonic() + 30
	while time.monotonic() < deadline:
		if marker.exists():
			return
		if process.poll() is not None:
			raise RuntimeError(f"Stage 11 {label} worker exited before its interruption marker")
		time.sleep(0.1)
	process.kill()
	process.wait(timeout=10)
	raise RuntimeError(f"Stage 11 {label} worker did not reach its interruption marker")


def _kill_at_marker(arguments: argparse.Namespace, worker: str, directory: Path, pause_after: int, label: str) -> None:
	marker = directory / f"{label}.marker"
	process = _spawn(arguments, worker, event=marker, pause_after=pause_after)
	try:
		_wait_marker(process, marker, label)
		process.kill()
		process.wait(timeout=10)
	finally:
		if process.poll() is None:
			process.kill()
			process.wait(timeout=10)


def _query(configuration: Any, connector: Any, driver: str, schema: str, statement: str, parameters: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
	connection = _connect(configuration, connector, driver, schema=schema)
	cursor = connection.cursor()
	try:
		cursor.execute(statement, parameters)
		return [tuple(row) for row in cursor.fetchall()]
	finally:
		cursor.close()
		connection.close()


def _assert(condition: bool, message: str) -> None:
	if not condition:
		raise AssertionError(message)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--dbinfo-file", type=Path, required=True, help="Explicit disposable MySQL configuration")
	parser.add_argument("--schema", default="stockguidance_disposable_stage11_20261026", help="Disposable schema name")
	parser.add_argument("--worker", choices=("clear", "rebuild", "queued"))
	parser.add_argument("--event", type=Path)
	parser.add_argument("--pause-after", type=int, default=0)
	parser.add_argument("--delete", action="store_true")
	arguments = parser.parse_args(argv)
	configuration = load_database_configuration(arguments.dbinfo_file)
	_validate_disposable_schema(arguments.schema, configuration.database)
	if arguments.worker is not None:
		return _run_worker(arguments)
	if arguments.pause_after < 0:
		raise SystemExit("--pause-after must be non-negative")

	connector, driver = _connector(configuration)
	admin = _connect(configuration, connector, driver, autocommit=True)
	source = _source(arguments.dbinfo_file)
	factory = cast(WriteConnectionFactory, cast(Callable[[Path], Any], lambda _path: _connect(configuration, connector, driver, schema=arguments.schema)))
	calendar = _calendar()
	with tempfile.TemporaryDirectory(prefix="stockguidance-stage11-") as temporary:
		directory = Path(temporary)
		try:
			cursor = admin.cursor()
			try:
				cursor.execute("DROP DATABASE IF EXISTS " + _quote(arguments.schema))
				_create_schema(admin, arguments.schema)
			finally:
				cursor.close()
			initialize_simplified_schema(source, factory, windows=(2,))
			_seed_fixture(source, factory, calendar)
			repository = PendingUpdateRepository(source, factory)

			# A process killed after the first clear commit leaves a false flag and a
			# partially cleared database. Restart repeats the persisted scope and
			# removes only the completed operation.
			clear_operation = _new_operation(source, factory, _scope_for_clear(source))
			clear_scope_json = clear_operation.scope.to_json()
			_kill_at_marker(arguments, "clear", directory, 1, "clear-after-first-commit")
			active = repository.load_active()
			if active is None:
				raise RuntimeError("Killed clear removed its pending operation")
			_assert(not active.affected_rows_cleared, "Killed clear did not leave affected_rows_cleared false")
			_assert(active.scope.to_json() == clear_scope_json, "Clear scope changed after process termination")
			_assert(_query(configuration, connector, driver, arguments.schema, "SELECT COUNT(*) FROM raw_stock_data WHERE date_id = 1") == [(0,)], "Committed clear chunk was not durable")
			_assert(_query(configuration, connector, driver, arguments.schema, "SELECT COUNT(*) FROM raw_stock_data WHERE date_id = 2") == [(2,)], "Uncommitted clear chunk was visible")
			with consistent_read_transaction(source, factory) as transaction:
				readiness = DatabaseReadinessGuard().check(ReadScope(date_ids=(3,)), transaction)
				_assert(readiness.status is ReadinessState.READY, "Unrelated read was blocked by a clear")
			process = _spawn(arguments, "clear", delete=True)
			return_code = process.wait(timeout=30)
			_assert(return_code == 0, "Clear restart failed")
			_assert(repository.load_active() is None, "Completed clear pending row was not removed")
			_assert(_query(configuration, connector, driver, arguments.schema, "SELECT COUNT(*) FROM raw_stock_data WHERE date_id = 3") == [(2,)], "Unrelated raw data changed")

			# First numerical operation: kill after delta commit while the z-score
			# remains unfinished. A second coordinator waits for the writer lock,
			# resumes the active scope, and only then plans its empty next request.
			first_rebuild = _new_operation(source, factory, _numerical_scope(source, clear=False))
			_mark_empty_clear(source, factory, first_rebuild)
			_rebuild_process = _spawn(arguments, "rebuild", event=directory / "rebuild-after-delta.marker", pause_after=1)
			_wait_marker(_rebuild_process, directory / "rebuild-after-delta.marker", "rebuild-after-delta")
			with consistent_read_transaction(source, factory) as transaction:
				readiness = DatabaseReadinessGuard().check(ReadScope(date_ids=(4,)), transaction)
				_assert(readiness.status is ReadinessState.UNFINISHED_OPERATION, "Overlapping reader was not blocked during rebuild")
			queued = _spawn(arguments, "queued")
			time.sleep(0.5)
			_assert(queued.poll() is None, "Second writer did not wait for the active operation")
			_rebuild_process.kill()
			_rebuild_process.wait(timeout=10)
			queued_code = queued.wait(timeout=40)
			if queued_code != 0:
				_, queued_error = queued.communicate()
				raise RuntimeError(f"Queued correction did not resume and complete the active operation (exit {queued_code}): {queued_error[-2000:]}")
			_assert(repository.load_active() is None, "Queued correction left a pending row")
			delta_row = _query(configuration, connector, driver, arguments.schema, "SELECT close_ratio_to_prior, calculation_status, quality_flags FROM daily_delta_stock_data WHERE date_id = 4 AND ticker_id = 1")
			_assert(len(delta_row) == 1 and float(str(delta_row[0][0])) == 1.25 and str(delta_row[0][1]) == "not_yet_available" and int(str(delta_row[0][2])) == 0, f"Numerical delta value/state/quality changed on recovery: {delta_row!r}")
			zscore_row = _query(configuration, connector, driver, arguments.schema, "SELECT zscore, status, quality_flags FROM zscores_price_2 WHERE date_id = 4 AND ticker_id = 1")
			_assert(len(zscore_row) == 1 and float(str(zscore_row[0][0])) == 1.0 and int(str(zscore_row[0][1])) == 0 and int(str(zscore_row[0][2])) == 0, f"Numerical z-score value/state/quality changed on recovery: {zscore_row!r}")
			_assert(_query(configuration, connector, driver, arguments.schema, "SELECT delta_complete FROM transaction_dates WHERE id = 4") == [(1,)], "Delta completion was not retained")
			_assert(_query(configuration, connector, driver, arguments.schema, "SELECT row_count, ready_count FROM stockguidance_zscore_sessions WHERE date_id = 4") == [(1, 1)], "Z-score completion was not retained")

			# Second numerical operation: clear only derived evidence, kill after
			# the z-score commit, and verify a restart preserves both committed
			# calculation units instead of rebuilding or clearing them again.
			second_rebuild = _new_operation(source, factory, _numerical_scope(source, clear=True))
			process = _spawn(arguments, "clear")
			_assert(process.wait(timeout=30) == 0, "Derived-only clear failed")
			active = repository.load_active()
			if active is None:
				raise RuntimeError("Derived-only clear removed its pending operation")
			_assert(active.affected_rows_cleared, "Derived-only clear did not commit its flag")
			before_restart = active.scope.to_json()
			_kill_at_marker(arguments, "rebuild", directory, 2, "rebuild-after-zscore")
			active = repository.load_active()
			if active is None:
				raise RuntimeError("Rebuild-after-z-score kill removed its pending operation")
			_assert(active.scope.to_json() == before_restart, "Rebuild scope changed after z-score commit")
			_assert(_query(configuration, connector, driver, arguments.schema, "SELECT delta_complete FROM transaction_dates WHERE id = 4") == [(1,)], "Delta completion was lost after z-score boundary")
			_assert(_query(configuration, connector, driver, arguments.schema, "SELECT COUNT(*) FROM stockguidance_zscore_sessions WHERE date_id = 4") == [(1,)], "Z-score completion was not durable after process kill")
			process = _spawn(arguments, "rebuild", delete=True)
			_assert(process.wait(timeout=30) == 0, "Post-z-score restart failed")
			_assert(repository.load_active() is None, "Post-z-score restart did not clean pending state")
			delta_row = _query(configuration, connector, driver, arguments.schema, "SELECT close_ratio_to_prior, calculation_status, quality_flags FROM daily_delta_stock_data WHERE date_id = 4 AND ticker_id = 1")
			_assert(len(delta_row) == 1 and float(str(delta_row[0][0])) == 1.25 and str(delta_row[0][1]) == "not_yet_available" and int(str(delta_row[0][2])) == 0, f"Delta changed on idempotent restart: {delta_row!r}")
			zscore_row = _query(configuration, connector, driver, arguments.schema, "SELECT zscore, status, quality_flags FROM zscores_price_2 WHERE date_id = 4 AND ticker_id = 1")
			_assert(len(zscore_row) == 1 and float(str(zscore_row[0][0])) == 1.0 and int(str(zscore_row[0][1])) == 0 and int(str(zscore_row[0][2])) == 0, f"Z-score changed on idempotent restart: {zscore_row!r}")

			print(json.dumps({"stage": 11, "status": "complete", "database": "mysql_disposable", "process_kill_boundaries": ["clear_after_first_commit", "rebuild_after_delta_commit", "rebuild_after_zscore_commit"], "pending_cleanup": True, "unrelated_data_preserved": True, "production_access": {"ddl": False, "dml": False}}, sort_keys=True))
			return 0
		finally:
			_drop_schema(admin, arguments.schema)
			admin.close()


if __name__ == "__main__":
	raise SystemExit(main())
