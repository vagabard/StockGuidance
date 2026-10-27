"""MySQL-only, bounded server-to-server execution for the Stage 12 operator.

No numerical table is materialized in Python. Checkpoints and destination
changes share one InnoDB transaction on the connection owning the writer lock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading
import time
from typing import Any

from stockguidance.database.simplified_migration import (
	MigrationError, MigrationPaths, _hash, _json, _session_date, _utc_datetime,
)
from stockguidance.database.simplified_schema import initialize_simplified_schema, registry_digest, require_simplified_schema
from stockguidance.database.table_names import TableRole, quote_identifier
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS
from stockguidance.market_data.quality import QualityFlag, QualityRecord


ENGINE_VERSION = 2
RUN = "stage12_migration_run"
STEPS = "stage12_migration_steps"
SESSIONS = "stage12_migration_sessions"
LOCK = "stockguidance_daily_maintenance"
RAW_COLUMNS = ("open", "high", "low", "close", "volume", "vwap", "transactions")


def columns(names: Sequence[str], alias: str = "") -> str:
	return ", ".join((alias + "." if alias else "") + quote_identifier(name) for name in names)


def after(keys: Sequence[str], values: Sequence[object], alias: str = "s") -> tuple[str, tuple[object, ...]]:
	"""Expanded lexicographic predicate, usable as a composite index range."""
	if not values:
		return "1=1", ()
	terms: list[str] = []
	parameters: list[object] = []
	for index, key in enumerate(keys):
		terms.append("(" + " AND ".join(
			[f"{alias}.{quote_identifier(prior)} = %s" for prior in keys[:index]]
			+ [f"{alias}.{quote_identifier(key)} > %s"]
		) + ")")
		parameters.extend(values[:index + 1])
	return "(" + " OR ".join(terms) + ")", tuple(parameters)


def bounded(keys: Sequence[str], low: Sequence[object], high: Sequence[object], alias: str = "s") -> tuple[str, tuple[object, ...]]:
	lower, lp = after(keys, low, alias)
	upper, up = after(keys, high, alias)
	return f"({lower}) AND NOT ({upper})", lp + up


@dataclass(frozen=True)
class Copy:
	table: str
	keys: tuple[str, ...]
	fields: tuple[str, ...]
	expressions: tuple[str, ...]
	joins: str = ""
	# Only exceptional/receipt/definition metadata can have a Python decoder.
	read_fields: tuple[str, ...] = ()
	decode: Callable[[Sequence[object]], tuple[object, ...]] | None = None
	target_table: str | None = None
	source_filter: str = ""


class SQLMigration:
	def __init__(self, owner: Any, paths: MigrationPaths) -> None:
		self.owner, self.paths = owner, paths
		self.names = owner.table_names
		self.batch_size = owner.batch_size
		self.connection: Any = None
		self.cursor: Any = None
		self.started = time.monotonic()
		self.activity = "preflight"
		self.server_uuid = ""
		self.migration_time = datetime.now(timezone.utc).replace(tzinfo=None)
		self.available: set[str] = set()
		self.stop = threading.Event()
		self.fault = owner.batch_hook

	def source(self, table: str) -> str:
		return quote_identifier(self.owner.legacy_database) + "." + quote_identifier(table)

	def target(self, table: str) -> str:
		return quote_identifier(self.owner.target_database) + "." + quote_identifier(table)

	def table(self, role: TableRole) -> str:
		return str(self.names.table(role))

	def emit(self, message: str) -> None:
		if self.owner.progress:
			self.owner.progress(message)

	def heartbeat(self) -> None:
		while not self.stop.wait(10):
			self.emit(f"{self.activity}: working; elapsed={time.monotonic() - self.started:.1f}s")

	def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
		self.cursor.execute(sql, parameters)

	def rows(self, sql: str, parameters: tuple[object, ...] = ()) -> list[Any]:
		self.execute(sql, parameters)
		return list(self.cursor.fetchall())

	def scalar(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
		return self.rows(sql, parameters)[0][0]

	def assert_lock(self) -> None:
		if self.scalar("SELECT IS_USED_LOCK(%s) = CONNECTION_ID()", (LOCK,)) != 1:
			raise MigrationError("Maintenance writer lock was lost; rerun to resume")

	@contextmanager
	def session(self, *, write: bool) -> Iterator[None]:
		self.connection = self.owner.target_connect(self.owner.target.database_config)
		self.cursor = self.connection.cursor()
		worker = threading.Thread(target=self.heartbeat, daemon=True)
		worker.start()
		locked = False
		try:
			self.execute("SET SESSION time_zone = '+00:00'")
			self.execute("SET SESSION div_precision_increment = 30")
			self.execute("SET SESSION sql_mode = 'STRICT_ALL_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
			self.server_uuid = str(self.scalar("SELECT @@server_uuid"))
			legacy = self.owner.legacy_connect(self.owner.legacy.database_config)
			try:
				q = legacy.cursor()
				try:
					q.execute("SELECT @@server_uuid", ())
					if str(q.fetchall()[0][0]) != self.server_uuid:
						raise MigrationError("SQL migration requires source and target on the same MySQL server")
				finally:
					q.close()
			finally:
				legacy.close()
			if write:
				if self.scalar("SELECT GET_LOCK(%s, 5)", (LOCK,)) != 1:
					raise MigrationError("Another market-data writer or migration is running")
				locked = True
			self.available = {str(r[0]) for r in self.rows(
				"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (self.owner.legacy_database,))}
			yield
		except BaseException:
			# Never replace a connection/query failure with a rollback failure.
			self.emit(f"{self.activity}: stopped; rerun the same command to reload the durable database checkpoint")
			with suppress(Exception):
				self.connection.rollback()
			raise
		finally:
			self.stop.set()
			worker.join(timeout=1)
			if locked:
				with suppress(Exception):
					self.execute("SELECT RELEASE_LOCK(%s)", (LOCK,))
					self.cursor.fetchall()
			with suppress(Exception):
				self.cursor.close()
			with suppress(Exception):
				self.connection.close()

	def identity(self) -> str:
		return _json({"engine": ENGINE_VERSION, "migration": self.owner._migration_id,
			"server_uuid": self.server_uuid, "registry": registry_digest(self.names)})

	def has_control(self) -> bool:
		return bool(self.scalar("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
			(self.owner.target_database, RUN)))

	def initialize_control(self) -> None:
		# Adopt only the known, empty old-engine target. No populated data is erased.
		has_run = self.has_control() and bool(self.scalar(f"SELECT COUNT(*) FROM {self.target(RUN)} WHERE id=1"))
		if not has_run:
			for spec in self.specifications():
				if self.scalar(f"SELECT EXISTS(SELECT 1 FROM {self.target(spec.target_table or spec.table)} LIMIT 1)"):
					raise MigrationError("Uncheckpointed target is populated; use a separate empty target")
		self.execute(f"CREATE TABLE IF NOT EXISTS {self.target(RUN)} (id TINYINT NOT NULL PRIMARY KEY, identity_json JSON NOT NULL, migration_time DATETIME(6) NOT NULL, phase VARCHAR(32) NOT NULL, CHECK(id=1)) ENGINE=InnoDB")
		self.execute(f"CREATE TABLE IF NOT EXISTS {self.target(STEPS)} (step VARCHAR(128) NOT NULL PRIMARY KEY, last_key JSON NOT NULL, committed_rows BIGINT UNSIGNED NOT NULL, complete TINYINT NOT NULL, report JSON NULL) ENGINE=InnoDB")
		self.execute(f"CREATE TABLE IF NOT EXISTS {self.target(SESSIONS)} (id INT UNSIGNED NOT NULL PRIMARY KEY, transaction_date DATE NOT NULL, prior_id INT UNSIGNED NULL, following_id INT UNSIGNED NULL, raw_revision CHAR(64) NULL, available_at DATETIME(6) NULL, derived_receipt TINYINT NOT NULL DEFAULT 0) ENGINE=InnoDB")
		existing = self.rows(f"SELECT identity_json, migration_time FROM {self.target(RUN)} WHERE id=1 FOR UPDATE")
		if existing:
			if json.loads(str(existing[0][0])) != json.loads(self.identity()):
				raise MigrationError("Database checkpoint belongs to another migration or engine version")
			self.migration_time = existing[0][1]
		else:
			self.execute(f"INSERT INTO {self.target(RUN)} VALUES (1,%s,%s,'copying')", (self.identity(), self.migration_time))
		self.connection.commit()
		self.execute("SET @stage12_time = %s", (self.migration_time,))
		self.check_reference_rows()
		self.connection.commit()
		# Per-session formula/completion checks must not scan the whole delta table.
		delta_table = self.table(TableRole.DELTAS)
		indexes = self.rows("SELECT INDEX_NAME,COLUMN_NAME,SEQ_IN_INDEX FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX", (self.owner.target_database, delta_table))
		if not any(r[1] == "date_id" and int(r[2]) == 1 for r in indexes):
			self.execute(f"CREATE INDEX stage12_delta_date_ticker ON {self.target(delta_table)} (date_id,ticker_id)")
		if not self.scalar(f"SELECT EXISTS(SELECT 1 FROM {self.target(SESSIONS)} LIMIT 1)"):
			# Calendar/receipt staging is removed after successful validation.
			# Rebuild only this small support data for a subsequent verification.
			self.execute(f"DELETE FROM {self.target(STEPS)} WHERE step IN ('prepare:sessions','prepare:receipts')")
			self.connection.commit()

	def initialize_schema(self) -> None:
		"""Resume migration-owned initial DDL without accepting unrelated partial schemas."""
		state = self.owner._target_state()
		journal = self.owner._load_journal(self.paths) if self.paths.journal.exists() else {}
		if state == "empty":
			journal = self.owner._write_journal(self.paths, "target_initialized", initializing_schema=True,
				engine_version=ENGINE_VERSION, server_uuid=self.server_uuid)
		if journal.get("initializing_schema") is not True:
			initialize_simplified_schema(self.owner.target, self.owner.target_connect, windows=self.owner.windows, table_names=self.names)
			self.connection.rollback()
			return
		if journal.get("server_uuid") != self.server_uuid or journal.get("target_schema", {}).get("registry_digest") != registry_digest(self.names):
			raise MigrationError("Schema initialization journal does not match this server/registry")
		from stockguidance.database.simplified_schema import simplified_schema_statements, retired_table_names, _STATE_DESCRIPTIONS, SCHEMA_NAME, REGISTRY_VERSION
		from stockguidance.database.simplified_contract import DATA_STATE_IDS, SCHEMA_VERSION
		from stockguidance.market_data.identity import data_configuration_key, source_key
		existing = {str(r[0]) for r in self.rows("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (self.owner.target_database,))}
		if existing.intersection(retired_table_names(self.names)):
			raise MigrationError("Initialization target contains retired operational tables")
		self.connection.rollback()
		for statement in simplified_schema_statements(self.names, self.owner.windows):
			if statement.startswith("CREATE INDEX "):
				pieces = statement.split()
				name, table = pieces[2], pieces[4].strip("`")
				if self.scalar("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s", (self.owner.target_database, table, name)):
					continue
			self.execute(statement)
			self.hook("schema_ddl", "initialize:schema")
		def seed(table: str, fields: tuple[str, ...], values: tuple[object, ...]) -> None:
			self.execute(f"INSERT INTO {self.target(table)} ({columns(fields)}) SELECT {','.join('%s' for _ in fields)} WHERE NOT EXISTS (SELECT 1 FROM {self.target(table)} WHERE id=%s)", (*values, values[0]))
		seed(self.table(TableRole.SCHEMA), ("id", "schema_name", "schema_version", "registry_version", "registry_digest", "created_at"),
			(1, SCHEMA_NAME, SCHEMA_VERSION, REGISTRY_VERSION, registry_digest(self.names), self.migration_time))
		for state_code, number in DATA_STATE_IDS.items():
			seed(self.table(TableRole.DATA_STATES), ("id", "code", "description"), (number, state_code.value, _STATE_DESCRIPTIONS[state_code]))
		configuration = self.owner.target.data_configuration
		seed(self.table(TableRole.SOURCE), ("id", "configuration_key", "source_key", "source_family", "endpoint", "market", "universe", "interval_code", "calendar_id", "raw_price_split_adjusted", "raw_price_dividend_adjusted", "raw_price_vendor_defined_adjustment", "created_at"),
			(1, data_configuration_key(configuration), source_key(self.owner.target.identity), configuration.source_family, configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value, configuration.calendar_id, configuration.raw_price_split_adjusted, configuration.raw_price_dividend_adjusted, configuration.raw_price_vendor_defined_adjustment, self.migration_time))
		self.connection.commit()
		self.check_reference_rows()
		self.connection.rollback()

	def check_reference_rows(self) -> None:
		require_simplified_schema(self.cursor, self.names)
		physical = set(self.names.target_tables().values()) | {RUN, STEPS, SESSIONS}
		for pair in self.names.resolve_zscore_tables(self.owner.windows).values():
			physical.update((pair.price, pair.price_delta))
		engines = self.rows("SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (self.owner.target_database,))
		if any(table in physical and engine != "InnoDB" for table, engine in engines):
			raise MigrationError("All migration destination/control tables must use InnoDB")
		rows = self.rows(f"SELECT id,code FROM {self.target(self.table(TableRole.DATA_STATES))} ORDER BY id")
		if rows != [(1, "good"), (2, "bad"), (3, "missing"), (4, "interpolated")]:
			raise MigrationError("Target state dictionary differs from the migration contract")
		from stockguidance.market_data.identity import data_configuration_key, source_key
		configuration = self.owner.target.data_configuration
		expected = (1, data_configuration_key(configuration), source_key(self.owner.target.identity), configuration.source_family,
			configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value,
			configuration.calendar_id, int(configuration.raw_price_split_adjusted), int(configuration.raw_price_dividend_adjusted), configuration.raw_price_vendor_defined_adjustment)
		actual = self.rows(f"SELECT id,configuration_key,source_key,source_family,endpoint,market,universe,interval_code,calendar_id,raw_price_split_adjusted,raw_price_dividend_adjusted,raw_price_vendor_defined_adjustment FROM {self.target(self.table(TableRole.SOURCE))}")
		if actual != [expected]:
			raise MigrationError("Target source configuration differs from the selected configuration")

	def checkpoint(self, step: str) -> tuple[list[Any], int, bool]:
		self.execute(f"INSERT INTO {self.target(STEPS)} (step,last_key,committed_rows,complete) SELECT %s,JSON_ARRAY(),0,0 WHERE NOT EXISTS (SELECT 1 FROM {self.target(STEPS)} WHERE step=%s)", (step, step))
		r = self.rows(f"SELECT last_key,committed_rows,complete FROM {self.target(STEPS)} WHERE step=%s FOR UPDATE", (step,))[0]
		self.activity = f"{step} last_known_key={r[0]} committed_rows={r[1]}"
		return json.loads(str(r[0])), int(r[1]), bool(r[2])

	def save(self, step: str, key: Sequence[object], count: int, done: bool, report: object = None) -> None:
		self.execute(f"UPDATE {self.target(STEPS)} SET last_key=%s, committed_rows=%s, complete=%s, report=%s WHERE step=%s",
			(_json(key), count, int(done), _json(report), step))

	def check_warnings(self) -> None:
		warnings = self.rows("SHOW WARNINGS LIMIT 20")
		if warnings:
			raise MigrationError(f"SQL migration produced warnings: {warnings!r}")

	def boundary(self, spec: Copy, low: Sequence[object], *, target: bool = False) -> tuple[list[Any], int] | None:
		predicate, parameters = after(spec.keys, low)
		physical = self.target(spec.target_table or spec.table) if target else self.source(spec.table)
		if spec.source_filter and not target:
			predicate = f"({predicate}) AND ({spec.source_filter})"
		# A bounded index scan in the server returns only one terminal key and count.
		sql = (f"SELECT {columns(spec.keys)}, batch_count FROM (SELECT {columns(spec.keys)}, COUNT(*) OVER () AS batch_count "
			f"FROM (SELECT {columns(spec.keys, 's')} FROM {physical} s WHERE {predicate} "
			f"ORDER BY {columns(spec.keys, 's')} LIMIT %s) b) z ORDER BY "
			+ ",".join(quote_identifier(k) + " DESC" for k in spec.keys) + " LIMIT 1")
		rows = self.rows(sql, parameters + (self.batch_size,))
		return (list(rows[0][:-1]), int(rows[0][-1])) if rows else None

	def hook(self, event: str, step: str) -> None:
		if self.fault:
			self.fault(event, step, self.connection)

	def progress(self, step: str, count: int, batch: int, high: Sequence[object]) -> None:
		elapsed = time.monotonic() - self.started
		self.activity = f"{step} rows={count} last_key={_json(high)}"
		self.emit(f"{self.activity} batch={batch} elapsed={elapsed:.1f}s rate={count / max(elapsed, .001):.1f} rows/s committed")

	def copy(self, spec: Copy) -> None:
		step = "copy:" + (spec.target_table or spec.table)
		while True:
			self.activity = step
			self.assert_lock()
			low, count, done = self.checkpoint(step)
			if done:
				self.connection.rollback()
				return
			end = self.boundary(spec, low)
			if end is None:
				self.save(step, low, count, True)
				self.connection.commit()
				return
			high, size = end
			where, params = bounded(spec.keys, low, high)
			if spec.source_filter:
				where = f"({where}) AND ({spec.source_filter})"
			if spec.decode:
				rows = self.rows(f"SELECT {columns(spec.read_fields, 's')} FROM {self.source(spec.table)} s WHERE {where} ORDER BY {columns(spec.keys, 's')}", params)
				converted = [spec.decode(r) for r in rows]
				self.cursor.executemany(f"INSERT INTO {self.target(spec.target_table or spec.table)} ({columns(spec.fields)}) VALUES ({','.join('%s' for _ in spec.fields)})", converted)
			else:
				self.execute(f"INSERT INTO {self.target(spec.table)} ({columns(spec.fields)}) SELECT {','.join(spec.expressions)} FROM {self.source(spec.table)} s {spec.joins} WHERE {where} ORDER BY {columns(spec.keys, 's')}", params)
			self.check_warnings()
			self.hook("after_insert", step)
			self.assert_lock()
			# Destination count also covers drivers without reliable executemany rowcount.
			actual = self.scalar(f"SELECT COUNT(*) FROM {self.target(spec.target_table or spec.table)} s WHERE {where}", params)
			if actual != size:
				raise MigrationError(f"{step}: batch count mismatch: expected {size}, got {actual}")
			invalid = ""
			if spec.table == self.table(TableRole.PRICES):
				invalid = "s.state_id=1 AND " + self.invalid_raw()
			elif "zscore" in spec.fields:
				invalid = "s.status NOT IN (0,1,2,3,4,5) OR (s.status=0 AND s.zscore IS NULL) OR (s.status<>0 AND s.zscore IS NOT NULL)"
			if invalid:
				bad = self.scalar(f"SELECT COUNT(*) FROM {self.target(spec.table)} s WHERE ({where}) AND ({invalid})", params)
				if bad:
					examples = self.rows(f"SELECT {columns(spec.keys, 's')} FROM {self.target(spec.table)} s WHERE ({where}) AND ({invalid}) LIMIT 20", params)
					raise MigrationError(f"{step}: invalid/unannotated observations={bad}; example keys={examples!r}; batch was not committed")
			self.save(step, high, count + size, False)
			self.hook("before_commit", step)
			self.connection.commit()
			self.hook("after_commit", step)
			self.progress(step, count + size, size, high)

	def quality(self, row: Sequence[object]) -> tuple[object, ...]:
		date_id, ticker_id, revision, payload = row
		r = QualityRecord.from_payload(json.loads(str(payload)))
		if r.revision != revision:
			raise MigrationError(f"Quality revision does not match payload at date_id={date_id}, ticker_id={ticker_id}")
		date = self.scalar(f"SELECT transaction_date FROM {self.target(self.table(TableRole.SESSIONS))} WHERE id=%s", (date_id,))
		label = self.scalar(f"SELECT label FROM {self.target(self.table(TableRole.TICKERS))} WHERE id=%s", (ticker_id,))
		if r.observation.session_date != _session_date(date) or r.observation.ticker != label:
			raise MigrationError("Quality payload identity differs from its relationship keys")
		state = 4 if r.effective is not None and r.flags & (QualityFlag.INTERPOLATED_SINGLE | QualityFlag.INTERPOLATED_GAP) else 2
		ids = []
		for day in (r.prior, r.following):
			matches = [] if day is None else self.rows(f"SELECT id FROM {self.target(self.table(TableRole.SESSIONS))} WHERE transaction_date=%s", (day,))
			if day is not None and not matches:
				raise MigrationError("Repair endpoint date is absent from legacy session dictionary")
			ids.append(matches[0][0] if matches else None)
		def values(items: Sequence[object]) -> list[str | None]:
			return [None if v is None else str(v) for v in items]
		def utc(dt: datetime | None) -> datetime | None:
			return None if dt is None else dt.astimezone(timezone.utc).replace(tzinfo=None)
		return (date_id, ticker_id, revision, state, _json(values(r.observation.values)),
			None if r.effective is None else _json(values(r.effective)), _json(r.fields),
			None if r.effective is None else "linear-trading-session-v1", *ids,
			None if not r.endpoint_values else _json([values(v) for v in r.endpoint_values]),
			utc(r.observation.available_at), utc(r.available_at), utc(r.endpoints_available_at), self.migration_time)

	def specifications(self) -> list[Copy]:
		n = self.table
		def plain(role: TableRole, keys: tuple[str, ...], fields: tuple[str, ...], extra: tuple[str, ...] = (), extra_values: tuple[str, ...] = ()) -> Copy:
			return Copy(n(role), keys, fields + extra, tuple("s." + quote_identifier(f) for f in fields) + extra_values)
		specs = [plain(TableRole.SESSIONS, ("id",), ("id", "transaction_date")),
			plain(TableRole.TICKERS, ("id",), ("id", "label", "exchange"))]
		qtable = self.owner._historical_table("row_quality")
		if qtable in self.available:
			specs.append(Copy(qtable, ("date_id", "ticker_id"),
				("date_id", "ticker_id", "revision", "state_id", "original_values", "effective_values", "invalid_fields", "interpolation_method", "prior_date_id", "following_date_id", "endpoint_values", "observed_at", "available_at", "endpoints_available_at", "recorded_at"), (),
				read_fields=("date_id", "ticker_id", "revision", "payload"), decode=self.quality, target_table=n(TableRole.REPAIR_METADATA)))
		base: tuple[str, ...] = ("ticker_id", "date_id", *RAW_COLUMNS)
		specs.append(Copy(n(TableRole.PRICES), ("ticker_id", "date_id"), (*base, "state_id", "observed_at"),
			tuple("s." + quote_identifier(f) for f in base) + ("COALESCE(q.state_id,1)", "q.observed_at"),
			f"LEFT JOIN {self.target(n(TableRole.REPAIR_METADATA))} q ON q.date_id=s.date_id AND q.ticker_id=s.ticker_id"))
		output = self.owner._historical_table("output_quality")
		def quality_join(series: str) -> tuple[str, tuple[str, ...]]:
			if output not in self.available:
				return "", ("0", "0", "JSON_OBJECT()")
			return (f"LEFT JOIN {self.source(output)} q ON q.series_key={series} AND q.date_id=s.date_id AND q.ticker_id=s.ticker_id",
				("COALESCE(q.flags,0)", "COALESCE(q.gap_length,0)", "COALESCE(q.provenance,JSON_OBJECT())"))
		join, qvalues = quality_join("'legacy-derived'")
		base = ("ticker_id", "date_id", *DERIVED_COLUMNS)
		status = ("CASE WHEN a.prior_id IS NULL THEN 'insufficient_history' "
			"WHEN c.ticker_id IS NULL OR p.ticker_id IS NULL OR c.state_id IN (2,3) OR p.state_id IN (2,3) "
			"OR c.open IS NULL OR c.high IS NULL OR c.low IS NULL OR c.close IS NULL "
			"OR p.open IS NULL OR p.high IS NULL OR p.low IS NULL OR p.close IS NULL THEN 'missing_input' WHEN "
			+ " OR ".join("s." + quote_identifier(f) + " IS NULL" for f in DERIVED_COLUMNS) + " THEN 'not_yet_available' ELSE 'ready' END")
		join += (f" LEFT JOIN {self.target(SESSIONS)} a ON a.id=s.date_id"
			f" LEFT JOIN {self.target(n(TableRole.PRICES))} c ON c.date_id=s.date_id AND c.ticker_id=s.ticker_id"
			f" LEFT JOIN {self.target(n(TableRole.PRICES))} p ON p.date_id=a.prior_id AND p.ticker_id=s.ticker_id")
		specs.append(Copy(n(TableRole.DELTAS), ("ticker_id", "date_id"), (*base, "calculation_status", "quality_flags", "gap_length", "provenance", "completed_at"),
			tuple("s." + quote_identifier(f) for f in base) + (status, *qvalues, "@stage12_time"), join))
		series_fields = ("series_key", "schema_version", "source_scope", "calendar_id", "family", "window", "calculation_version", "delta_definition", "precision", "missing_value_policy", "created_at")
		if n(TableRole.ZSCORE_SERIES) in self.available:
			def series_decode(r: Sequence[object]) -> tuple[object, ...]:
				from stockguidance.zscores.storage import ZScoreSeriesDefinition
				from stockguidance.zscores.specification import ZScoreFamily
				definition = ZScoreSeriesDefinition(str(r[2]), str(r[3]), ZScoreFamily(str(r[4])), int(str(r[5])),
					str(r[6]), str(r[8]), str(r[7]), str(r[9]), int(str(r[1])))
				if definition.series_key != r[0]:
					raise MigrationError("Z-score definition fingerprint does not match its fields")
				return (*r[:-1], _utc_datetime(r[-1], "series creation time").replace(tzinfo=None))
			specs.append(Copy(n(TableRole.ZSCORE_SERIES), ("series_key",), series_fields, (), read_fields=series_fields, decode=series_decode))
		meta = ("series_key", "date_id", "dependency_digest", "availability_digest", "actual_input_available", "completed_at", "row_count", "ready_count", "status_counts", "content_digest")
		if n(TableRole.ZSCORE_SESSIONS) in self.available:
			selected = " OR ".join(
				f"(z.family='price' AND z.`window`={window}) OR (z.family='price_delta' AND z.`window`={window})"
				for window in self.owner.windows
			)
			# Legacy databases can retain completion rows for retired series. Only
			# definitions selected for this migration may cross the FK boundary.
			series_filter = f"s.series_key IN (SELECT z.series_key FROM {self.source(n(TableRole.ZSCORE_SERIES))} z WHERE {selected})"
			def metadata_decode(r: Sequence[object]) -> tuple[object, ...]:
				if any(_hash(r[i]) is None for i in (0, 2, 3, 9)) or int(str(r[4])) not in (0, 1):
					raise MigrationError("Invalid z-score completion identity/availability metadata")
				return (*r[:5], _utc_datetime(r[5], "z-score completion").replace(tzinfo=None), *r[6:8], _json(status_counts(r[8])), r[9])
			# The filter is literal SQL; the selected family/window values are stable
			# migration configuration and are bound by the source query below.
			specs.append(Copy(n(TableRole.ZSCORE_SESSIONS), ("series_key", "date_id"), meta, (), read_fields=meta, decode=metadata_decode, source_filter=series_filter))
		for window in self.owner.windows:
			pair = self.names.resolve_zscore_tables((window,))[window]
			for family, table in (("price", pair.price), ("price_delta", pair.price_delta)):
				if table not in self.available:
					continue
				definitions = self.rows(f"SELECT series_key FROM {self.source(n(TableRole.ZSCORE_SERIES))} WHERE family=%s AND `window`=%s", (family, window))
				if len(definitions) != 1 or _hash(definitions[0][0]) is None:
					raise MigrationError(f"Expected exactly one series definition for {family}:{window}")
				# Series keys are validated lowercase hex before becoming SQL literals.
				join, qvalues = quality_join("'" + str(definitions[0][0]) + "'")
				base = ("date_id", "ticker_id", "zscore", "status")
				specs.append(Copy(table, ("date_id", "ticker_id"), (*base, "quality_flags", "gap_length", "provenance"),
					tuple("s." + quote_identifier(f) for f in base) + qvalues, join))
		return specs

	def prepare_sessions(self) -> None:
		step = "prepare:sessions"
		_, _, done = self.checkpoint(step)
		if not done:
			self.execute(f"INSERT INTO {self.target(SESSIONS)} (id,transaction_date,prior_id,following_id) SELECT id,transaction_date,LAG(id) OVER (ORDER BY transaction_date),LEAD(id) OVER (ORDER BY transaction_date) FROM {self.target(self.table(TableRole.SESSIONS))}")
			self.save(step, [], 0, True)
		self.connection.commit()
		step = "prepare:receipts"
		while True:
			low, count, done = self.checkpoint(step)
			if done:
				self.connection.rollback()
				break
			where = "session_date > %s" if low else "1=1"
			r = self.rows(f"SELECT session_date,raw_id,available_at,derived_dependency_id,derived_content_id FROM {self.source(self.table(TableRole.IMPORTS))} WHERE {where} ORDER BY session_date LIMIT %s", (*low, self.batch_size))
			for day, revision, available, dep, digest in r:
				try:
					timestamp = _utc_datetime(available, "receipt availability").replace(tzinfo=None)
				except MigrationError:
					timestamp = None
				valid = _hash(revision) is not None and timestamp is not None
				self.execute(f"UPDATE {self.target(SESSIONS)} SET raw_revision=%s,available_at=%s,derived_receipt=%s WHERE transaction_date=%s",
					(revision if valid else None, timestamp if valid else None, int(valid and _hash(dep) is not None and _hash(digest) is not None), day))
			self.save(step, [str(r[-1][0])] if r else low, count + len(r), not r)
			self.assert_lock()
			self.connection.commit()

	def invalid_raw(self, alias: str = "s") -> str:
		def p(name: str) -> str:
			return alias + "." + quote_identifier(name)
		return "(" + " OR ".join(
			[f"{p(f)} IS NULL OR {p(f)}<=0" for f in RAW_COLUMNS[:4]]
			+ [f"{p('volume')} IS NULL OR {p('volume')}<0 OR {p('volume')}>9223372036854775807",
				f"{p('vwap')} IS NOT NULL AND {p('vwap')}<=0",
				f"{p('transactions')} IS NOT NULL AND ({p('transactions')}<0 OR {p('transactions')}>9223372036854775807)",
				f"{p('low')}>LEAST({p('open')},{p('close')})", f"{p('high')}<GREATEST({p('open')},{p('close')})", f"{p('low')}>{p('high')}"]
		) + ")"

	def effective(self, alias: str, index: int) -> str:
		field = RAW_COLUMNS[index]
		type_sql = "DECIMAL(42,22)" if index == 5 else "DECIMAL(28,8)" if index < 4 else "DECIMAL(28,8)"
		return f"CASE WHEN {alias}.state_id=4 THEN CAST(JSON_UNQUOTE(JSON_EXTRACT({alias}q.effective_values,'$[{index}]')) AS {type_sql}) ELSE {alias}.{quote_identifier(field)} END"

	def expected_delta_sql(self, date_id: int) -> tuple[str, tuple[object, ...]]:
		"""Independent SQL reference for the existing sixteen-column kernel."""
		raw, repair, tickers = (self.target(self.table(r)) for r in (TableRole.PRICES, TableRole.REPAIR_METADATA, TableRole.TICKERS))
		ce, pe, ne = ([self.effective(a, i) for i in range(7)] for a in ("c", "p", "n"))
		ratios = [f"CAST(({x})/NULLIF(({y}),0) AS DOUBLE)" for x, y in zip(ce, pe)]
		outcome = f"CASE WHEN n.state_id IN (1,4) THEN CAST(({ne[3]})/NULLIF(({ne[0]}),0) AS DOUBLE) END"
		base = (f"SELECT c.ticker_id,c.date_id,{','.join(e + ' AS ' + quote_identifier(f) for e, f in zip(ratios, DERIVED_COLUMNS[:7]))},"
			f"CAST(({ce[3]})/NULLIF(({ce[0]}),0) AS DOUBLE) AS day_change,{outcome} AS outcome,t.label "
			f"FROM {self.target(SESSIONS)} a JOIN {raw} c ON c.date_id=a.id AND c.state_id IN (1,4) "
			f"JOIN {tickers} t ON t.id=c.ticker_id LEFT JOIN {repair} cq ON cq.date_id=c.date_id AND cq.ticker_id=c.ticker_id "
			f"LEFT JOIN {raw} p ON p.date_id=a.prior_id AND p.ticker_id=c.ticker_id AND p.state_id IN (1,4) "
			f"LEFT JOIN {repair} pq ON pq.date_id=p.date_id AND pq.ticker_id=p.ticker_id "
			f"LEFT JOIN {raw} n ON n.date_id=a.following_id AND n.ticker_id=c.ticker_id AND n.state_id IN (1,4) "
			f"LEFT JOIN {repair} nq ON nq.date_id=n.date_id AND nq.ticker_id=n.ticker_id WHERE a.id=%s")
		ranked = f"SELECT b.*,ROW_NUMBER() OVER (ORDER BY outcome DESC, CAST(label AS BINARY)) AS ranking FROM ({base}) b"
		extreme = "((CASE WHEN outcome>=1 THEN 0.5 ELSE 0 END)+outcome/(2*(outcome+1)))"
		values = [*('b.' + quote_identifier(f) for f in DERIVED_COLUMNS[:7]), "day_change", "outcome", "outcome-1",
			f"CASE WHEN outcome IS NOT NULL THEN {extreme} END", "CASE WHEN outcome IS NOT NULL THEN outcome>=1 END",
			"CASE WHEN outcome IS NOT NULL THEN ranking<=100 END", f"CASE WHEN outcome IS NOT NULL THEN {extreme}*2-1 END",
			"CASE WHEN outcome IS NOT NULL THEN (ranking<=100)*2-1 END", "CASE WHEN outcome IS NOT NULL THEN (outcome>=1)*2-1 END"]
		return f"SELECT ticker_id,date_id,{','.join(values)} FROM ({ranked}) b", (date_id,)

	def completion(self, *, verify: bool = False) -> None:
		temp = quote_identifier("stage12_expected_delta")
		self.execute(f"CREATE TEMPORARY TABLE IF NOT EXISTS {temp} (ticker_id INT UNSIGNED NOT NULL,date_id INT UNSIGNED NOT NULL," + ",".join(quote_identifier(f) + " DOUBLE NULL" for f in DERIVED_COLUMNS) + ", PRIMARY KEY(ticker_id,date_id)) ENGINE=InnoDB")
		while True:
			step = "validate:completion" if verify else "completion:sessions"
			self.activity = step
			self.assert_lock()
			low, count, done = self.checkpoint(step)
			if done:
				self.connection.rollback()
				return
			rows = self.rows(f"SELECT id,raw_revision,available_at,derived_receipt FROM {self.target(SESSIONS)} WHERE id>%s ORDER BY id LIMIT 1", (low[0] if low else 0,))
			if not rows:
				self.save(step, low, count, True)
				self.connection.commit()
				return
			date_id, revision, available, derived_receipt = rows[0]
			invalid = self.scalar(f"SELECT COUNT(*) FROM {self.target(self.table(TableRole.PRICES))} s WHERE s.date_id=%s AND s.state_id=1 AND {self.invalid_raw()}", (date_id,))
			if invalid:
				raise MigrationError(f"Unannotated invalid raw observations: date_id={date_id}, count={invalid}; completion blocked")
			raw_ok = revision is not None and available is not None
			delta_ok = False
			if raw_ok and derived_receipt:
				self.execute(f"DELETE FROM {temp}")
				sql, parameters = self.expected_delta_sql(date_id)
				self.execute(f"INSERT INTO {temp} {sql}", parameters)
				self.check_warnings()
				comparison = " OR ".join(numeric_difference("s." + quote_identifier(f), "t." + quote_identifier(f)) for f in DERIVED_COLUMNS)
				actual = self.target(self.table(TableRole.DELTAS))
				mismatch = self.scalar(f"SELECT COUNT(*) FROM {temp} s LEFT JOIN {actual} t ON t.ticker_id=s.ticker_id AND t.date_id=s.date_id WHERE t.ticker_id IS NULL OR {comparison}")
				extra = self.scalar(f"SELECT COUNT(*) FROM {actual} t LEFT JOIN {temp} s ON s.ticker_id=t.ticker_id AND s.date_id=t.date_id WHERE t.date_id=%s AND s.ticker_id IS NULL", (date_id,))
				delta_ok = mismatch == 0 and extra == 0
			values = (int(raw_ok), available if raw_ok else None, self.migration_time if raw_ok else None, revision if raw_ok else None, int(delta_ok), self.migration_time if delta_ok else None)
			if verify:
				actual_values = self.rows(f"SELECT raw_complete,raw_available_at,raw_completed_at,raw_revision,delta_complete,delta_completed_at FROM {self.target(self.table(TableRole.SESSIONS))} WHERE id=%s", (date_id,))
				if actual_values != [values]:
					raise MigrationError(f"Completion mismatches at date_id={date_id}; expected={values!r}, actual={actual_values!r}")
			else:
				self.execute(f"UPDATE {self.target(self.table(TableRole.SESSIONS))} SET raw_complete=%s,raw_available_at=%s,raw_completed_at=%s,raw_revision=%s,delta_complete=%s,delta_completed_at=%s WHERE id=%s", (*values, date_id))
			self.save(step, [date_id], count + 1, False)
			self.assert_lock()
			self.hook("before_commit", step)
			self.connection.commit()
			self.hook("after_commit", step)
			self.progress(step, count + 1, 1, [date_id])

	def validate_copy(self, spec: Copy, *, restart: bool = False) -> None:
		step = "validate:" + (spec.target_table or spec.table)
		if restart:
			self.execute(f"DELETE FROM {self.target(STEPS)} WHERE step IN (%s,%s)", (step, step + ":extra"))
			self.connection.commit()
		for reverse in (False, True):
			name = step + (":extra" if reverse else "")
			while True:
				self.activity = name
				self.assert_lock()
				low, count, done = self.checkpoint(name)
				if done:
					self.connection.rollback()
					break
				end = self.boundary(spec, low, target=reverse)
				if end is None:
					self.save(name, low, count, True)
					self.connection.commit()
					break
				high, size = end
				where, params = bounded(spec.keys, low, high)
				join = " AND ".join("t." + quote_identifier(k) + "=s." + quote_identifier(k) for k in spec.keys)
				if spec.source_filter and not reverse:
					where = f"({where}) AND ({spec.source_filter})"
				if spec.decode and not reverse:
					source_rows = self.rows(f"SELECT {columns(spec.read_fields, 's')} FROM {self.source(spec.table)} s WHERE {where} ORDER BY {columns(spec.keys, 's')}", params)
					expected = [spec.decode(r) for r in source_rows]
					actual = self.rows(f"SELECT {columns(spec.fields, 's')} FROM {self.target(spec.target_table or spec.table)} s WHERE {where} ORDER BY {columns(spec.keys, 's')}", params)
					if len(expected) != len(actual) or any(not metadata_equal(spec.fields, e, a) for e, a in zip(expected, actual)):
						raise MigrationError(f"{name}: decoded metadata differs in range {_json(low)} .. {_json(high)}")
				else:
					if reverse:
						left, right = self.target(spec.target_table or spec.table), self.source(spec.table)
						mismatch = "t." + quote_identifier(spec.keys[0]) + " IS NULL"
						joins = ""
						if spec.source_filter:
							joins += " AND " + spec.source_filter.replace("s.", "t.")
					else:
						left, right = self.source(spec.table), self.target(spec.table)
						mismatch = "t." + quote_identifier(spec.keys[0]) + " IS NULL OR " + " OR ".join(
							f"NOT ({'CAST(' + e + ' AS JSON)' if f == 'provenance' else e} <=> t.{quote_identifier(f)})" for f, e in zip(spec.fields, spec.expressions))
						if "zscore" in spec.fields:
							mismatch += " OR t.status NOT IN (0,1,2,3,4,5) OR (t.status=0 AND t.zscore IS NULL) OR (t.status<>0 AND t.zscore IS NOT NULL)"
						joins = spec.joins
					bad = self.scalar(f"SELECT COUNT(*) FROM {left} s {joins} LEFT JOIN {right} t ON {join} WHERE ({where}) AND ({mismatch})", params)
					if bad:
						examples = self.rows(f"SELECT {columns(spec.keys, 's')} FROM {left} s {joins} LEFT JOIN {right} t ON {join} WHERE ({where}) AND ({mismatch}) LIMIT 20", params)
						raise MigrationError(f"{name}: mismatches={bad}; example keys={examples!r}")
				self.save(name, high, count + size, False)
				self.assert_lock()
				self.hook("before_commit", name)
				self.connection.commit()
				self.hook("after_commit", name)
				self.progress(name, count + size, size, high)

	def report(self) -> dict[str, object]:
		steps = self.rows(f"SELECT step,last_key,committed_rows,complete FROM {self.target(STEPS)} ORDER BY step")
		return {"engine_version": ENGINE_VERSION, "steps": [{"step": r[0], "last_key": json.loads(str(r[1])), "committed_rows": r[2], "complete": bool(r[3])} for r in steps]}

	def completion_report(self) -> dict[str, object]:
		table = self.target(self.table(TableRole.SESSIONS))
		counts = self.rows(f"SELECT COUNT(*),COALESCE(SUM(raw_complete),0),COALESCE(SUM(delta_complete),0) FROM {table}")[0]
		deferred = self.rows(f"SELECT id,transaction_date,raw_complete,delta_complete FROM {table} WHERE raw_complete=0 OR delta_complete=0 ORDER BY transaction_date")
		return {"sessions": int(counts[0]), "raw_complete": int(counts[1]), "delta_complete": int(counts[2]), "deferred": deferred,
			"repair_action": "Run maintained readiness/maintenance for deferred sessions; migration never promotes unknown completion."}

	def validate_zscore_completion(self) -> None:
		metadata = self.table(TableRole.ZSCORE_SESSIONS)
		if metadata not in self.available:
			return
		step = "validate:zscore_counts"
		spec = Copy(metadata, ("series_key", "date_id"), (), ())
		definitions = self.rows(f"SELECT series_key,family,`window` FROM {self.target(self.table(TableRole.ZSCORE_SERIES))}")
		physical = {}
		for key, family, window in definitions:
			if int(window) in self.owner.windows:
				pair = self.names.resolve_zscore_tables((int(window),))[int(window)]
				physical[key] = pair.price if family == "price" else pair.price_delta
		while True:
			self.activity = step
			self.assert_lock()
			low, count, done = self.checkpoint(step)
			if done:
				self.connection.rollback()
				return
			end = self.boundary(spec, low, target=True)
			if end is None:
				self.save(step, low, count, True)
				self.connection.commit()
				return
			high, size = end
			where, params = bounded(spec.keys, low, high)
			for key, date_id, row_count, ready_count, encoded in self.rows(f"SELECT series_key,date_id,row_count,ready_count,status_counts FROM {self.target(metadata)} s WHERE {where}", params):
				if key not in physical:
					raise MigrationError(f"Z-score completion has an unselected/unknown definition: {key}")
				actual = {str(r[0]): int(r[1]) for r in self.rows(f"SELECT status,COUNT(*) FROM {self.target(physical[key])} WHERE date_id=%s GROUP BY status", (date_id,))}
				if sum(actual.values()) != row_count or actual.get("0", 0) != ready_count or actual != status_counts(encoded):
					raise MigrationError(f"Z-score completion counts mismatch for {physical[key]}, date_id={date_id}: {actual!r}")
			self.save(step, high, count + size, False)
			self.assert_lock()
			self.connection.commit()
			self.progress(step, count + size, size, high)

	def validate_all(self, *, restart: bool = False) -> dict[str, object]:
		self.check_reference_rows()
		for spec in self.specifications():
			self.validate_copy(spec, restart=restart)
		self.completion(verify=True)
		self.validate_zscore_completion()
		# Dictionary IDs were explicitly inserted into AUTO_INCREMENT columns.
		for role in (TableRole.SESSIONS, TableRole.TICKERS):
			table = self.table(role)
			maximum = self.scalar(f"SELECT COALESCE(MAX(id),0) FROM {self.target(table)}")
			next_id = self.scalar("SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (self.owner.target_database, table))
			if next_id is None or int(next_id) <= int(maximum):
				raise MigrationError(f"AUTO_INCREMENT is not above copied IDs in {table}")
		return {**self.report(), "completion": self.completion_report()}

	def preflight(self) -> None:
		self.activity = "preflight"
		for spec in self.specifications():
			if spec.table not in self.available:
				raise MigrationError(f"Missing legacy table {spec.table}")
			engine = self.scalar("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (self.owner.legacy_database, spec.table))
			if engine != "InnoDB":
				raise MigrationError(f"Source table {spec.table} must use InnoDB")
			primary = tuple(str(r[0]) for r in self.rows("SELECT COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX", (self.owner.legacy_database, spec.table)))
			if primary != spec.keys:
				raise MigrationError(f"Source primary key differs from expected keyset for {spec.table}: {primary!r}")
			read_fields = spec.read_fields or spec.fields
			if not spec.read_fields:
				if "zscore" in spec.fields:
					read_fields = spec.fields[:4]
				elif "calculation_status" in spec.fields:
					read_fields = spec.fields[:2 + len(DERIVED_COLUMNS)]
				elif "state_id" in spec.fields:
					read_fields = spec.fields[:9]
			self.execute(f"SELECT {columns(read_fields, 's')} FROM {self.source(spec.table)} s LIMIT 0")
			self.cursor.fetchall()
		output_quality = self.owner._historical_table("output_quality")
		if output_quality in self.available and self.scalar(f"SELECT COUNT(*) FROM {self.source(output_quality)} WHERE NOT JSON_VALID(provenance) OR flags<0 OR flags>15 OR gap_length<0"):
			raise MigrationError("Legacy output-quality metadata has invalid flags, gap length, or provenance JSON")
		# Check active markers and semantic configuration without large COUNT scans.
		plan = self.owner._require_plan(self.paths)
		if plan.payload.get("backup", {}).get("status") != "verified":
			raise MigrationError("A verified backup/restore manifest is required")
		self.connection.rollback()
		if self.has_control():
			if self.scalar(f"SELECT COUNT(*) FROM {self.target(RUN)} WHERE phase='cutover'"):
				raise MigrationError("Apply is unavailable after cutover")
			if self.scalar("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (self.owner.target_database, STEPS)) and self.scalar(f"SELECT COUNT(*) FROM {self.target(STEPS)} WHERE step='cutover:confirmed' AND complete=1"):
				raise MigrationError("Apply is unavailable after cutover")
		if self.paths.journal.exists() and self.owner._load_journal(self.paths).get("phase") in {"cutover", "retired"}:
			raise MigrationError("Apply is unavailable after cutover; use status or validation")

	def run(self, operation: str, *, confirmation: str = "") -> dict[str, object]:
		if operation == "apply" and confirmation != f"APPLY {self.owner.target_database}":
			raise MigrationError(f"Apply requires --apply-confirmation 'APPLY {self.owner.target_database}'")
		if operation == "cutover":
			if confirmation != f"CUTOVER {self.owner.target_database}":
				raise MigrationError(f"Cutover requires --cutover-confirmation 'CUTOVER {self.owner.target_database}'")
			if self.owner._load_journal(self.paths).get("phase") not in {"validated", "cutover"}:
				raise MigrationError("Cutover requires a validated apply journal")
		with self.session(write=operation != "status"):
			if operation == "status":
				if not self.has_control():
					return {"engine_version": ENGINE_VERSION, "phase": "not_started", "steps": []}
				r = self.rows(f"SELECT identity_json,phase FROM {self.target(RUN)} WHERE id=1")
				if not r or json.loads(str(r[0][0])) != json.loads(self.identity()):
					raise MigrationError("Database checkpoint identity does not match")
				return {"phase": r[0][1], **self.report()}
			if operation == "apply":
				self.preflight()
				# This second verifier is deliberately omitted: plan already streamed both hashes.
				export = self.owner._export_metadata(self.paths)
				self.initialize_schema()
				# DDL was committed on the initializer connection. Drop any older
				# source inspection snapshot before reading the newly created tables.
				self.connection.rollback()
				self.initialize_control()
				self.owner._write_journal(self.paths, "target_initialized", engine_version=ENGINE_VERSION, export=export)
				specs = self.specifications()
				for spec in specs[:2]:
					self.copy(spec)
				self.prepare_sessions()
				for spec in specs[2:]:
					self.copy(spec)
				self.completion()
			else:
				if not self.has_control():
					raise MigrationError("No SQL migration checkpoint; run apply first")
				self.initialize_control()
				if not self.scalar(f"SELECT COUNT(*) FROM {self.target(STEPS)} WHERE step='completion:sessions' AND complete=1"):
					raise MigrationError("Copy/completion steps are unfinished; rerun apply first")
				self.prepare_sessions()
			phase = self.scalar(f"SELECT phase FROM {self.target(RUN)} WHERE id=1")
			if operation in {"validate", "cutover"} and phase != "validating":
				# Start a fresh validation generation exactly once. Interrupted validates
				# retain all completed range checkpoints on the next invocation.
				self.execute(f"DELETE FROM {self.target(STEPS)} WHERE step LIKE 'validate:%%'")
			self.execute(f"UPDATE {self.target(RUN)} SET phase='validating' WHERE id=1")
			self.connection.commit()
			validation = self.validate_all()
			already_cutover = bool(self.scalar(f"SELECT COUNT(*) FROM {self.target(STEPS)} WHERE step='cutover:confirmed' AND complete=1"))
			final_phase = "cutover" if operation == "cutover" or already_cutover else "validated"
			if operation == "cutover":
				self.checkpoint("cutover:confirmed")
				self.save("cutover:confirmed", [], 0, True)
			self.execute(f"UPDATE {self.target(RUN)} SET phase=%s WHERE id=1", (final_phase,))
			self.connection.commit()
			self.execute(f"DROP TABLE IF EXISTS {self.target(SESSIONS)}")
			if operation == "cutover":
				from stockguidance.market_data.atomic import atomic_write
				marker = {"schema_version": 1, "operation": "simplified_database_cutover", "migration_id": self.owner._migration_id,
					"active_database": self.owner.target_database, "legacy_database": self.owner.legacy_database,
					"target_schema": {"name": "stockguidance_simplified", "version": 1, "registry_digest": registry_digest(self.names)},
					"operator_confirmation": confirmation, "created_at": datetime.now(timezone.utc).isoformat()}
				atomic_write(self.paths.export_dir / "cutover-marker.json", _json(marker).encode("utf-8"))
			result: dict[str, object] = self.owner._write_journal(self.paths, final_phase, engine_version=ENGINE_VERSION, validation=validation,
				config_action="Only after acceptance checks, point active dbinfo at the validated target; this tool does not edit active configuration.")
			return result


def status_counts(value: object) -> dict[str, int]:
	try:
		decoded = json.loads(str(value))
	except (ValueError, TypeError):
		decoded = None
	if isinstance(decoded, dict):
		return {str(k): int(v) for k, v in decoded.items()}
	return {str(int(k)): int(v) for k, v in (p.split(":", 1) for p in str(value).split(",") if p)}


def numeric_difference(left: str, right: str) -> str:
	return f"(({left} IS NULL) <> ({right} IS NULL) OR ({left} IS NOT NULL AND {right} IS NOT NULL AND ABS({left}-{right})>GREATEST(1e-12,1e-10*GREATEST(ABS({left}),ABS({right})))))"


def metadata_equal(fields: Sequence[str], expected: Sequence[object], actual: Sequence[object]) -> bool:
	json_fields = {"original_values", "effective_values", "invalid_fields", "endpoint_values", "status_counts"}
	for name, left, right in zip(fields, expected, actual):
		if name in json_fields:
			left = None if left is None else json.loads(str(left))
			right = None if right is None else json.loads(str(right))
		if left != right:
			return False
	return len(expected) == len(actual)
