"""Rehearse SQL batches and interruption recovery on two NEW disposable schemas.

Credentials are read from an existing dbinfo; its database is never selected.
The prefixed fixture schemas are retained for inspection. No production writes.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tracemalloc
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
	sys.path.insert(0, str(ROOT / "src"))

from stockguidance.config.source import DataConfiguration, SourceConfig  # noqa: E402
from stockguidance.database.configuration import load_database_configuration  # noqa: E402
from stockguidance.database.connections import MySQLConnectionFactory  # noqa: E402
from stockguidance.database.simplified_migration import MigrationError, MigrationPaths, SimplifiedDatabaseMigration  # noqa: E402
from stockguidance.database.table_names import load_table_names, quote_identifier, validate_identifier  # noqa: E402
from stockguidance.database.update_schema import initialize_update_schema, zscore_schema_statements  # noqa: E402
from stockguidance.database.write_connections import WriteConnectionFactory  # noqa: E402
from stockguidance.market_data.identity import data_configuration_key  # noqa: E402
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, legacy_rows  # noqa: E402
from stockguidance.market_data.quality import ObservedBar, QualityFlag, QualityRecord  # noqa: E402
from stockguidance.market_data.records import DailyBar, records_id  # noqa: E402
from stockguidance.zscores.specification import ZScoreFamily  # noqa: E402
from stockguidance.zscores.storage import ZScoreSeriesDefinition  # noqa: E402

NOW = datetime(2026, 10, 26, 19, 30, tzinfo=timezone.utc)


def _bar(ticker: str, close: str) -> DailyBar:
	v = Decimal(close)
	return DailyBar(ticker, v, v + 1, v - 1, v, Decimal(100), v, 5)


def _seed(source: SourceConfig, database: Any) -> None:
	initialize_update_schema(source, cast(WriteConnectionFactory, database))
	connection = database(source.database_config)
	setup_cursor = connection.cursor()
	try:
		for statement in zscore_schema_statements(load_table_names(), (2,)):
			setup_cursor.execute(statement, ())
		connection.commit()
	finally:
		setup_cursor.close()
		connection.close()
	connection = database(source.database_config)
	cursor = connection.cursor()
	dates = (date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3))
	bars = (
		(_bar("AAA", "10.00000000"), _bar("BBB", "20.00000000")),
		(_bar("AAA", "12.00000000"), _bar("BBB", "22.00000000")),
		(_bar("AAA", "15.00000000"), _bar("BBB", "25.00000000")),
	)
	try:
		for session in dates:
			cursor.execute("INSERT INTO transaction_dates (transaction_date) VALUES (%s)", (session,))
		cursor.execute("INSERT INTO ticker_symbol (label, exchange) VALUES (%s, %s)", ("AAA", "XNYS"))
		cursor.execute("INSERT INTO ticker_symbol (label, exchange) VALUES (%s, %s)", ("BBB", "XNYS"))
		quality = QualityRecord(
			ObservedBar("AAA", dates[1], 2, ("invalid", "13.00000000", "11.00000000", "12.00000000", "100.00000000", "12.00000000", 5), NOW),
			QualityFlag.BAD | QualityFlag.INTERPOLATED_SINGLE, ("open",),
			tuple(str(value) for value in (Decimal("12.00000000"), Decimal("13.00000000"), Decimal("11.00000000"), Decimal("12.00000000"), Decimal("100.00000000"), Decimal("12.00000000"), 5)), 1, dates[0], dates[2], NOW, NOW,
			(tuple(str(value) for value in (Decimal("10.00000000"), Decimal("11.00000000"), Decimal("9.00000000"), Decimal("10.00000000"), Decimal("100.00000000"), Decimal("10.00000000"), 5)), tuple(str(value) for value in (Decimal("15.00000000"), Decimal("16.00000000"), Decimal("14.00000000"), Decimal("15.00000000"), Decimal("100.00000000"), Decimal("15.00000000"), 5))),
		)
		for date_id, day in enumerate(dates, start=1):
			for ticker_id, bar in enumerate(bars[date_id - 1], start=1):
				cursor.execute("INSERT INTO raw_stock_data (ticker_id,date_id,open,high,low,close,volume,vwap,transactions) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (ticker_id, date_id, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions))
			if date_id == 2:
				cursor.execute("INSERT INTO stockguidance_row_quality (date_id,ticker_id,revision,flags,payload) VALUES (%s,%s,%s,%s,%s)", (2, 1, quality.revision, int(quality.flags), json.dumps(quality.payload(), sort_keys=True)))
			cursor.execute("INSERT INTO stockguidance_imports (session_date,raw_id,available_at,derived_dependency_id,derived_content_id) VALUES (%s,%s,%s,%s,%s)", (day, records_id(bars[date_id - 1]), NOW.isoformat(), "a" * 64, "b" * 64))
		for date_id, current in enumerate(bars, start=1):
			prior = bars[date_id - 2] if date_id > 1 else ()
			following = bars[date_id] if date_id < len(bars) else ()
			rows = legacy_rows(prior, current, following)
			for ticker_id, row in enumerate(rows, start=1):
				cursor.execute(f"INSERT INTO daily_delta_stock_data (ticker_id,date_id,{','.join(DERIVED_COLUMNS)}) VALUES ({','.join('%s' for _ in range(18))})", (ticker_id, date_id, *row.values))
			if date_id == 2:
				cursor.execute("INSERT INTO stockguidance_output_quality (series_key,date_id,ticker_id,flags,gap_length,provenance) VALUES (%s,%s,%s,%s,%s,%s)", ("legacy-derived", 2, 1, 2, 1, '{"repair":"interpolation"}'))
		# One retired table proves that metadata export is part of the rehearsal.
		cursor.execute("CREATE TABLE stockguidance_migration_completion (workload VARCHAR(32), session_date DATE, series_key VARCHAR(64), content_digest CHAR(64), committed_at VARCHAR(40), PRIMARY KEY(workload, session_date, series_key)) ENGINE=InnoDB", ())
		cursor.execute("INSERT INTO stockguidance_migration_completion VALUES (%s,%s,%s,%s,%s)", ("raw", dates[0], "raw", "c" * 64, NOW.isoformat()))
		for family in (ZScoreFamily.PRICE, ZScoreFamily.PRICE_DELTA):
			definition = ZScoreSeriesDefinition(data_configuration_key(source.data_configuration), source.calendar_id, family, 2, "rolling-population-zscore-v1", "float32", "not_applicable" if family is ZScoreFamily.PRICE else "close_ratio_to_prior_minus_one")
			cursor.execute("INSERT INTO stockguidance_zscore_series VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (definition.series_key, 1, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy, NOW.isoformat()))
			physical = "zscores_price_2" if family is ZScoreFamily.PRICE else "zscores_price_delta_2"
			for date_id in range(1, 4):
				for ticker_id in (1, 2):
					status, score = (1, None) if date_id == 1 else (0, float(date_id + ticker_id) / 10)
					cursor.execute(f"INSERT INTO {physical} (date_id,ticker_id,zscore,status) VALUES (%s,%s,%s,%s)", (date_id, ticker_id, score, status))
				cursor.execute("INSERT INTO stockguidance_zscore_sessions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (definition.series_key, date_id, "d" * 64, "e" * 64, int(date_id > 1), NOW.isoformat(), 2, 0 if date_id == 1 else 2, "1:2" if date_id == 1 else "0:2", "f" * 64))
		connection.commit()
	finally:
		cursor.close()
		connection.close()


def rehearse(dbinfo: Path, prefix: str, root: Path) -> dict[str, object]:
	if not prefix.startswith("stockguidance_disposable_"):
		raise ValueError("Fixture prefix must start with stockguidance_disposable_")
	validate_identifier(prefix, "disposable prefix")
	configuration = load_database_configuration(dbinfo)
	legacy_name, target_name = prefix + "_old", prefix + "_new"
	for name in (legacy_name, target_name):
		validate_identifier(name, "disposable database")
		if name.casefold() == configuration.database.casefold():
			raise ValueError("Fixture cannot select configured database")
	import mysql.connector
	admin = mysql.connector.connect(host=configuration.host, port=configuration.port,
		user=configuration.user, password=configuration.password, autocommit=True)
	try:
		q = admin.cursor()
		try:
			# No IF NOT EXISTS: never reuse or overwrite someone else's fixture.
			for name in (legacy_name, target_name):
				q.execute(f"CREATE DATABASE {quote_identifier(name)}")
		finally:
			q.close()
	finally:
		admin.close()
	root.mkdir(parents=True, exist_ok=True)
	data_configuration = root / "data-configuration.json"
	data_configuration.write_text(json.dumps({"schema_version": 1, "source_family": "fixture-provider", "endpoint": "fixture", "market": "us-stocks", "universe": "fixture", "calendar_id": "XNYS", "interval": "1d", "raw_price_split_adjusted": False, "raw_price_dividend_adjusted": False, "raw_price_vendor_defined_adjustment": None, "verification_policy": {"source_family_mismatch": "block", "endpoint_mismatch": "advisory", "source_family_sample_days": 1, "endpoint_sample_pulls": 1}}), encoding="utf-8")
	identity = DataConfiguration("fixture-provider", "fixture", "us-stocks", "fixture", False, False, None, calendar_id="XNYS").to_source_identity()
	sources = []
	for name in (legacy_name, target_name):
		path = root / (name + ".json")
		path.write_text(json.dumps({"host": configuration.host, "port": configuration.port, "database": name,
			"user": configuration.user, "password": configuration.password, "data_configuration_file": str(data_configuration.resolve())}), encoding="utf-8")
		sources.append(SourceConfig(identity, root, path))
	legacy, target = sources
	connect = MySQLConnectionFactory()
	_seed(legacy, connect)
	# Add >2 production-sized batches with gapped keys; no Python numerical fetch.
	c = connect(legacy.database_config)
	try:
		q = c.cursor()
		q.execute("INSERT INTO ticker_symbol (id,label,exchange) WITH RECURSIVE digits AS (SELECT 0 n UNION ALL SELECT n+1 FROM digits WHERE n<9) SELECT 100+2*(a.n+10*b.n+100*d.n+1000*e.n+10000*f.n),CONCAT('F',a.n+10*b.n+100*d.n+1000*e.n+10000*f.n),'XNYS' FROM digits a CROSS JOIN digits b CROSS JOIN digits d CROSS JOIN digits e CROSS JOIN digits f WHERE a.n+10*b.n+100*d.n+1000*e.n+10000*f.n<25003", ())
		q.execute("INSERT INTO raw_stock_data (ticker_id,date_id,open,high,low,close,volume,vwap,transactions) SELECT id,1,10,11,9,10,100,10,5 FROM ticker_symbol WHERE id>=100", ())
		# >100 tied outcomes independently generated by the existing Python kernel.
		for date_id in (2, 3):
			q.execute("INSERT INTO raw_stock_data (ticker_id,date_id,open,high,low,close,volume,vwap,transactions) SELECT id,%s,10,11,9,10,100,10,5 FROM ticker_symbol WHERE id BETWEEN 100 AND 308", (date_id,))
		q.execute("SELECT id,label FROM ticker_symbol WHERE id<100 OR id BETWEEN 100 AND 308", ())
		ids = {str(r[1]): int(str(r[0])) for r in q.fetchall()}
		extra = tuple(_bar('F' + str(i), '10') for i in range(105))
		bars = [tuple(sorted((_bar('AAA', value), _bar('BBB', other), *extra), key=lambda b: b.ticker)) for value, other in (('10','20'),('12','22'),('15','25'))]
		for date_id in (2, 3):
			q.execute("UPDATE stockguidance_imports SET raw_id=%s WHERE session_date=%s", (records_id(bars[date_id-1]), date(2024,7,date_id)))
			q.execute("DELETE FROM daily_delta_stock_data WHERE date_id=%s", (date_id,))
			for row in legacy_rows(bars[date_id-2], bars[date_id-1], bars[date_id] if date_id<3 else ()):
				q.execute("INSERT INTO daily_delta_stock_data (ticker_id,date_id," + ','.join(DERIVED_COLUMNS) + ") VALUES (" + ','.join('%s' for _ in range(18)) + ")", (ids[row.ticker], date_id, *row.values))
		c.commit()
		q.close()
	finally:
		c.close()
	backup, restored = root / "synthetic-backup.sql", root / "synthetic-restore.sql"
	backup.write_bytes(b"Synthetic rehearsal manifest; not a production backup or restore test.\n")
	restored.write_bytes(backup.read_bytes())
	manifest = root / "backup-manifest.json"
	manifest.write_text(json.dumps({"schema_version": 1, "backup_path": str(backup.resolve()), "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(), "restore_path": str(restored.resolve()), "restore_sha256": hashlib.sha256(restored.read_bytes()).hexdigest(), "restore_verified": True}), encoding="utf-8")
	paths = MigrationPaths.from_values(manifest, root / "export")
	results: list[str] = []
	blocker = connect(target.database_config)
	try:
		q = blocker.cursor()
		q.execute("SELECT GET_LOCK('stockguidance_daily_maintenance',0)", ())
		assert q.fetchall()[0][0] == 1, "Another writer is active; postpone rehearsal"
		try:
			SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(2,)).apply(paths, confirmation="APPLY " + target_name)
		except MigrationError as error:
			assert "Another market-data writer" in str(error), str(error)
		else:
			raise AssertionError("Concurrent migration was not rejected")
		q.execute("SELECT RELEASE_LOCK('stockguidance_daily_maintenance')", ())
		q.fetchall()
		q.close()
	finally:
		blocker.close()
	results.append("concurrent_invocation_rejected")
	def fail_schema(event: str, step: str, connection: Any) -> None:
		if event == "schema_ddl":
			connection.close()
			raise RuntimeError("injected schema interruption")
	try:
		SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(2,), batch_hook=fail_schema).apply(paths, confirmation="APPLY " + target_name)
	except RuntimeError as error:
		assert "injected schema interruption" in str(error), str(error)
	else:
		raise AssertionError("DDL interruption was not reached")
	results.append("schema_ddl")
	c = connect(legacy.database_config)
	try:
		q = c.cursor()
		q.execute("UPDATE raw_stock_data SET high=0 WHERE ticker_id=100 AND date_id=1", ())
		c.commit()
		try:
			SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(2,)).apply(paths, confirmation="APPLY " + target_name)
		except MigrationError as error:
			assert "invalid/unannotated" in str(error), str(error)
		else:
			raise AssertionError("Unannotated invalid raw observation was accepted")
		q.execute("UPDATE raw_stock_data SET high=11 WHERE ticker_id=100 AND date_id=1", ())
		c.commit()
		q.close()
	finally:
		c.close()
	results.append("invalid_raw_batch_rejected")
	worker_config = root / "worker.json"
	worker_config.write_text(json.dumps({"legacy": str(legacy.database_config), "target": str(target.database_config), "manifest": str(manifest), "export": str(paths.export_dir)}), encoding="utf-8")
	child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker-config", str(worker_config)], capture_output=True, text=True, timeout=60)
	if child.returncode != 73:
		raise AssertionError("Abrupt process-exit test did not reach batch boundary: " + child.stderr)
	results.append("abrupt_process_exit_after_insert")
	def lose_lock(event: str, step: str, connection: Any) -> None:
		if event == "after_insert" and step == "copy:raw_stock_data":
			q = connection.cursor()
			q.execute("SELECT RELEASE_LOCK('stockguidance_daily_maintenance')", ())
			q.fetchall()
			q.close()
	try:
		SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(2,), batch_hook=lose_lock).apply(paths, confirmation="APPLY " + target_name)
	except MigrationError as error:
		assert "lock was lost" in str(error), str(error)
	else:
		raise AssertionError("Lock loss was not detected")
	results.append("lock_loss_after_insert")
	for event in ("after_insert", "before_commit", "after_commit"):
		triggered = False
		def fault(actual: str, step: str, connection: Any) -> None:
			nonlocal triggered
			if actual == event and step == "copy:raw_stock_data" and not triggered:
				triggered = True
				# Closing the actual MySQL connection exercises real rollback/unknown commit.
				connection.close()
				raise RuntimeError("injected connection loss at " + event)
		migration = SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(2,), batch_hook=fault)
		try:
			migration.apply(paths, confirmation="APPLY " + target_name)
		except RuntimeError as error:
			if "injected connection loss" not in str(error):
				raise
		else:
			raise AssertionError("Interruption hook was not reached")
		c = connect(target.database_config)
		try:
			q = c.cursor()
			q.execute("SELECT COUNT(*) FROM raw_stock_data", ())
			actual = q.fetchall()[0][0]
			expected = 10000 if event == "after_commit" else 0
			assert actual == expected, (event, actual, expected)
			q.close()
		finally:
			c.close()
		results.append(event)
	migration = SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(2,))
	tracemalloc.start()
	start = time.perf_counter()
	applied = migration.apply(paths, confirmation="APPLY " + target_name)
	seconds = time.perf_counter() - start
	_, peak = tracemalloc.get_traced_memory()
	tracemalloc.stop()
	assert applied["phase"] == "validated"
	# A second application must skip copied ranges rather than duplicate them.
	migration.apply(paths, confirmation="APPLY " + target_name)
	def fail_validation(event: str, step: str, connection: Any) -> None:
		if event == "after_commit" and step == "validate:raw_stock_data":
			connection.close()
			raise RuntimeError("injected validation interruption")
	try:
		SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(2,), batch_hook=fail_validation).validate(paths)
	except RuntimeError as error:
		assert "injected validation interruption" in str(error), str(error)
	else:
		raise AssertionError("Validation interruption was not reached")
	assert migration.validate(paths)["phase"] == "validated"
	results.append("validation_after_commit")
	assert migration.status(paths)["phase"] == "validated"
	# Capture actual MySQL range selection and session-completion index availability.
	c = connect(target.database_config)
	try:
		q = c.cursor()
		q.execute("EXPLAIN FORMAT=JSON SELECT ticker_id,date_id FROM " + quote_identifier(legacy_name) + ".raw_stock_data s WHERE s.ticker_id>100 OR (s.ticker_id=100 AND s.date_id>1) ORDER BY ticker_id,date_id LIMIT 10000", ())
		query_plan = json.loads(str(q.fetchall()[0][0]))
		q.execute("SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='daily_delta_stock_data' AND COLUMN_NAME='date_id' AND SEQ_IN_INDEX=1", ())
		assert q.fetchall(), "Completion requires a date-leading delta index"
		q.close()
	finally:
		c.close()
	c = connect(target.database_config)
	try:
		q = c.cursor()
		q.execute("SELECT COUNT(*) FROM raw_stock_data", ())
		assert q.fetchall()[0][0] == 25219
		q.execute("SELECT state_id FROM raw_stock_data WHERE date_id=2 AND ticker_id=1", ())
		assert q.fetchall()[0][0] == 4
		q.execute("SELECT raw_complete,delta_complete FROM transaction_dates WHERE id=2", ())
		assert q.fetchall()[0] == (1, 1)
		# Test actual allocation, not just information_schema estimates.
		q.execute("INSERT INTO ticker_symbol (label,exchange) VALUES ('AFTER_COPY','XNYS')", ())
		q.execute("SELECT id FROM ticker_symbol WHERE label='AFTER_COPY'", ())
		assert int(str(q.fetchall()[0][0])) > 50104
		c.rollback()
		q.execute("INSERT INTO transaction_dates (transaction_date) VALUES ('2024-07-05')", ())
		q.execute("SELECT id FROM transaction_dates WHERE transaction_date='2024-07-05'", ())
		assert int(str(q.fetchall()[0][0])) > 3
		c.rollback()
		q.execute("UPDATE raw_stock_data SET close=close+1 WHERE ticker_id=1 AND date_id=1", ())
		c.commit()
		q.close()
	finally:
		c.close()
	try:
		migration.validate(paths)
	except MigrationError as error:
		assert "mismatches" in str(error), str(error)
	else:
		raise AssertionError("Value corruption was not detected")
	c = connect(target.database_config)
	try:
		q = c.cursor()
		q.execute("UPDATE raw_stock_data SET close=10 WHERE ticker_id=1 AND date_id=1", ())
		c.commit()
		q.close()
	finally:
		c.close()
	assert migration.validate(paths)["phase"] == "validated"
	assert migration.cutover(paths, confirmation="CUTOVER " + target_name)["phase"] == "cutover"
	assert (paths.export_dir / "cutover-marker.json").exists()
	try:
		migration.apply(paths, confirmation="APPLY " + target_name)
	except MigrationError as error:
		assert "after cutover" in str(error), str(error)
	else:
		raise AssertionError("Apply after cutover must be refused")
	return {"status": "passed", "database_write_scope": [legacy_name, target_name], "interruption_boundaries": results,
		"raw_rows": 25219, "batch_size": 10000, "corruption_detected": True, "fixtures_retained": True, "ranking_ties": 105, "cutover": "passed",
		"resumed_apply_and_validation_seconds": seconds, "python_tracemalloc_peak_bytes": peak, "raw_keyset_explain": query_plan,
		"backup_restore_test": "synthetic manifest only; real restore usability remains an operator check"}


def main() -> int:
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument("--dbinfo-file", type=Path)
	p.add_argument("--prefix", help="NEW stockguidance_disposable_* prefix; databases are retained")
	p.add_argument("--output-dir", type=Path, help="Private fixture credentials and report directory")
	p.add_argument("--worker-config", type=Path, help=argparse.SUPPRESS)
	a = p.parse_args()
	if a.worker_config:
		from stockguidance.database.simplified_migration import build_source_configs
		settings = json.loads(a.worker_config.read_text(encoding="utf-8"))
		for key in ("legacy", "target"):
			if not load_database_configuration(Path(settings[key])).database.startswith("stockguidance_disposable_"):
				raise ValueError("Worker requires disposable schemas")
		legacy, target = build_source_configs(Path(settings["legacy"]), Path(settings["target"]), a.worker_config.parent)
		def die(event: str, step: str, connection: Any) -> None:
			if event == "after_insert" and step == "copy:raw_stock_data":
				os._exit(73)
		connect = MySQLConnectionFactory()
		migration = SimplifiedDatabaseMigration(legacy, target, connect, connect, windows=(2,), batch_hook=die)
		migration.apply(MigrationPaths.from_values(Path(settings["manifest"]), Path(settings["export"])), confirmation="APPLY " + load_database_configuration(target.database_config).database)
		raise AssertionError("Worker did not exit at batch boundary")
	if not a.dbinfo_file or not a.prefix or not a.output_dir:
		p.error("--dbinfo-file, --prefix and --output-dir are required")
	report = rehearse(a.dbinfo_file, a.prefix, a.output_dir.resolve())
	(a.output_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
	print(json.dumps(report, indent=2))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
