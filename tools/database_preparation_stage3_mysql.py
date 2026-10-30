"""Rehearse Stage 3 volume preparation against disposable MySQL databases.

Credentials are read from an existing dbinfo file and held only in memory.
The runner creates and drops only databases whose names use the required
disposable prefix; it never selects or modifies the configured database.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
	sys.path.insert(0, str(ROOT / "src"))

from stockguidance.calendar.exchange import ExchangeCalendar  # noqa: E402
from stockguidance.config.source import DataConfiguration, SourceConfig  # noqa: E402
from stockguidance.database.configuration import DatabaseConfiguration, load_database_configuration  # noqa: E402
from stockguidance.database.preparation import (  # noqa: E402
	PreparationError,
	apply_volume_result,
	calculate_volume_zscores,
	plan_volume_zscore,
	render_volume_sql,
	select_volume_inputs,
	validate_volume_result,
)
from stockguidance.database.simplified_schema import initialize_simplified_schema  # noqa: E402
from stockguidance.database.table_names import TableNames, load_table_names, quote_identifier, validate_identifier  # noqa: E402
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory  # noqa: E402
from stockguidance.market_data.identity import data_configuration_key, source_key  # noqa: E402


class _MySQLTarget:
	def __init__(self, configuration: DatabaseConfiguration):
		self.configuration = configuration

	def __call__(self, _database_config: Path) -> WriteConnection:
		import mysql.connector

		return cast(WriteConnection, mysql.connector.connect(
			host=self.configuration.host,
			database=self.configuration.database,
			user=self.configuration.user,
			password=self.configuration.password,
			port=self.configuration.port,
			autocommit=False,
			buffered=True,
		))


class _Admin:
	def __init__(self, configuration: DatabaseConfiguration):
		self.configuration = configuration

	def connect(self) -> Any:
		import mysql.connector

		return mysql.connector.connect(
			host=self.configuration.host,
			user=self.configuration.user,
			password=self.configuration.password,
			port=self.configuration.port,
			autocommit=True,
		)

	def create(self, database: str) -> None:
		connection = self.connect()
		cursor = connection.cursor()
		try:
			cursor.execute(f"CREATE DATABASE {quote_identifier(database)} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin", ())
		finally:
			cursor.close()
			connection.close()

	def drop(self, database: str) -> None:
		connection = self.connect()
		cursor = connection.cursor()
		try:
			cursor.execute(f"DROP DATABASE {quote_identifier(database)}", ())
		finally:
			cursor.close()
			connection.close()


class _TargetLock:
	def __init__(self, source: SourceConfig, connect: _MySQLTarget):
		self.source = source
		self.connect = connect

	def __enter__(self) -> "_TargetLock":
		return self

	def __exit__(self, *_: object) -> None:
		return None

	def assert_held(self) -> None:
		return None


def _custom_names() -> TableNames:
	return replace(
		load_table_names(),
		source="p3_source",
		sessions="p3_sessions",
		tickers="p3_tickers",
		prices="p3_prices",
		deltas="p3_deltas",
		imports="p3_imports",
		zscore_series="p3_series",
		zscore_sessions="p3_zscore_sessions",
		price_template="p3_price_{window}",
		price_delta_template="p3_price_delta_{window}",
		volume_template="p3_volume_{window}",
		data_verifications="p3_verifications",
		data_verification_samples="p3_verification_samples",
		schema="p3_schema",
		data_states="p3_states",
		pending_updates="p3_pending",
		repair_metadata="p3_repairs",
	)


def _strip_comments(script: str) -> str:
	return "\n".join(line for line in script.splitlines() if not line.startswith("--")).strip().rstrip(";")


def _seed(source: SourceConfig, connect: _MySQLTarget, names: TableNames) -> None:
	initialize_simplified_schema(source, cast(WriteConnectionFactory, connect), windows=(2,), table_names=names)
	connection = connect(source.database_config)
	cursor = connection.cursor()
	try:
		sessions = (date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3))
		sessions_table = quote_identifier(names.sessions)
		tickers_table = quote_identifier(names.tickers)
		prices_table = quote_identifier(names.prices)
		for index, day in enumerate(sessions, start=1):
			cursor.execute(
				f"INSERT INTO {sessions_table} (id, transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision) VALUES (%s,%s,1,%s,%s,%s)",
				(index, day, "2024-07-10 00:00:00", "2024-07-10 00:00:00", f"{index:064x}"),
			)
		cursor.execute(f"INSERT INTO {tickers_table} (id, label, exchange) VALUES (1, 'AAA', 'XNYS')", ())
		for index, volume in enumerate((Decimal("10"), Decimal("20"), Decimal("30")), start=1):
			cursor.execute(f"INSERT INTO {prices_table} (ticker_id, date_id, volume, state_id) VALUES (1,%s,%s,1)", (index, volume))
		connection.commit()
	finally:
		cursor.close()
		connection.close()


def rehearse(dbinfo_file: Path, prefix: str, output_dir: Path) -> dict[str, object]:
	if not prefix.startswith("stockguidance_disposable_preparation_"):
		raise ValueError("Prefix must start with stockguidance_disposable_preparation_")
	validate_identifier(prefix, "disposable prefix")
	base = load_database_configuration(dbinfo_file)
	if prefix.casefold() == base.database.casefold():
		raise ValueError("The disposable prefix cannot be the configured database")
	database = prefix
	validate_identifier(database, "disposable database")
	admin = _Admin(base)
	created = False
	output_dir.mkdir(parents=True, exist_ok=True)
	try:
		admin.create(database)
		created = True
		target_configuration = replace(base, database=database)
		connect = _MySQLTarget(target_configuration)
		data_configuration = DataConfiguration("fixture-provider", "fixture", "us-stocks", "fixture", False, False, None, calendar_id="XNYS")
		source = SourceConfig(data_configuration.to_source_identity(), output_dir, Path("disposable-memory-dbinfo"), _configuration=data_configuration)
		names = _custom_names()
		_seed(source, connect, names)
		calendar = ExchangeCalendar("XNYS", date(2024, 6, 1), date(2024, 7, 5))
		from stockguidance.database.preparation import VolumePreparationRequest

		request = VolumePreparationRequest(source_key(source.identity), data_configuration_key(source.data_configuration), "XNYS", date(2024, 7, 2), date(2024, 7, 3), (1,), (2,), 2)
		plan = plan_volume_zscore(request, calendar, names)
		select_sql = render_volume_sql(plan, "select", names)
		calculate_sql = render_volume_sql(plan, "calculate", names)
		apply_sql = render_volume_sql(plan, "apply", names)
		if not select_sql.read_only or not calculate_sql.read_only or apply_sql.read_only:
			raise AssertionError("SQL route read-only flags are incorrect")
		if "%s" in select_sql.script or "%s" in calculate_sql.script:
			raise AssertionError("Rendered inspection SQL contains application placeholders")

		factory = cast(WriteConnectionFactory, connect)
		rows = select_volume_inputs(source, factory, plan, names)
		result = calculate_volume_zscores(plan, rows, datetime(2024, 7, 10, tzinfo=timezone.utc))
		connection = connect(source.database_config)
		cursor = connection.cursor()
		try:
			select_query = _strip_comments(select_sql.script)
			cursor.execute("EXPLAIN FORMAT=JSON " + select_query, ())
			explain_text = str(cursor.fetchall()[0][0])
			start = time.perf_counter()
			cursor.execute(_strip_comments(calculate_sql.script), ())
			sql_rows = cursor.fetchall()
			timing_ms = round((time.perf_counter() - start) * 1000, 3)
		finally:
			cursor.close()
			connection.close()
		code_rows = {(row.window, row.date_id, row.ticker_id): (row.row.score, int(row.row.status)) for row in result.rows}
		if len(sql_rows) != len(code_rows):
			raise AssertionError("SQL and code result row counts differ")
		for date_id, _day, ticker_id, window, score, status in sql_rows:
			expected_score, expected_status = code_rows[(int(str(window)), int(str(date_id)), int(str(ticker_id)))]
			if int(str(status)) != expected_status or (expected_score is None and score is not None) or (expected_score is not None and abs(float(str(score)) - expected_score) > 1e-6):
				raise AssertionError("SQL and code result values differ")

		changed_connection = connect(source.database_config)
		changed_cursor = changed_connection.cursor()
		try:
			changed_cursor.execute(f"UPDATE {quote_identifier(names.prices)} SET volume = 999 WHERE ticker_id = 1 AND date_id = 3", ())
			changed_connection.commit()
		finally:
			changed_cursor.close()
			changed_connection.close()
		try:
			apply_volume_result(source, factory, plan, result, table_names=names, lock_factory=lambda: _TargetLock(source, connect))
		except PreparationError as error:
			if "changed between" not in str(error):
				raise
		else:
			raise AssertionError("Changed-source apply was not rejected")
		restore_connection = connect(source.database_config)
		restore_cursor = restore_connection.cursor()
		try:
			restore_cursor.execute(f"UPDATE {quote_identifier(names.prices)} SET volume = 30 WHERE ticker_id = 1 AND date_id = 3", ())
			restore_connection.commit()
		finally:
			restore_cursor.close()
			restore_connection.close()

		try:
			apply_volume_result(source, factory, plan, result, table_names=names, lock_factory=lambda: _TargetLock(source, connect), interrupt_after_clear=True)
		except PreparationError as error:
			if "interrupted" not in str(error):
				raise
		else:
			raise AssertionError("Interruption hook was not reached")
		pending_connection = connect(source.database_config)
		pending_cursor = pending_connection.cursor()
		try:
			pending_cursor.execute(f"SELECT affected_rows_cleared FROM {quote_identifier(names.pending_updates)}", ())
			pending_state = pending_cursor.fetchall()
		finally:
			pending_cursor.close()
			pending_connection.close()
		if pending_state != [(1,)]:
			raise AssertionError("Interrupted preparation did not leave durable clear state")
		apply_volume_result(source, factory, plan, result, table_names=names, lock_factory=lambda: _TargetLock(source, connect))
		apply_volume_result(source, factory, plan, result, table_names=names, lock_factory=lambda: _TargetLock(source, connect))
		validation = validate_volume_result(source, factory, plan, result, table_names=names)
		return {
			"status": "passed",
			"database_scope": [database],
			"custom_registry": True,
			"dependency_rows": len(rows),
			"result_rows": len(result.rows),
			"sql_code_parity": True,
			"source_change_rejected": True,
			"crash_resume": True,
			"rerun": True,
			"validated_rows": validation.rows_checked,
			"explain_available": bool(explain_text),
			"explain_prefix": explain_text[:240],
			"calculate_timing_ms": timing_ms,
		}
	finally:
		if created:
			admin.drop(database)


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--dbinfo-file", type=Path, required=True)
	parser.add_argument("--prefix", required=True)
	parser.add_argument("--output-dir", type=Path, required=True)
	arguments = parser.parse_args()
	report = rehearse(arguments.dbinfo_file, arguments.prefix, arguments.output_dir.resolve())
	(arguments.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
	print(json.dumps(report, indent=2))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
