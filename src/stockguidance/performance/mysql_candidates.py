from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import re
from time import perf_counter

from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.identity import content_id
from stockguidance.performance.candidates import PythonBaseline, _file_hash, _json, _manifest_table_names, disposable_schema_statements


DISPOSABLE_SCHEMA = re.compile(r"stockguidance_disposable_[a-z0-9_]{1,39}\Z")


def validate_schema(schema: str, production_schema: str) -> str:
	if not DISPOSABLE_SCHEMA.fullmatch(schema) or schema.casefold() == production_schema.casefold():
		raise ValueError("Require a distinct stockguidance_disposable_ MySQL schema")
	return schema


def load_manifest(path: Path) -> dict:
	manifest = _json(path)
	unsigned = {key: value for key, value in manifest.items() if key != "manifest_id"}
	if manifest.get("stage") != 6 or manifest.get("schema_version") != 1 or content_id(unsigned) != manifest.get("manifest_id"):
		raise ValueError("Invalid candidate manifest identity")
	if _file_hash(Path(manifest["inputs"]["audit_report"])) != manifest["snapshot_identity"]["audit_sha256"]:
		raise ValueError("Changed audit identity")
	return manifest


def mysql_schema_statements(manifest: dict) -> tuple[str, ...]:
	names = _manifest_table_names(manifest)
	precision = _json(Path(manifest["inputs"]["precision_report"]))["schema_selection"]
	if precision["schema_hash"] != manifest["corrected_schema_hash"] or content_id(precision["ddl"]) != precision["schema_hash"]:
		raise ValueError("Changed corrected precision schema")
	if set(precision["columns"]) != {"open", "high", "low", "close", "volume", "vwap"} or any(precision["ddl"].get(column) != definition for column, definition in precision["columns"].items()):
		raise ValueError("Inconsistent corrected precision columns")
	statements = []
	for statement in disposable_schema_statements(names):
		if statement.startswith("CREATE INDEX"):
			statements.append(statement)
			continue
		statement = statement.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT PRIMARY KEY AUTO_INCREMENT")
		statement = statement.replace("INTEGER", "BIGINT").replace("REAL", "DOUBLE")
		statement = statement.replace("label TEXT NOT NULL COLLATE BINARY UNIQUE", "label VARCHAR(16) COLLATE utf8mb4_bin NOT NULL UNIQUE")
		if statement.startswith(f"CREATE TABLE {quote_identifier(names.prices)} "):
			for column, definition in precision["columns"].items():
				if re.fullmatch(r"DECIMAL\(\d+,\d+\)", definition) is None:
					raise ValueError("Invalid precision definition")
				statement = statement.replace(f"{column} TEXT", f"{column} {definition}")
		statement = statement.replace("session_date TEXT", "session_date DATE").replace("transaction_date TEXT", "transaction_date DATE")
		statement = statement.replace("TEXT", "VARCHAR(255)")
		statement = statement.replace("value VARCHAR(255)", "value LONGTEXT")
		statement = re.sub(r"\b(key|window|precision)\b", r"`\1`", statement)
		if "(date_id BIGINT NOT NULL, ticker_id BIGINT NOT NULL, zscore DOUBLE" in statement:
			statement = statement.replace("zscore DOUBLE", "zscore FLOAT")
		statements.append(statement + " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin")
	return tuple(statements)


