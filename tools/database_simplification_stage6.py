"""Run Stage 6 raw/interpolation acceptance on one disposable MySQL schema."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable, cast
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
	sys.path.insert(0, str(SRC))

from stockguidance.calendar.sessions import TradingSession  # noqa: E402
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity  # noqa: E402
from stockguidance.database.configuration import load_database_configuration  # noqa: E402
from stockguidance.database.raw_rebuild import RawRebuildRunner  # noqa: E402
from stockguidance.database.simplified_contract import ClearScope, OperationKind, PendingScope, PendingUpdate, PreparedPayload, WorkUnit  # noqa: E402
from stockguidance.database.simplified_schema import initialize_simplified_schema  # noqa: E402
from stockguidance.database.write_connections import WriteConnectionFactory  # noqa: E402
from stockguidance.market_data.archive import archive_content  # noqa: E402
from stockguidance.market_data.identity import data_configuration_key  # noqa: E402
from stockguidance.market_data.providers.contracts import DownloadedSession, ProviderResponse, ResponseState  # noqa: E402
from stockguidance.market_data.records import records_id  # noqa: E402
from stockguidance.performance.database_simplification_stage2 import (  # noqa: E402
	_connector,
	_connect,
	_create_schema,
	_drop_schema,
	_quote,
)


NOW = datetime(2026, 10, 26, 19, 30, tzinfo=timezone.utc)


@dataclass
class FixtureCalendar:
	by_date: dict[date, TradingSession]

	calendar_id = "XNYS"
	version = "stage6-fixture"
	timezone = ZoneInfo("UTC")
	start = date(2024, 7, 5)
	end = date(2024, 7, 9)

	def session(self, session_date: date) -> TradingSession:
		return self.by_date[session_date]

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		return tuple(item for item in self.by_date.values() if start <= item.session_date <= end)

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		return self.by_date[date.fromordinal(session.session_date.toordinal() + offset)]


def _session(day: date, sequence: int) -> TradingSession:
	return TradingSession("XNYS", day, sequence, datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc), datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc))


def _operation(source: SourceConfig, payload: PreparedPayload) -> PendingUpdate:
	day = payload.session_date
	return PendingUpdate(
		1,
		PendingScope(
			1, OperationKind.INITIAL_IMPORT, data_configuration_key(source.data_configuration), source.calendar_id,
			NOW, NOW, (payload,), ClearScope(),
			(WorkUnit("raw_session", 2, None, day), WorkUnit("repair_row", 2, 1, day)), (),
		),
		NOW, NOW, True, None,
	)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--dbinfo-file", type=Path, required=True, help="Explicit disposable MySQL configuration")
	parser.add_argument("--schema", default="stockguidance_disposable_stage6_20261026", help="Disposable schema name")
	arguments = parser.parse_args(argv)
	if not arguments.schema.startswith("stockguidance_disposable_"):
		raise SystemExit("--schema must use the stockguidance_disposable_ prefix")

	configuration = load_database_configuration(arguments.dbinfo_file)
	connector, driver = _connector(configuration)
	admin = _connect(configuration, connector, driver, autocommit=True)
	identity = SourceIdentity("fixture-provider", "grouped-daily", "us-stocks", "all-us-stocks", PriceAdjustment.UNADJUSTED)
	source = SourceConfig(identity, ROOT / "data", arguments.dbinfo_file, "stage6-fixture")
	factory = cast(WriteConnectionFactory, cast(Callable[[Path], Any], lambda _path: _connect(configuration, connector, driver, schema=arguments.schema)))
	calendar = FixtureCalendar({day: _session(day, index) for index, day in enumerate((date(2024, 7, 5), date(2024, 7, 8), date(2024, 7, 9)))})
	try:
		cursor = admin.cursor()
		try:
			cursor.execute("DROP DATABASE IF EXISTS " + _quote(arguments.schema))
			_create_schema(admin, arguments.schema)
		finally:
			cursor.close()
		initialize_simplified_schema(source, factory, windows=(20,))

		connection = factory(source.database_config)
		cursor = connection.cursor()
		try:
			for day in calendar.by_date:
				cursor.execute("INSERT INTO transaction_dates (transaction_date) VALUES (%s)", (day,))
			cursor.execute("INSERT INTO ticker_symbol (label, exchange) VALUES (%s,%s)", ("AAA", "XNYS"))
			cursor.execute("INSERT INTO raw_stock_data (ticker_id, date_id, open, high, low, close, volume, vwap, transactions, state_id, observed_at) VALUES (1,1,10,12,9,10,100,10,5,1,%s)", (NOW,))
			cursor.execute("INSERT INTO raw_stock_data (ticker_id, date_id, open, high, low, close, volume, vwap, transactions, state_id, observed_at) VALUES (1,3,20,22,19,20,100,20,5,1,%s)", (NOW,))
			connection.commit()
		finally:
			cursor.close()
			connection.close()

		middle = calendar.session(date(2024, 7, 8))
		bad_values = ("10.00000000", "20.00000000", "9.00000000", "0", "100.00000000", "10.00000000000000000000", 5)
		response = ProviderResponse(
			ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 1),),
			source_timestamps=(("AAA", int(middle.closes_at.timestamp() * 1000)),), bad_records=(("AAA", bad_values),),
		)
		with tempfile.TemporaryDirectory(prefix="stockguidance-stage6-") as temporary:
			path = Path(temporary) / "middle.ndjson"
			content = archive_content(DownloadedSession(middle, response, NOW))
			path.write_bytes(content)
			payload = PreparedPayload("middle", middle.session_date, path, hashlib.sha256(content).hexdigest(), records_id(response.records), NOW, NOW)
			runner = RawRebuildRunner(source, calendar, clock=lambda: NOW)
			first = runner.rebuild(_operation(source, payload), factory(source.database_config))
			if first.failed_units or first.deferred_units or len(first.committed_units) != 2 or first.explicit_non_ready:
				raise AssertionError("Stage 6 raw/interpolation rebuild did not complete cleanly")
			second = runner.rebuild(_operation(source, payload), factory(source.database_config))
			if second.failed_units or second.deferred_units or len(second.committed_units) != 2:
				raise AssertionError("Stage 6 resume did not skip completed raw/repair units")

		connection = factory(source.database_config)
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT state_id, close, observed_at FROM raw_stock_data WHERE date_id = 2 AND ticker_id = 1", ())
			row = cursor.fetchall()
			if len(row) != 1 or row[0][0] != 4 or Decimal(str(row[0][1])) != Decimal("15.00000000") or row[0][2] is None:
				raise AssertionError("Interpolated raw row/state/timestamp was not preserved")
			cursor.execute("SELECT state_id, interpolation_method, prior_date_id, following_date_id FROM stockguidance_repair_metadata WHERE date_id = 2 AND ticker_id = 1", ())
			if cursor.fetchall() != [(4, "linear-trading-session-v1", 1, 3)]:
				raise AssertionError("Interpolation metadata was not committed atomically")
		finally:
			cursor.close()
			connection.close()
		print("Stage 6 disposable MySQL raw/interpolation acceptance passed")
		return 0
	finally:
		_drop_schema(admin, arguments.schema)
		admin.close()


if __name__ == "__main__":
	raise SystemExit(main())
