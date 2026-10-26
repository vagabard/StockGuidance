"""Stage 2 disposable-MySQL benchmark for raw state placement.

The benchmark deliberately owns its schema and data.  It never selects the
configured database, and the schema guard rejects anything outside the
``stockguidance_disposable_*`` namespace.  The generated population is
synthetic; it is deterministic so that two layouts receive the same rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import sys
from time import perf_counter
from typing import Any, Callable, Iterable, Protocol, Sequence, cast

from stockguidance.database.configuration import DatabaseConfiguration, MySQLDriver, load_database_configuration
from stockguidance.database.table_names import quote_identifier, validate_identifier


BENCHMARK_NAME = "stockguidance-database-simplification-stage2-v1"
SCHEMA_PATTERN = re.compile(r"stockguidance_disposable_[a-z0-9_]{1,39}\Z")
DEFAULT_SCHEMA = "stockguidance_disposable_stage2_benchmark"
DEFAULT_SEED = 20261026
DEFAULT_SESSION_COUNT = 504
DEFAULT_TICKER_COUNT = 1500
DEFAULT_REPETITIONS = 20
DEFAULT_WRITE_ROWS = 1000
DEFAULT_BATCH_SIZE = 5000
STATE_CODES = {1: "good", 2: "bad", 3: "missing", 4: "interpolated"}
LAYOUTS = ("raw_column", "separate_state")


class Connector(Protocol):
	def connect(self, **kwargs: object) -> Any: ...


@dataclass(frozen=True)
class QueryCase:
	name: str
	start_date_id: int
	end_date_id: int
	ticker_id: int | None
	quality_flags: int


@dataclass(frozen=True)
class Population:
	seed: int
	session_count: int
	ticker_count: int
	rows: tuple[tuple[int, int, str | None, int, int, float | None, int], ...]
	state_counts: dict[str, int]
	quality_counts: dict[str, int]
	start_date: date
	end_date: date


@dataclass(frozen=True)
class CandidateTables:
	raw: str
	states: str | None
	zscores: str


def _validate_schema(schema: str, configured_database: str) -> str:
	if SCHEMA_PATTERN.fullmatch(schema) is None or schema.casefold() == configured_database.casefold():
		raise ValueError("Require a distinct stockguidance_disposable_* MySQL schema")
	return validate_identifier(schema, "disposable schema")


def _quote(value: str) -> str:
	return quote_identifier(validate_identifier(value))


def _percentile(values: Sequence[float], fraction: float) -> float:
	if not values:
		raise ValueError("Cannot calculate a percentile for no values")
	ordered = sorted(values)
	index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
	return ordered[index]


def _summary(values: Sequence[float]) -> dict[str, float]:
	if not values:
		raise ValueError("Cannot summarize no measurements")
	return {
		"count": float(len(values)),
		"median_seconds": statistics.median(values),
		"p95_seconds": _percentile(values, 0.95),
		"min_seconds": min(values),
		"max_seconds": max(values),
	}


def _geometric_mean(values: Iterable[float]) -> float:
	items = tuple(values)
	if not items or any(value <= 0 for value in items):
		raise ValueError("Geometric means require positive values")
	return math.exp(sum(math.log(value) for value in items) / len(items))


def _normalize(value: object) -> object:
	if isinstance(value, Decimal):
		return format(value.normalize(), "f")
	if isinstance(value, (date,)):
		return value.isoformat()
	if isinstance(value, float):
		return format(value, ".17g")
	return value


def _rows_digest(rows: Iterable[Sequence[object]]) -> tuple[int, str]:
	digest = hashlib.sha256()
	count = 0
	for row in rows:
		payload = json.dumps([_normalize(value) for value in row], separators=(",", ":"), sort_keys=False).encode("utf-8")
		digest.update(payload)
		digest.update(b"\n")
		count += 1
	return count, digest.hexdigest()


def _connector(configuration: DatabaseConfiguration) -> tuple[Connector, str]:
	drivers = (configuration.driver,) if configuration.driver is not MySQLDriver.AUTO else (MySQLDriver.CONNECTOR, MySQLDriver.PYMYSQL)
	last_error: Exception | None = None
	for driver in drivers:
		module_name = "mysql.connector" if driver is MySQLDriver.CONNECTOR else "pymysql"
		try:
			module = cast(Connector, importlib.import_module(module_name))
		except ImportError as error:
			last_error = error
			continue
		return module, driver.value
	if last_error is not None:
		raise RuntimeError("No configured MySQL connector is installed") from last_error
	raise RuntimeError("No MySQL connector is configured")


def _connect(configuration: DatabaseConfiguration, connector: Connector, connector_name: str, schema: str | None = None, *, autocommit: bool = False) -> Any:
	parameters: dict[str, object] = {
		"host": configuration.host,
		"port": configuration.port,
		"user": configuration.user,
		"password": configuration.password,
		"autocommit": autocommit,
	}
	if schema is not None:
		parameters["database"] = schema
	if connector_name == MySQLDriver.PYMYSQL.value:
		parameters.update(charset="utf8mb4", connect_timeout=30)
	else:
		parameters.update(connection_timeout=30, buffered=False)
	return connector.connect(**parameters)


def _execute(cursor: Any, statement: str, parameters: Sequence[object] = ()) -> None:
	cursor.execute(statement, tuple(parameters))


def _synthetic_sessions(count: int) -> tuple[date, ...]:
	result: list[date] = []
	current = date(2015, 1, 2)
	while len(result) < count:
		if current.weekday() < 5:
			result.append(current)
		current += timedelta(days=1)
	return tuple(result)


def build_population(seed: int = DEFAULT_SEED, session_count: int = DEFAULT_SESSION_COUNT, ticker_count: int = DEFAULT_TICKER_COUNT) -> Population:
	if seed != DEFAULT_SEED:
		raise ValueError(f"Stage 2 uses pinned synthetic seed {DEFAULT_SEED}")
	if session_count < 64 or ticker_count < 100:
		raise ValueError("Stage 2 requires a realistically large synthetic population")
	sessions = _synthetic_sessions(session_count)
	rows: list[tuple[int, int, str | None, int, int, float | None, int]] = []
	state_counts = {code: 0 for code in STATE_CODES.values()}
	quality_counts = {str(value): 0 for value in range(4)}
	for date_id in range(1, session_count + 1):
		for ticker_id in range(1, ticker_count + 1):
			bucket = (date_id * 31 + ticker_id * 17 + seed) % 100
			if bucket < 2:
				state_id = 3
			elif bucket < 4:
				state_id = 2
			elif bucket < 7:
				state_id = 4
			else:
				state_id = 1
			quality = (date_id * 13 + ticker_id * 7 + seed) % 4
			quality_counts[str(quality)] += 1
			code = STATE_CODES[state_id]
			state_counts[code] += 1
			if state_id in (2, 3):
				close = None
			else:
				close = float(Decimal("40.00000000") + Decimal(ticker_id % 251) / Decimal("10") + Decimal(date_id) / Decimal("1000"))
			rows.append((date_id, ticker_id, None if close is None else f"{close:.8f}", state_id, quality, None if close is None else (close % 17.0) / 5.0, 0 if state_id in (1, 4) else (2 if state_id == 3 else 4)))
	return Population(seed, session_count, ticker_count, tuple(rows), state_counts, quality_counts, sessions[0], sessions[-1])


def _tables(layout: str) -> CandidateTables:
	if layout not in LAYOUTS:
		raise ValueError("Unknown Stage 2 layout")
	suffix = "raw" if layout == "raw_column" else "separate"
	return CandidateTables(f"stage2_raw_stock_data_{suffix}", None if layout == "raw_column" else "stage2_raw_data_states", f"stage2_zscore_values_{suffix}")


def _schema_statements(layout: str) -> tuple[str, ...]:
	tables = _tables(layout)
	raw = _quote(tables.raw)
	zscores = _quote(tables.zscores)
	statements = (
		f"CREATE TABLE {raw} (ticker_id INT UNSIGNED NOT NULL, date_id INT UNSIGNED NOT NULL, close DECIMAL(28,8) NULL, quality_flags SMALLINT UNSIGNED NOT NULL, state_id SMALLINT UNSIGNED NOT NULL, PRIMARY KEY (ticker_id, date_id), KEY stage2_raw_date_ticker_state (date_id, ticker_id, state_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
		f"CREATE TABLE {zscores} (date_id INT UNSIGNED NOT NULL, ticker_id INT UNSIGNED NOT NULL, zscore FLOAT NULL, status TINYINT UNSIGNED NOT NULL, quality_flags SMALLINT UNSIGNED NOT NULL, PRIMARY KEY (date_id, ticker_id), KEY stage2_zscore_ticker_date_status (ticker_id, date_id, status, zscore), KEY stage2_zscore_date_status (date_id, status, ticker_id, zscore)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
	)
	if tables.states is None:
		return statements
	states = _quote(tables.states)
	return (statements[0].replace(", state_id SMALLINT UNSIGNED NOT NULL", "").replace(", KEY stage2_raw_date_ticker_state (date_id, ticker_id, state_id)", ""),
		statements[1],
		f"CREATE TABLE {states} (ticker_id INT UNSIGNED NOT NULL, date_id INT UNSIGNED NOT NULL, state_id SMALLINT UNSIGNED NOT NULL, PRIMARY KEY (ticker_id, date_id), KEY stage2_state_date_ticker (date_id, ticker_id, state_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin")


def _create_schema(connection: Any, schema: str) -> None:
	cursor = connection.cursor()
	try:
		_execute(cursor, f"CREATE DATABASE {_quote(schema)} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
	finally:
		cursor.close()


def _schema_exists(connection: Any, schema: str) -> bool:
	cursor = connection.cursor()
	try:
		_execute(cursor, "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (schema,))
		return int(cursor.fetchone()[0]) == 1
	finally:
		cursor.close()


def _drop_schema(connection: Any, schema: str) -> None:
	cursor = connection.cursor()
	try:
		_execute(cursor, f"DROP DATABASE {_quote(schema)}")
	finally:
		cursor.close()


def _create_tables(connection: Any) -> None:
	cursor = connection.cursor()
	try:
		for layout in LAYOUTS:
			for statement in _schema_statements(layout):
				_execute(cursor, statement)
	finally:
		cursor.close()


def _insert_batches(connection: Any, population: Population, layout: str, batch_size: int) -> float:
	tables = _tables(layout)
	raw = _quote(tables.raw)
	zscores = _quote(tables.zscores)
	raw_sql = f"INSERT INTO {raw} (ticker_id, date_id, close, quality_flags, state_id) VALUES (%s, %s, %s, %s, %s)" if layout == "raw_column" else f"INSERT INTO {raw} (ticker_id, date_id, close, quality_flags) VALUES (%s, %s, %s, %s)"
	state_sql = None if tables.states is None else f"INSERT INTO {_quote(tables.states)} (ticker_id, date_id, state_id) VALUES (%s, %s, %s)"
	zscore_sql = f"INSERT INTO {zscores} (date_id, ticker_id, zscore, status, quality_flags) VALUES (%s, %s, %s, %s, %s)"
	started = perf_counter()
	cursor = connection.cursor()
	try:
		for offset in range(0, len(population.rows), batch_size):
			batch = population.rows[offset:offset + batch_size]
			if layout == "raw_column":
				cursor.executemany(raw_sql, [(ticker, date_id, close, quality, state) for date_id, ticker, close, state, quality, _zscore, _status in batch])
			else:
				cursor.executemany(raw_sql, [(ticker, date_id, close, quality) for date_id, ticker, close, _state, quality, _zscore, _status in batch])
				assert state_sql is not None
				cursor.executemany(state_sql, [(ticker, date_id, state) for date_id, ticker, _close, state, _quality, _zscore, _status in batch])
			cursor.executemany(zscore_sql, [(date_id, ticker, zscore, status, quality) for date_id, ticker, _close, _state, quality, zscore, status in batch])
			connection.commit()
	finally:
		cursor.close()
	return perf_counter() - started


def _analyze(connection: Any) -> None:
	cursor = connection.cursor()
	try:
		for layout in LAYOUTS:
			for table in (_tables(layout).raw, _tables(layout).states, _tables(layout).zscores):
				if table is not None:
					_execute(cursor, f"ANALYZE TABLE {_quote(table)}")
					cursor.fetchall()
	finally:
		cursor.close()


def _query_case(population: Population) -> tuple[QueryCase, ...]:
	last = population.session_count
	return (
		QueryCase("narrow_ticker_lookback", last - 19, last, 7, 0),
		QueryCase("broad_training_range", 1, population.session_count, None, 0),
		QueryCase("quality_filtered_recent", last - 59, last, None, 0),
	)


def _query_sql(layout: str, case: QueryCase) -> tuple[str, tuple[object, ...]]:
	tables = _tables(layout)
	zscores = _quote(tables.zscores)
	if tables.states is None:
		state_join = f"JOIN {_quote(tables.raw)} AS r ON r.date_id = z.date_id AND r.ticker_id = z.ticker_id"
		state_filter = "r.state_id IN (%s, %s)"
	else:
		state_join = f"JOIN {_quote(tables.states)} AS r ON r.date_id = z.date_id AND r.ticker_id = z.ticker_id"
		state_filter = "r.state_id IN (%s, %s)"
	conditions = ["z.date_id BETWEEN %s AND %s", "z.status = %s", "z.quality_flags = %s", state_filter]
	parameters: list[object] = [case.start_date_id, case.end_date_id, 0, case.quality_flags, 1, 4]
	if case.ticker_id is not None:
		conditions.append("z.ticker_id = %s")
		parameters.append(case.ticker_id)
	statement = f"SELECT z.date_id, z.ticker_id, z.zscore FROM {zscores} AS z {state_join} WHERE {' AND '.join(conditions)} ORDER BY z.date_id, z.ticker_id"
	return statement, tuple(parameters)


def _run_query(connection: Any, statement: str, parameters: Sequence[object]) -> tuple[int, str, float]:
	cursor = connection.cursor()
	try:
		started = perf_counter()
		_execute(cursor, statement, parameters)
		count, digest = _rows_digest(cursor)
		return count, digest, perf_counter() - started
	finally:
		cursor.close()


def _warm_and_measure_queries(connection: Any, population: Population, repetitions: int) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
	if repetitions < 20:
		raise ValueError("Stage 2 requires at least 20 measured repetitions")
	results: dict[str, Any] = {layout: {} for layout in LAYOUTS}
	plans: dict[str, Any] = {layout: {} for layout in LAYOUTS}
	orders: list[dict[str, str]] = []
	for case in _query_case(population):
		for layout in LAYOUTS:
			statement, parameters = _query_sql(layout, case)
			for _ in range(2):
				_run_query(connection, statement, parameters)
			cursor = connection.cursor()
			try:
				_execute(cursor, "EXPLAIN " + statement, parameters)
				plans[layout][case.name] = [list(row) for row in cursor.fetchall()]
			finally:
				cursor.close()
			measurements: list[dict[str, Any]] = []
			for repetition in range(repetitions):
				order = (LAYOUTS if repetition % 2 == 0 else (LAYOUTS[1], LAYOUTS[0]))
				orders.append({"case": case.name, "repetition": str(repetition), "first": order[0], "second": order[1]})
				for layout in order:
					statement, parameters = _query_sql(layout, case)
					count, digest, elapsed = _run_query(connection, statement, parameters)
					measurements.append({"layout": layout, "repetition": repetition, "seconds": elapsed, "row_count": count, "result_sha256": digest})
		for layout in LAYOUTS:
			results[layout][case.name] = {
				"measurements": [item for item in measurements if item["layout"] == layout],
				"summary": _summary([float(item["seconds"]) for item in measurements if item["layout"] == layout]),
			}
	return results, plans, orders


def _verify_results(connection: Any, population: Population) -> dict[str, Any]:
	verification: dict[str, Any] = {}
	for case in _query_case(population):
		candidate_results: dict[str, Any] = {}
		for layout in LAYOUTS:
			statement, parameters = _query_sql(layout, case)
			count, digest, _elapsed = _run_query(connection, statement, parameters)
			candidate_results[layout] = {"row_count": count, "sha256": digest}
		verification[case.name] = {
			"candidates": candidate_results,
			"identical": len({(item["row_count"], item["sha256"]) for item in candidate_results.values()}) == 1,
		}
	return verification


def _write_rows(connection: Any, layout: str, date_id: int, ticker_count: int, *, operation: str) -> float:
	tables = _tables(layout)
	raw = _quote(tables.raw)
	state_values = [(ticker, date_id, "55.12345678", 0, 1) for ticker in range(1, ticker_count + 1)]
	cursor = connection.cursor()
	try:
		started = perf_counter()
		if operation == "insert":
			if layout == "raw_column":
				cursor.executemany(f"INSERT INTO {raw} (ticker_id, date_id, close, quality_flags, state_id) VALUES (%s, %s, %s, %s, %s)", state_values)
			else:
				cursor.executemany(f"INSERT INTO {raw} (ticker_id, date_id, close, quality_flags) VALUES (%s, %s, %s, %s)", [(ticker, date, close, quality) for ticker, date, close, quality, _state in state_values])
				assert tables.states is not None
				cursor.executemany(f"INSERT INTO {_quote(tables.states)} (ticker_id, date_id, state_id) VALUES (%s, %s, %s)", [(ticker, date, state) for ticker, date, _close, _quality, state in state_values])
		elif operation == "update":
			if layout == "raw_column":
				cursor.execute(f"UPDATE {raw} SET close = %s, state_id = %s WHERE date_id = %s", ("55.22345678", 4, date_id))
			else:
				cursor.execute(f"UPDATE {raw} SET close = %s WHERE date_id = %s", ("55.22345678", date_id))
				assert tables.states is not None
				cursor.execute(f"UPDATE {_quote(tables.states)} SET state_id = %s WHERE date_id = %s", (4, date_id))
		else:
			raise ValueError("Unknown write operation")
		connection.commit()
		return perf_counter() - started
	finally:
		cursor.close()


def _measure_writes(connection: Any, population: Population, repetitions: int, write_rows: int) -> dict[str, Any]:
	if repetitions < 20:
		raise ValueError("Stage 2 requires at least 20 measured repetitions")
	measurements: dict[str, dict[str, list[float]]] = {layout: {"insert": [], "update": []} for layout in LAYOUTS}
	base_date_id = population.session_count + 10000
	for repetition in range(repetitions):
		order = LAYOUTS if repetition % 2 == 0 else (LAYOUTS[1], LAYOUTS[0])
		for layout in order:
			date_id = base_date_id + repetition
			measurements[layout]["insert"].append(_write_rows(connection, layout, date_id, write_rows, operation="insert"))
			measurements[layout]["update"].append(_write_rows(connection, layout, date_id, write_rows, operation="update"))
		cursor = connection.cursor()
		try:
			for table in (_tables(layout).raw, _tables(layout).states):
				if table is not None:
					_execute(cursor, f"DELETE FROM {_quote(table)} WHERE date_id = %s", (date_id,))
			connection.commit()
		finally:
			cursor.close()
	return {layout: {operation: {"measurements_seconds": values, "summary": _summary(values)} for operation, values in operations.items()} for layout, operations in measurements.items()}


def _table_sizes(connection: Any) -> dict[str, Any]:
	cursor = connection.cursor()
	try:
		_execute(cursor, "SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, DATA_LENGTH + INDEX_LENGTH FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'stage2_%' ORDER BY TABLE_NAME")
		return {str(row[0]): {"estimated_rows": row[1], "data_bytes": row[2], "index_bytes": row[3], "total_bytes": row[4]} for row in cursor.fetchall()}
	finally:
		cursor.close()


def _indexes(connection: Any) -> dict[str, Any]:
	cursor = connection.cursor()
	try:
		_execute(cursor, "SELECT TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, CARDINALITY FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'stage2_%' ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX")
		result: dict[str, list[dict[str, object]]] = {}
		for table, index, sequence, column, cardinality in cursor.fetchall():
			result.setdefault(str(table), []).append({"index": index, "sequence": sequence, "column": column, "cardinality": cardinality})
		return result
	finally:
		cursor.close()


def _environment(connection: Any, connector_name: str) -> dict[str, Any]:
	cursor = connection.cursor()
	try:
		variables: dict[str, object] = {}
		for name, statement in {
			"server_version": "SELECT VERSION()",
			"version_comment": "SELECT @@version_comment",
			"sql_mode": "SELECT @@sql_mode",
			"storage_engine": "SELECT @@default_storage_engine",
			"innodb_buffer_pool_size": "SELECT @@innodb_buffer_pool_size",
			"innodb_flush_log_at_trx_commit": "SELECT @@innodb_flush_log_at_trx_commit",
			"performance_schema": "SELECT @@performance_schema",
		}.items():
			_execute(cursor, statement)
			variables[name] = cursor.fetchone()[0]
		return {
			"python": sys.version,
			"python_executable": sys.executable,
			"platform": platform.platform(),
			"processor": platform.processor(),
			"cpu_count": os.cpu_count(),
			"connector": connector_name,
			"mysql": variables,
			"cache_protocol": "two unmeasured warm-up executions per candidate and alternating candidate order; MySQL buffer-pool/page-cache eviction was not forced",
		}
	finally:
		cursor.close()


def _decision(query_results: dict[str, Any], write_results: dict[str, Any], verification: dict[str, Any], *, measurement_complete: bool) -> dict[str, Any]:
	if not measurement_complete:
		return {
			"selected_layout": "raw_column",
			"selection_status": "inconclusive",
			"rule": "select separate_state only when its geometric-mean median query ratio is < 0.90 and no query p95 or write latency ratio exceeds 1.10; otherwise raw_column",
			"reason": "The MySQL measurement gate is incomplete; the accepted conservative fallback is raw_column, not evidence that the layouts are equal.",
		}
	raw = query_results["raw_column"]
	separate = query_results["separate_state"]
	ratios = [float(separate[case]["summary"]["median_seconds"]) / float(raw[case]["summary"]["median_seconds"]) for case in raw]
	median_ratio = _geometric_mean(ratios)
	p95_ratios = [float(separate[case]["summary"]["p95_seconds"]) / float(raw[case]["summary"]["p95_seconds"]) for case in raw]
	write_ratios = [float(write_results["separate_state"][operation]["summary"]["median_seconds"]) / float(write_results["raw_column"][operation]["summary"]["median_seconds"]) for operation in ("insert", "update")]
	write_p95_ratios = [float(write_results["separate_state"][operation]["summary"]["p95_seconds"]) / float(write_results["raw_column"][operation]["summary"]["p95_seconds"]) for operation in ("insert", "update")]
	selected = median_ratio < 0.90 and max(p95_ratios + write_ratios + write_p95_ratios, default=0.0) <= 1.10
	return {
		"selected_layout": "separate_state" if selected else "raw_column",
		"selection_status": "measured",
		"geometric_mean_separate_to_raw_median_query_ratio": median_ratio,
		"per_case_separate_to_raw_p95_ratios": dict(zip(raw, p95_ratios, strict=True)),
		"separate_to_raw_write_median_ratios": dict(zip(("insert", "update"), write_ratios, strict=True)),
		"separate_to_raw_write_p95_ratios": dict(zip(("insert", "update"), write_p95_ratios, strict=True)),
		"selected_by_rule": selected,
		"verification_all_identical": all(bool(item["identical"]) for item in verification.values()),
	}


def incomplete_report(reason: str, *, schema: str | None = None, seed: int = DEFAULT_SEED, session_count: int = DEFAULT_SESSION_COUNT, ticker_count: int = DEFAULT_TICKER_COUNT, repetitions: int = DEFAULT_REPETITIONS) -> dict[str, Any]:
	population = build_population(seed, session_count, ticker_count)
	return {
		"benchmark": BENCHMARK_NAME,
		"stage": 2,
		"status": "incomplete",
		"synthetic": True,
		"reason": reason,
		"schema": schema,
		"dataset": {"seed": seed, "calendar": "synthetic weekday-only sessions", "session_count": session_count, "ticker_count": ticker_count, "raw_rows_per_layout": len(population.rows), "state_counts": population.state_counts, "quality_counts": population.quality_counts, "missing_placeholders_included": population.state_counts["missing"] > 0, "zscore_rows_per_layout": len(population.rows)},
		"protocol": {"measured_repetitions_per_query": repetitions, "warmup_executions_per_candidate": 2, "candidate_order": "alternating raw_column,separate_state and separate_state,raw_column", "write_repetitions": repetitions, "write_rows_per_repetition": DEFAULT_WRITE_ROWS},
		"decision": _decision({}, {}, {}, measurement_complete=False),
		"acceptance": {"reproducible_measurements": False, "equal_results": None, "recorded_decision": True, "measurement_gate": "incomplete"},
		"production_access": {"database": "none", "ddl": False, "dml": False, "provider_calls": False},
	}


def run_benchmark(configuration_file: Path, schema: str, *, seed: int = DEFAULT_SEED, session_count: int = DEFAULT_SESSION_COUNT, ticker_count: int = DEFAULT_TICKER_COUNT, repetitions: int = DEFAULT_REPETITIONS, write_rows: int = DEFAULT_WRITE_ROWS, batch_size: int = DEFAULT_BATCH_SIZE, keep_schema: bool = False) -> dict[str, Any]:
	configuration = load_database_configuration(configuration_file.resolve())
	schema = _validate_schema(schema, configuration.database)
	population = build_population(seed, session_count, ticker_count)
	connector, connector_name = _connector(configuration)
	admin = _connect(configuration, connector, connector_name, autocommit=True)
	created = False
	connection: Any | None = None
	try:
		if _schema_exists(admin, schema):
			raise ValueError("Disposable benchmark schema already exists; choose a new schema or remove the prior disposable schema")
		_create_schema(admin, schema)
		created = True
		connection = _connect(configuration, connector, connector_name, schema, autocommit=False)
		_create_tables(connection)
		load_seconds = {layout: _insert_batches(connection, population, layout, batch_size) for layout in LAYOUTS}
		_analyze(connection)
		environment = _environment(connection, connector_name)
		query_results, plans, orders = _warm_and_measure_queries(connection, population, repetitions)
		verification = _verify_results(connection, population)
		write_results = _measure_writes(connection, population, repetitions, write_rows)
		sizes = _table_sizes(connection)
		indexes = _indexes(connection)
		decision = _decision(query_results, write_results, verification, measurement_complete=True)
		return {
			"benchmark": BENCHMARK_NAME,
			"stage": 2,
			"status": "complete",
			"synthetic": True,
			"schema": schema,
			"dataset": {"seed": seed, "calendar": "synthetic weekday-only sessions", "session_count": session_count, "ticker_count": ticker_count, "raw_rows_per_layout": len(population.rows), "state_counts": population.state_counts, "quality_counts": population.quality_counts, "date_range": [population.start_date.isoformat(), population.end_date.isoformat()], "missing_placeholders_included": population.state_counts["missing"] > 0, "zscore_rows_per_layout": len(population.rows), "state_distribution": "identical deterministic state assignments in both layouts"},
			"environment": environment,
			"protocol": {"measured_repetitions_per_query": repetitions, "warmup_executions_per_candidate": 2, "candidate_order": "alternating raw_column,separate_state and separate_state,raw_column", "candidate_order_records": orders, "write_repetitions": repetitions, "write_rows_per_repetition": write_rows, "batch_size": batch_size, "query_timing": "execute plus complete result fetch; ORDER BY makes result digest deterministic", "cache_limitations": "MySQL buffer-pool/page-cache eviction was not forced; both candidates were warmed and measured in alternating order"},
			"load_seconds": load_seconds,
			"query_results": query_results,
			"query_plans": plans,
			"result_verification": verification,
			"write_results": write_results,
			"table_sizes": sizes,
			"indexes": indexes,
			"decision": decision,
			"acceptance": {"reproducible_measurements": True, "equal_results": all(bool(item["identical"]) for item in verification.values()), "recorded_decision": True, "measurement_gate": "complete"},
			"production_access": {"database": "disposable_only", "configured_database_selected": False, "ddl": False, "dml": False, "provider_calls": False},
		}
	finally:
		if connection is not None:
			connection.close()
		if created and not keep_schema:
			_drop_schema(admin, schema)
		admin.close()


def write_report(report: dict[str, Any], output: Path) -> None:
	output.parent.mkdir(parents=True, exist_ok=True)
	output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_summary(report: dict[str, Any], output: Path) -> None:
	"""Write the short human-readable companion to the machine report."""
	output.parent.mkdir(parents=True, exist_ok=True)
	dataset = report.get("dataset", {})
	decision = report.get("decision", {})
	acceptance = report.get("acceptance", {})
	lines = [
		"# Stage 2 raw-state placement benchmark",
		"",
		f"Status: **{report.get('status', 'unknown')}**",
		f"Synthetic dataset: seed `{dataset.get('seed')}`, {dataset.get('session_count')} sessions × {dataset.get('ticker_count')} tickers ({dataset.get('raw_rows_per_layout')} raw rows per layout).",
		f"Decision: **{decision.get('selected_layout')}** ({decision.get('selection_status')}).",
		f"Acceptance gate: measurements={acceptance.get('measurement_gate')}, equal-results={acceptance.get('equal_results')}, reproducible={acceptance.get('reproducible_measurements')}.",
		"",
		"The complete machine-readable record is `stage2-measurements.json`. The synthetic rows include explicit missing placeholders, all four source states, quality-filter values, and a z-score row for every ticker/date key in each candidate layout.",
	]
	if report.get("reason"):
		lines.extend(["", f"Limitation: {report['reason']}"])
	output.write_text("\n".join(lines) + "\n", encoding="utf-8")
