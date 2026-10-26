"""Run Stage 5 coordination and reader-visibility checks on disposable MySQL."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import sys
from typing import Any, Callable, cast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
	sys.path.insert(0, str(SRC))

from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity  # noqa: E402
from stockguidance.database.configuration import load_database_configuration  # noqa: E402
from stockguidance.database.dependency_planner import DependencyPlanner  # noqa: E402
from stockguidance.database.operation_recovery import (  # noqa: E402
	DatabasePendingClearer,
	DatabaseReadinessGuard,
	PendingOperationCoordinator,
	ReadScope,
	ReadinessState,
	RebuildRequiredError,
	consistent_read_transaction,
)
from stockguidance.database.repositories.simplified import PendingUpdateRepository  # noqa: E402
from stockguidance.database.simplified_contract import ClearScope, OperationKind, PendingScope, WorkUnit  # noqa: E402
from stockguidance.database.simplified_schema import initialize_simplified_schema  # noqa: E402
from stockguidance.database.table_names import load_table_names  # noqa: E402
from stockguidance.database.write_connections import WriteConnectionFactory  # noqa: E402
from stockguidance.market_data.identity import data_configuration_key  # noqa: E402
from stockguidance.performance.database_simplification_stage2 import (  # noqa: E402
	_connector,
	_connect,
	_create_schema,
	_drop_schema,
	_quote,
)
from stockguidance.workflows.market_lock import MaintenanceWriterLock  # noqa: E402


NOW = datetime(2026, 10, 26, 19, 30, tzinfo=timezone.utc)


def _scope(source: SourceConfig) -> PendingScope:
	day = date(2024, 7, 1)
	return PendingScope(
		1,
		OperationKind.INITIAL_IMPORT,
		data_configuration_key(source.data_configuration),
		source.calendar_id,
		NOW,
		NOW,
		(),
		ClearScope(
			raw=(WorkUnit("raw_session", 1, None, day),),
			completion=(WorkUnit("raw_session", 1, None, day),),
		),
		(),
		(),
	)


def _insert_fixture(factory: WriteConnectionFactory) -> None:
	connection = factory(Path("unused"))
	cursor = connection.cursor()
	try:
		cursor.execute("INSERT INTO transaction_dates (transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision) VALUES (%s, 1, %s, %s, %s)", (date(2024, 7, 1), NOW, NOW, "a" * 64))
		cursor.execute("INSERT INTO transaction_dates (transaction_date) VALUES (%s)", (date(2024, 7, 2),))
		cursor.execute("INSERT INTO ticker_symbol (label) VALUES (%s)", ("AAA",))
		cursor.execute("INSERT INTO raw_stock_data (ticker_id, date_id, close, state_id) VALUES (1, 1, %s, 1)", ("11.00000000",))
		connection.commit()
	finally:
		cursor.close()
		connection.close()


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--dbinfo-file", type=Path, required=True, help="Explicit MySQL credentials/configuration")
	parser.add_argument("--schema", default="stockguidance_disposable_stage5_20261026", help="Disposable schema name")
	arguments = parser.parse_args(argv)
	if not arguments.schema.startswith("stockguidance_disposable_"):
		raise SystemExit("--schema must use the stockguidance_disposable_ prefix")

	configuration = load_database_configuration(arguments.dbinfo_file)
	connector, driver = _connector(configuration)
	admin = _connect(configuration, connector, driver, autocommit=True)
	identity = SourceIdentity("fixture-provider", "grouped-daily", "us-stocks", "all-us-stocks", PriceAdjustment.ADJUSTED)
	source = SourceConfig(identity, ROOT / "data", arguments.dbinfo_file, "stage5-fixture")
	factory = cast(WriteConnectionFactory, cast(Callable[[Path], Any], lambda _path: _connect(configuration, connector, driver, schema=arguments.schema)))
	names = load_table_names()
	try:
		cursor = admin.cursor()
		try:
			cursor.execute("DROP DATABASE IF EXISTS " + _quote(arguments.schema))
			_create_schema(admin, arguments.schema)
		finally:
			cursor.close()
		initialize_simplified_schema(source, factory, windows=(20,))
		_insert_fixture(factory)

		repository = PendingUpdateRepository(source, factory)
		guard = DatabaseReadinessGuard(names)
		# Establish a snapshot before registration; that reader may finish against
		# its old view, while a newly started transaction is excluded.
		with consistent_read_transaction(source, factory) as transaction:
			if guard.check(ReadScope(date_ids=(1,)), transaction).status is not ReadinessState.READY:
				raise AssertionError("An empty pre-registration snapshot was not ready")
			operation = repository.register(_scope(source), NOW)
			if guard.check(ReadScope(date_ids=(1,)), transaction).status is not ReadinessState.READY:
				raise AssertionError("A pre-registration reader changed snapshots mid-transaction")
		with consistent_read_transaction(source, factory) as transaction:
			if guard.check(ReadScope(date_ids=(1,)), transaction).status is not ReadinessState.UNFINISHED_OPERATION:
				raise AssertionError("A new overlapping reader was not blocked")
			if guard.check(ReadScope(date_ids=(2,)), transaction).status is not ReadinessState.READY:
				raise AssertionError("An unrelated reader was blocked")

		# The coordinator acquires the real advisory lock, clears, and commits the
		# flag.  Rebuild is intentionally absent in Stage 5 and is never called.
		coordinator = PendingOperationCoordinator(source, factory, DependencyPlanner(), DatabasePendingClearer(names, batch_size=1), pending=repository)
		try:
			coordinator.accept(None, None)  # type: ignore[arg-type]
		except RebuildRequiredError:
			pass
		else:
			raise AssertionError("Stage 5 accepted a new operation without a rebuild stage")
		active = repository.load_active()
		if active is None or not active.affected_rows_cleared:
			raise AssertionError("Clearing did not commit affected_rows_cleared")
		if active.scope != operation.scope:
			raise AssertionError("Recovery changed the immutable scope")

		# Two independent MySQL sessions contend for the same existing writer lock.
		first = MaintenanceWriterLock(source, factory)
		first.__enter__()
		try:
			try:
				with MaintenanceWriterLock(source, factory):
					raise AssertionError("A second writer acquired the maintenance lock")
			except RuntimeError:
				pass
		finally:
			first.__exit__(None, None, None)

		repository.delete_completed(operation.operation_id, NOW)
		print("Stage 5 disposable MySQL coordination and visibility acceptance passed")
		return 0
	finally:
		_drop_schema(admin, arguments.schema)
		admin.close()


if __name__ == "__main__":
	raise SystemExit(main())