class MySQLTarget:
	def __init__(self, dbinfo_file: Path, schema: str, manifest: dict, *, extra_statements: tuple[str, ...] = (), extra_objects: tuple[str, ...] = ()) -> None:
		production = load_database_configuration(dbinfo_file)
		self.schema = validate_schema(schema, production.database)
		self.configuration = replace(production, database=self.schema)
		self.production_schema = production.database
		self.manifest = manifest
		self.statements = mysql_schema_statements(manifest) + tuple(extra_statements)
		self.extra_objects = tuple(extra_objects)
		self.metadata = {
			"production_target": "false", "database_engine": "mysql", "database_schema": self.schema,
			"schema_hash": content_id(self.statements), "corrected_schema_hash": manifest["corrected_schema_hash"],
			"registry": json.dumps(_manifest_table_names(manifest).tables(), sort_keys=True),
			"candidate_manifest_id": manifest["manifest_id"],
		}

	def connect(self, *, select_schema: bool = True):
		import mysql.connector

		configuration = self.configuration
		connection = mysql.connector.connect(host=configuration.host, port=configuration.port, user=configuration.user,
			password=configuration.password, database=self.schema if select_schema else None,
			connection_timeout=10, autocommit=True)
		return connection

	def prepare(self) -> dict:
		connection = self.connect(select_schema=False)
		try:
			cursor = connection.cursor()
			objects, production_inventory = self.validation_objects(cursor)
			objects += self.extra_objects
			cursor.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (self.schema,))
			if cursor.fetchone() is None:
				cursor.execute(f"CREATE DATABASE {quote_identifier(self.schema)} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
				connection.database = self.schema
				for statement in self.statements:
					cursor.execute(statement)
				for statement in objects:
					cursor.execute(statement)
				cursor.executemany("INSERT INTO stage6_disposable_metadata (`key`, value) VALUES (%s, %s)", list(self.metadata.items()))
				cursor.executemany(f"INSERT INTO {quote_identifier(_manifest_table_names(self.manifest).tickers)} (id, label) VALUES (%s, %s)",
					[(item["id"], item["label"]) for item in self.manifest["mappings"].get("ticker_mappings", [])])
				cursor.executemany(f"INSERT INTO {quote_identifier(_manifest_table_names(self.manifest).sessions)} (id, transaction_date) VALUES (%s, %s)",
					[(item["id"], item["session"]) for item in self.manifest["mappings"].get("date_mappings", [])])
				cursor.execute("INSERT INTO stage6_disposable_metadata (`key`, value) VALUES (%s, %s)", ("actual_schema_hash", self.schema_fingerprint(connection)))
				cursor.execute("INSERT INTO stage6_disposable_metadata (`key`, value) VALUES (%s, %s)", ("actual_objects_hash", self.objects_fingerprint(connection)))
			else:
				connection.database = self.schema
			self.validate(connection)
			cursor.execute("SELECT VERSION(), @@sql_mode, @@default_storage_engine")
			version, sql_mode, engine = cursor.fetchone()
			return {"engine": "mysql", "schema": self.schema, "version": version, "sql_mode": sql_mode,
				"storage_engine": engine, "schema_hash": self.metadata["schema_hash"], "production_target": False,
				"production_object_inventory": production_inventory, "validation_object_count": len(objects)}
		finally:
			connection.close()

	def validate(self, connection) -> None:
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT DATABASE()")
			if cursor.fetchone()[0] != self.schema:
				raise ValueError("Unexpected candidate database")
			cursor.execute("SELECT `key`, value FROM stage6_disposable_metadata")
			metadata = dict(cursor.fetchall())
			if any(metadata.get(key) != value for key, value in self.metadata.items()):
				raise ValueError("Disposable MySQL target identity mismatch")
			cursor.execute("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (self.schema,))
			tables = cursor.fetchall()
			if not tables or any(engine != "InnoDB" for _, engine in tables):
				raise ValueError("Candidate tables must be InnoDB")
			if metadata.get("actual_schema_hash") != self.schema_fingerprint(connection):
				raise ValueError("Disposable MySQL DDL changed")
			if metadata.get("actual_objects_hash") != self.objects_fingerprint(connection):
				raise ValueError("Disposable MySQL routines or triggers changed")
		finally:
			cursor.close()

	def validation_objects(self, cursor) -> tuple[tuple[str, ...], dict]:
		names = _manifest_table_names(self.manifest)
		cursor.execute("SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s ORDER BY TRIGGER_NAME", (self.production_schema,))
		triggers = cursor.fetchall()
		cursor.execute("SELECT ROUTINE_NAME, ROUTINE_DEFINITION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME", (self.production_schema,))
		routines = cursor.fetchall()
		inventory = {"triggers": [{"name": row[0], "table": row[1], "definition_hash": content_id(row[4])} for row in triggers],
			"routines": [{"name": row[0], "definition_hash": content_id(row[1]), "invoked_by_trial": row[0] == "check_ticker_exists"} for row in routines]}
		applicable = [row for row in triggers if row[1] in names.tables().values()]
		if not applicable:
			return (), inventory
		for name, table, timing, event, body in applicable:
			body = re.sub(r"--[^\n]*", "", body)
			if not re.fullmatch(r"\s*BEGIN\s+CALL\s+check_ticker_exists\s*\(NEW.ticker_id\);\s*END\s*", body, re.IGNORECASE) or table not in (names.prices, names.deltas) or timing != "BEFORE" or event not in ("INSERT", "UPDATE"):
				raise ValueError("Unrecognized production trigger requires equivalence review")
		routine = next((body for name, body in routines if name == "check_ticker_exists"), None)
		if routine is None:
			raise ValueError("Ticker validation routine definition unavailable")
		body = re.sub(r"--[^\n]*", "", routine)
		expected = "BEGIN DECLARE ticker_count INT; SELECT COUNT(*) INTO ticker_count FROM ticker_symbol WHERE id = p_ticker_id; IF ticker_count = 0 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'FOREIGN KEY VIOLATION: The ticker_id specified does not exist in the ticker_symbol table.'; END IF; END"
		if " ".join(body.split()) != expected:
			raise ValueError("Unrecognized ticker validation routine requires review")
		statements = [f"CREATE PROCEDURE stage6_check_ticker_exists(IN p_ticker_id INT) CONTAINS SQL BEGIN DECLARE ticker_count INT; SELECT COUNT(*) INTO ticker_count FROM {quote_identifier(names.tickers)} WHERE id = p_ticker_id; IF ticker_count = 0 THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'FOREIGN KEY VIOLATION: The ticker_id specified does not exist in the ticker_symbol table.'; END IF; END"]
		for index, (_, table, timing, event, _) in enumerate(applicable):
			statements.append(f"CREATE TRIGGER stage6_validate_ticker_{index} {timing} {event} ON {quote_identifier(table)} FOR EACH ROW CALL stage6_check_ticker_exists(NEW.ticker_id)")
		return tuple(statements), inventory

	def objects_fingerprint(self, connection) -> str:
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s ORDER BY TRIGGER_NAME", (self.schema,))
			triggers = cursor.fetchall()
			cursor.execute("SELECT ROUTINE_NAME, ROUTINE_DEFINITION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME", (self.schema,))
			return content_id([triggers, cursor.fetchall()])
		finally:
			cursor.close()

	def schema_fingerprint(self, connection) -> str:
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME", (self.schema,))
			names = [row[0] for row in cursor.fetchall()]
			definitions = []
			for name in names:
				cursor.execute(f"SHOW CREATE TABLE {quote_identifier(name)}")
				definition = cursor.fetchone()[1]
				definitions.append([name, re.sub(r" AUTO_INCREMENT=\d+", "", definition)])
			return content_id(definitions)
		finally:
			cursor.close()


def mysql_statement(statement: str) -> str:
	statement = statement.replace("?", "%s").replace(" COLLATE BINARY", " COLLATE utf8mb4_bin")
	statement = re.sub(r"\b(window|precision)\b", r"`\1`", statement)
	if statement.startswith("INSERT OR IGNORE"):
		statement = statement.replace("INSERT OR IGNORE", "INSERT", 1)
		column = statement.split("(", 1)[1].split(")", 1)[0].split(",")[0].strip()
		statement += f" ON DUPLICATE KEY UPDATE {column} = {column}"
	elif statement.startswith("INSERT OR REPLACE"):
		statement = statement.replace("INSERT OR REPLACE", "INSERT", 1)
		columns = [column.strip() for column in statement.split("(", 1)[1].split(")", 1)[0].split(",")]
		statement += " ON DUPLICATE KEY UPDATE " + ", ".join(f"{column} = VALUES({column})" for column in columns)
	return statement


class MySQLBaselineConnection:
	def __init__(self, target: MySQLTarget, trace) -> None:
		self.connection = target.connect()
		target.validate(self.connection)
		self.cursor = self.connection.cursor(buffered=True)
		self.trace = trace

	def execute(self, statement, parameters=()):
		self.trace(1)
		self.cursor.execute(mysql_statement(statement), parameters)
		return self

	def executemany(self, statement, parameters):
		parameters = list(parameters)
		self.trace(1)
		self.cursor.executemany(mysql_statement(statement), parameters)
		return self

	def _row(self, row):
		return None if row is None else tuple(format(value.normalize(), "f") if isinstance(value, Decimal) else value.isoformat() if hasattr(value, "isoformat") else value for value in row)

	def fetchone(self):
		return self._row(self.cursor.fetchone())

	def fetchall(self):
		return [self._row(row) for row in self.cursor.fetchall()]

	def commit(self):
		self.connection.commit()

	def rollback(self):
		self.connection.rollback()

	def close(self):
		self.connection.close()


class MySQLPythonBaseline(PythonBaseline):
	def __init__(self, manifest: dict, target: MySQLTarget, output: Path, **options) -> None:
		super().__init__(manifest, output, output, **options)
		self.target = target

	def _connect(self):
		return MySQLBaselineConnection(self.target, lambda count: setattr(self, "_trace_count", self._trace_count + count))

	def _bars(self, session):
		self._bars_cache.pop(session, None)
		if len(self._bars_cache) >= 3:
			self._bars_cache.clear()
		return super()._bars(session)


def run_mysql_baselines(manifest_path: Path, dbinfo_file: Path, schema: str, output: Path,
		workloads: tuple[str, ...], *, repetitions: int = 4, timeout_seconds: float = 3600.0) -> dict:
	if repetitions < 4:
		raise ValueError("Require first invocation plus three warm repeats")
	manifest = load_manifest(manifest_path)
	target = MySQLTarget(dbinfo_file, schema, manifest)
	report = {"schema_version": 1, "backend": "python", "storage_engine": "mysql", "comparison_eligible": False,
		"status": "incomplete", "manifest": {"id": manifest["manifest_id"]}, "schema": target.prepare(), "workloads": {},
		"protocol": {"writers": 1, "preparers": 1, "queued_sessions_max": 3, "native_threads": 1,
			"timeout_seconds": timeout_seconds, "timing_scope": "single-process diagnostic; Stage 9 supplies process/startup/RSS measurements",
			"query_count_scope": "submitted row statements excluding connection/guard work; server statement measurements pending Stage 9"},
		"production_access": {"ddl": False, "dml": False}}
	backend = MySQLPythonBaseline(manifest, target, output, timeout_seconds=timeout_seconds)
	output.parent.mkdir(parents=True, exist_ok=True)
	for workload in workloads:
		report["workloads"][workload] = []
		for repetition in range(repetitions):
			started = perf_counter()
			result = backend.run(workload)
			result.update(repetition=repetition, end_to_end_seconds=perf_counter() - started, cache_state="unknown")
			if result["end_to_end_seconds"] > timeout_seconds:
				raise TimeoutError("MySQL baseline exceeded trial timeout")
			report["workloads"][workload].append(result)
			output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	report["status"] = "complete"
	output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return report
