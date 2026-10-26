"""Run Stage 7 numerical rebuild acceptance on one disposable MySQL schema."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any, Callable, cast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
	sys.path.insert(0, str(SRC))

from stockguidance.calendar.sessions import TradingSession  # noqa: E402
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity  # noqa: E402
from stockguidance.database.configuration import load_database_configuration  # noqa: E402
from stockguidance.database.numerical_rebuild import SimplifiedNumericalRebuildRunner  # noqa: E402
from stockguidance.database.operation_recovery import RebuildResult  # noqa: E402
from stockguidance.database.simplified_contract import ClearScope, OperationKind, PendingScope, PendingUpdate, WorkUnit  # noqa: E402
from stockguidance.database.simplified_schema import initialize_simplified_schema  # noqa: E402
from stockguidance.database.write_connections import WriteConnectionFactory  # noqa: E402
from stockguidance.market_data.identity import data_configuration_key  # noqa: E402
from stockguidance.performance.database_simplification_stage2 import (  # noqa: E402
	_connector,
	_connect,
	_create_schema,
	_drop_schema,
	_quote,
)
from stockguidance.zscores.specification import ZScoreFamily  # noqa: E402
from stockguidance.zscores.storage import ZScoreSeriesDefinition  # noqa: E402


NOW = datetime(2026, 10, 26, 19, 30, tzinfo=timezone.utc)


@dataclass
class FixtureCalendar:
	items: tuple[TradingSession, ...]

	calendar_id = "XNYS"
	version = "stage7-fixture"
	timezone = timezone.utc
	start = date(2024, 7, 1)
	end = date(2024, 7, 3)

	def session(self, session_date: date) -> TradingSession:
		return next(item for item in self.items if item.session_date == session_date)

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		return tuple(item for item in self.items if start <= item.session_date <= end)

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		index = self.items.index(session) + offset
		if index < 0 or index >= len(self.items):
			raise IndexError("fixture calendar boundary")
		return self.items[index]


def _session(day: date, sequence: int) -> TradingSession:
	return TradingSession("XNYS", day, sequence, datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc), datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc))


class _NoRawRunner:
	def rebuild(self, _operation: PendingUpdate, _connection: object) -> RebuildResult:
		return RebuildResult()


def _operation(source: SourceConfig, calendar: FixtureCalendar, date_id: int, target: date) -> PendingUpdate:
	definition = ZScoreSeriesDefinition(data_configuration_key(source.data_configuration), calendar.calendar_id, ZScoreFamily.PRICE, 2, "rolling-population-zscore-v1", "float32", "not_applicable")
	delta = WorkUnit("delta_session", date_id, 1, target)
	zscore = WorkUnit("zscore_session", date_id, None, target, definition.series_key, 2)
	clear = ClearScope(deltas=(delta,), zscores=(WorkUnit("zscore_session", date_id, 1, target, definition.series_key, 2),))
	scope = PendingScope(1, OperationKind.DERIVED_REPAIR, data_configuration_key(source.data_configuration), calendar.calendar_id, NOW, NOW, (), clear, (delta, zscore), ())
	return PendingUpdate(date_id, scope, NOW, NOW, True, None)


def _run(runner: SimplifiedNumericalRebuildRunner, operation: PendingUpdate, factory: WriteConnectionFactory, source: SourceConfig) -> RebuildResult:
	connection = factory(source.database_config)
	try:
		return runner.rebuild(operation, connection)
	finally:
		connection.close()


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--dbinfo-file", type=Path, required=True, help="Explicit disposable MySQL configuration")
	parser.add_argument("--schema", default="stockguidance_disposable_stage7_20261026", help="Disposable schema name")
	arguments = parser.parse_args(argv)
	if not arguments.schema.startswith("stockguidance_disposable_"):
		raise SystemExit("--schema must use the stockguidance_disposable_ prefix")

	configuration = load_database_configuration(arguments.dbinfo_file)
	connector, driver = _connector(configuration)
	admin = _connect(configuration, connector, driver, autocommit=True)
	identity = SourceIdentity("fixture-provider", "grouped-daily", "us-stocks", "all-us-stocks", PriceAdjustment.UNADJUSTED)
	source = SourceConfig(identity, ROOT / "data", arguments.dbinfo_file, "stage7-fixture")
	factory = cast(WriteConnectionFactory, cast(Callable[[Path], Any], lambda _path: _connect(configuration, connector, driver, schema=arguments.schema)))
	calendar = FixtureCalendar(tuple(_session(day, index) for index, day in enumerate((date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3),), start=1)))
	try:
		cursor = admin.cursor()
		try:
			cursor.execute("DROP DATABASE IF EXISTS " + _quote(arguments.schema))
			_create_schema(admin, arguments.schema)
		finally:
			cursor.close()
		initialize_simplified_schema(source, factory, windows=(2,))

		connection = factory(source.database_config)
		cursor = connection.cursor()
		try:
			for item in calendar.items:
				cursor.execute("INSERT INTO transaction_dates (transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision) VALUES (%s,1,%s,%s,%s)", (item.session_date, NOW, NOW, "a" * 64))
			cursor.execute("INSERT INTO ticker_symbol (label, exchange) VALUES (%s,%s)", ("AAA", "XNYS"))
			for date_id, close in enumerate((Decimal("10.00000000"), Decimal("12.00000000"), Decimal("15.00000000")), start=1):
				cursor.execute("INSERT INTO raw_stock_data (ticker_id,date_id,open,high,low,close,volume,vwap,transactions,state_id,observed_at) VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)", (date_id, close, close + 1, close - 1, close, Decimal("100.00000000"), close, 5, NOW))
			connection.commit()
		finally:
			cursor.close()
			connection.close()

		runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]
		first_operation = _operation(source, calendar, 2, date(2024, 7, 2))
		first = _run(runner, first_operation, factory, source)
		if first.failed_units or first.deferred_units or len(first.committed_units) != 2:
			raise AssertionError("Fresh numerical rebuild did not commit both units")

		connection = factory(source.database_config)
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT close_ratio_to_prior, calculation_status FROM daily_delta_stock_data WHERE date_id=2 AND ticker_id=1", ())
			first_delta = cursor.fetchall()
			cursor.execute("SELECT zscore, status FROM zscores_price_2 WHERE date_id=2 AND ticker_id=1", ())
			first_score = cursor.fetchall()
		finally:
			cursor.close()
			connection.close()
		second = _run(runner, first_operation, factory, source)
		if second.failed_units or second.deferred_units:
			raise AssertionError("Resume returned failed or deferred units")

		latest_operation = _operation(source, calendar, 3, date(2024, 7, 3))
		latest = _run(runner, latest_operation, factory, source)
		if latest.failed_units or latest.deferred_units:
			raise AssertionError("Latest numerical rebuild returned failed or deferred units")
		connection = factory(source.database_config)
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT close_ratio_to_prior, calculation_status, next_day_change_in_price FROM daily_delta_stock_data WHERE date_id=3 AND ticker_id=1", ())
			if cursor.fetchall() != [(Decimal("1.25"), "not_yet_available", None)]:
				raise AssertionError("Latest delta did not preserve current-only values")
			cursor.execute("SELECT close_ratio_to_prior, calculation_status FROM daily_delta_stock_data WHERE date_id=2 AND ticker_id=1", ())
			if cursor.fetchall() != first_delta:
				raise AssertionError("Resume changed the committed delta")
			cursor.execute("SELECT zscore, status FROM zscores_price_2 WHERE date_id=2 AND ticker_id=1", ())
			if cursor.fetchall() != first_score:
				raise AssertionError("Resume changed the committed z-score")
		finally:
			cursor.close()
			connection.close()
		print("Stage 7 disposable MySQL numerical acceptance passed")
		return 0
	finally:
		_drop_schema(admin, arguments.schema)
		admin.close()


if __name__ == "__main__":
	raise SystemExit(main())
