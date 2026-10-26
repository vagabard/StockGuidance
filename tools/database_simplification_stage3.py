"""Run the Stage 3 schema/repository acceptance checks on disposable MySQL."""

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
from stockguidance.database.repositories.simplified import (  # noqa: E402
	ActiveOperationError,
	DataStateRepository,
	IncompatibleImportError,
	PendingUpdateRepository,
	SessionCompletionRepository,
	SourceConfigurationRepository,
)
from stockguidance.database.simplified_contract import (  # noqa: E402
	ClearScope,
	FailureDetail,
	OperationKind,
	PendingScope,
	WorkUnit,
)
from stockguidance.database.simplified_schema import SchemaVersionError, initialize_simplified_schema  # noqa: E402
from stockguidance.database.write_connections import WriteConnectionFactory  # noqa: E402
from stockguidance.market_data.identity import data_configuration_key  # noqa: E402
from stockguidance.performance.database_simplification_stage2 import (  # noqa: E402
	_connector,
	_connect,
	_create_schema,
	_drop_schema,
	_quote,
)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--dbinfo-file", type=Path, required=True, help="Explicit disposable MySQL configuration")
	parser.add_argument("--schema", default="stockguidance_disposable_stage3_20261026", help="Disposable schema name")
	parser.add_argument("--windows", type=int, nargs="+", default=[20, 60])
	arguments = parser.parse_args(argv)
	if not arguments.schema.startswith("stockguidance_disposable_"):
		raise SystemExit("--schema must use the stockguidance_disposable_ prefix")

	configuration = load_database_configuration(arguments.dbinfo_file)
	connector, driver = _connector(configuration)
	admin = _connect(configuration, connector, driver, autocommit=True)
	identity = SourceIdentity("fixture-provider", "grouped-daily", "us-stocks", "all-us-stocks", PriceAdjustment.ADJUSTED)
	source = SourceConfig(identity, ROOT / "data", arguments.dbinfo_file, "stage3-fixture")
	factory = cast(WriteConnectionFactory, cast(Callable[[Path], Any], lambda _path: _connect(configuration, connector, driver, schema=arguments.schema)))
	try:
		cursor = admin.cursor()
		try:
			cursor.execute("DROP DATABASE IF EXISTS " + _quote(arguments.schema))
			_create_schema(admin, arguments.schema)
		finally:
			cursor.close()
		initialize_simplified_schema(source, factory, windows=tuple(arguments.windows))
		if len(DataStateRepository(source, factory).load()) != 4:
			raise AssertionError("The four source states were not initialized")
		if SourceConfigurationRepository(source, factory).load().configuration_key != data_configuration_key(source.data_configuration):
			raise AssertionError("The source singleton was not initialized")

		connection = factory(source.database_config)
		cursor = connection.cursor()
		try:
			cursor.execute("INSERT INTO transaction_dates (transaction_date) VALUES (%s)", (date(2024, 7, 1),))
			cursor.execute("INSERT INTO ticker_symbol (label) VALUES (%s)", ("AAA",))
			connection.commit()
			try:
				cursor.execute("INSERT INTO raw_stock_data (ticker_id, date_id, state_id) VALUES (%s,%s,%s)", (1, 1, 999))
			except Exception:
				connection.rollback()
			else:
				raise AssertionError("Invalid raw state reference was accepted")
			try:
				cursor.execute("INSERT INTO stockguidance_source (id, configuration_key, source_key, source_family, endpoint, market, universe, interval_code, calendar_id, raw_price_split_adjusted, raw_price_dividend_adjusted, created_at) SELECT 2, configuration_key, source_key, source_family, endpoint, market, universe, interval_code, calendar_id, raw_price_split_adjusted, raw_price_dividend_adjusted, created_at FROM stockguidance_source WHERE id = 1", ())
			except Exception:
				connection.rollback()
			else:
				raise AssertionError("A duplicate singleton row was accepted")
			connection.rollback()
		finally:
			cursor.close()
			connection.close()

		completion = SessionCompletionRepository(source, factory)
		now = datetime(2026, 10, 26, 19, 30, tzinfo=timezone.utc)
		completion.set_raw_complete(1, "a" * 64, now, now)
		if not completion.load(1).raw_complete:
			raise AssertionError("Raw completion did not persist")
		completion.set_delta_complete(1, now)

		scope = PendingScope(1, OperationKind.INITIAL_IMPORT, data_configuration_key(source.data_configuration), source.calendar_id, now, now, (), ClearScope(), (WorkUnit("raw_session", 1, None, date(2024, 7, 1)),), ())
		pending = PendingUpdateRepository(source, factory)
		operation = pending.register(scope, now)
		try:
			pending.register(scope, now)
		except ActiveOperationError:
			pass
		else:
			raise AssertionError("A second active operation was accepted")
		pending.record_failure(operation.operation_id, FailureDetail("fixture", "disposable check", now), now)
		if pending.load_active() is None:
			raise AssertionError("Pending scope did not load")
		pending.mark_cleared(operation.operation_id, now)
		pending.delete_completed(operation.operation_id, now)
		if pending.load_active() is not None:
			raise AssertionError("Completed pending operation was not deleted")

		wrong_identity = SourceIdentity("fixture-provider", "grouped-daily", "wrong-market", "all-us-stocks", PriceAdjustment.ADJUSTED)
		wrong_source = SourceConfig(wrong_identity, ROOT / "data", arguments.dbinfo_file, "stage3-fixture")
		try:
			SourceConfigurationRepository(wrong_source, factory).assert_compatible()
		except IncompatibleImportError:
			pass
		else:
			raise AssertionError("An incompatible source configuration was accepted")

		connection = factory(source.database_config)
		cursor = connection.cursor()
		try:
			cursor.execute("UPDATE stockguidance_schema_version SET schema_version = 99 WHERE id = 1", ())
			connection.commit()
		finally:
			cursor.close()
			connection.close()
		try:
			DataStateRepository(source, factory).load()
		except SchemaVersionError:
			pass
		else:
			raise AssertionError("A mismatched schema version was accepted")
		print("Stage 3 disposable MySQL acceptance passed")
		return 0
	finally:
		_drop_schema(admin, arguments.schema)
		admin.close()


if __name__ == "__main__":
	raise SystemExit(main())
