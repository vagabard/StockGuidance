from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import traceback
from time import perf_counter

from stockguidance.database.table_names import quote_identifier
from stockguidance.legacy_migration.precision import ExactArchiveBar, exact_records_id, read_exact_archive
from stockguidance.market_data.identity import content_id
from stockguidance.performance.candidates import _file_hash, _manifest_table_names
from stockguidance.performance.mysql_candidates import MySQLPythonBaseline, MySQLTarget, load_manifest


TRIAL_RECEIPT = "2026-10-20T00:00:00+00:00"
STAGE_TABLE = "stage8a_raw_stage"
ROUTINE_NAME = "stage8a_adopt_raw"
CHECKPOINT_SCHEMA_VERSION = 1


def _precision_columns(manifest: dict) -> dict[str, str]:
	precision = json.loads(Path(manifest["inputs"]["precision_report"]).read_text(encoding="utf-8"))["schema_selection"]
	return {name: precision["columns"][name] for name in ("open", "high", "low", "close", "volume", "vwap")}


def _decimal_shape(value: Decimal) -> tuple[int, int]:
	digits = value.as_tuple().digits
	exponent = value.as_tuple().exponent
	if value == 0:
		return 1, max(0, -exponent)
	return max(1, len(digits) + exponent), max(0, -exponent)


def _fits_decimal(value: Decimal | None, definition: str) -> bool:
	if value is None:
		return True
	precision, scale = (int(part) for part in definition.removeprefix("DECIMAL(").removesuffix(")").split(","))
	integer_digits, fractional_digits = _decimal_shape(value)
	return integer_digits <= precision - scale and fractional_digits <= scale


def _stage_table_statement(manifest: dict) -> str:
	columns = _precision_columns(manifest)
	stage = quote_identifier(STAGE_TABLE)
	return (
		f"CREATE TABLE {stage} ("
		"session_date DATE NOT NULL, ticker_label VARCHAR(16) COLLATE utf8mb4_bin NOT NULL, "
		f"`open` {columns['open']} NOT NULL, `high` {columns['high']} NOT NULL, "
		f"`low` {columns['low']} NOT NULL, `close` {columns['close']} NOT NULL, "
		f"volume {columns['volume']} NOT NULL, vwap {columns['vwap']} NULL, "
		"transactions BIGINT NULL, file_sha256 CHAR(64) NOT NULL, records_id CHAR(64) NOT NULL, "
		"PRIMARY KEY (session_date, ticker_label), KEY stage8a_raw_stage_ticker (ticker_label)"
		") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"
	)


def _routine_statement(manifest: dict) -> str:
	names = _manifest_table_names(manifest)
	stage = quote_identifier(STAGE_TABLE)
	tickers = quote_identifier(names.tickers)
	sessions = quote_identifier(names.sessions)
	prices = quote_identifier(names.prices)
	imports = quote_identifier(names.imports)
	completion = quote_identifier("stage6_completion")
	routine = quote_identifier(ROUTINE_NAME)
	return f"""CREATE PROCEDURE {routine}(IN p_session_date DATE)
SQL SECURITY INVOKER
MODIFIES SQL DATA
BEGIN
 DECLARE v_session_count BIGINT DEFAULT 0;
 DECLARE v_date_id BIGINT DEFAULT NULL;
 DECLARE v_stage_count BIGINT DEFAULT 0;
 DECLARE v_records_count BIGINT DEFAULT 0;
 SELECT COUNT(*), MIN(id) INTO v_session_count, v_date_id FROM {sessions} WHERE transaction_date = p_session_date;
 IF v_session_count > 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8a session mapping is not unique';
 END IF;
 IF v_session_count = 0 THEN
  INSERT INTO {sessions} (transaction_date)
  SELECT p_session_date WHERE NOT EXISTS (SELECT 1 FROM {sessions} WHERE transaction_date = p_session_date);
 END IF;
 SELECT COUNT(*), MIN(id) INTO v_session_count, v_date_id FROM {sessions} WHERE transaction_date = p_session_date;
 IF v_session_count <> 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8a session mapping is unavailable';
 END IF;
 SELECT COUNT(*), COUNT(DISTINCT records_id) INTO v_stage_count, v_records_count FROM {stage} WHERE session_date = p_session_date;
 IF v_stage_count = 0 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8a staging session is empty';
 END IF;
 IF v_records_count <> 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8a staging records identity is inconsistent';
 END IF;
 INSERT INTO {tickers} (id, label)
 SELECT ids.max_id + candidates.ordinal, candidates.ticker_label
 FROM (SELECT COALESCE(MAX(id), 0) AS max_id FROM {tickers}) ids
 JOIN (
  SELECT s.ticker_label, ROW_NUMBER() OVER (ORDER BY s.ticker_label) AS ordinal
  FROM {stage} s
  WHERE s.session_date = p_session_date
    AND NOT EXISTS (SELECT 1 FROM {tickers} t WHERE t.label = s.ticker_label)
  GROUP BY s.ticker_label
 ) candidates ON 1 = 1;
 IF (SELECT COUNT(*) FROM {stage} s LEFT JOIN {tickers} t ON t.label = s.ticker_label WHERE s.session_date = p_session_date AND t.id IS NULL) <> 0 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8a ticker mapping is unavailable';
 END IF;
 INSERT INTO {prices} (ticker_id, date_id, `open`, `high`, `low`, `close`, volume, vwap, transactions)
 SELECT t.id, v_date_id, s.`open`, s.`high`, s.`low`, s.`close`, s.volume, s.vwap, s.transactions
 FROM {stage} s JOIN {tickers} t ON t.label = s.ticker_label
 WHERE s.session_date = p_session_date
 ON DUPLICATE KEY UPDATE
  `open` = VALUES(`open`), `high` = VALUES(`high`), `low` = VALUES(`low`), `close` = VALUES(`close`),
  volume = VALUES(volume), vwap = VALUES(vwap), transactions = VALUES(transactions);
 INSERT INTO {imports} (session_date, raw_id, available_at, derived_dependency_id, derived_content_id)
 SELECT p_session_date, MAX(records_id), '{TRIAL_RECEIPT}', NULL, NULL FROM {stage} WHERE session_date = p_session_date
 ON DUPLICATE KEY UPDATE raw_id = VALUES(raw_id), available_at = VALUES(available_at);
 INSERT INTO {completion} (workload, session_date, series_key, content_digest, committed_at)
 SELECT 'raw', p_session_date, 'raw', MAX(records_id), '{TRIAL_RECEIPT}' FROM {stage} WHERE session_date = p_session_date
 ON DUPLICATE KEY UPDATE content_digest = VALUES(content_digest), committed_at = VALUES(committed_at);
END"""


def sql_raw_schema_statements(manifest: dict) -> tuple[str, ...]:
	names = _manifest_table_names(manifest)
	if STAGE_TABLE.casefold() in {name.casefold() for name in names.tables().values()}:
		raise ValueError("Stage 8a staging table collides with the selected registry")
	return (_stage_table_statement(manifest),)


def sql_raw_objects(manifest: dict) -> tuple[str, ...]:
	return (_routine_statement(manifest),)


class SQLRawTarget(MySQLTarget):
	def __init__(self, dbinfo_file: Path, schema: str, manifest: dict) -> None:
		super().__init__(dbinfo_file, schema, manifest, extra_statements=sql_raw_schema_statements(manifest), extra_objects=sql_raw_objects(manifest))


class SQLRawCandidate:
	def __init__(self, manifest: dict, target: SQLRawTarget, output: Path) -> None:
		self.manifest = manifest
		self.target = target
		self.output = output
		self.names = _manifest_table_names(manifest)
		self.columns = _precision_columns(manifest)

	def _archive(self, session: str) -> tuple[tuple[ExactArchiveBar, ...], str, dict]:
		item = next((value for value in self.manifest["files"] if value.get("session") == session), None)
		if not isinstance(item, dict) or not item.get("valid") or not item.get("path") or not item.get("file_sha256") or not item.get("records_id"):
			raise ValueError(f"Stage 8a source session is unavailable: {session}")
		path = Path(item["path"])
		if _file_hash(path) != item["file_sha256"]:
			raise ValueError(f"Changed Stage 6 input: {session}")
		bars, file_hash = read_exact_archive(path)
		if file_hash != item["file_sha256"] or exact_records_id(bars) != item["records_id"]:
			raise ValueError(f"Changed Stage 6 records identity: {session}")
		for bar in bars:
			for field in ("open", "high", "low", "close", "volume", "vwap"):
				if not _fits_decimal(getattr(bar, field), self.columns[field]):
					raise ValueError(f"Archive {field} does not fit corrected SQL precision: {session}")
		return bars, file_hash, item

	def _checkpoint_path(self, checkpoint: Path | None) -> Path:
		return checkpoint or self.output.with_name(f"{self.output.stem}-checkpoint.json")

	def _validate_checkpoint(self, checkpoint: Path | None, session: str, records_id: str) -> None:
		if checkpoint is None or not checkpoint.exists():
			return
		try:
			value = json.loads(checkpoint.read_text(encoding="utf-8"))
		except (OSError, UnicodeError, json.JSONDecodeError):
			raise ValueError("Invalid Stage 8a checkpoint") from None
		if value != {"schema_version": CHECKPOINT_SCHEMA_VERSION, "workload": "raw", "session": session, "content_digest": records_id, "after_commit": True}:
			raise ValueError("Stale Stage 8a checkpoint")

	def _write_checkpoint(self, checkpoint: Path | None, session: str, records_id: str) -> None:
		path = self._checkpoint_path(checkpoint)
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(json.dumps({"schema_version": CHECKPOINT_SCHEMA_VERSION, "workload": "raw", "session": session, "content_digest": records_id, "after_commit": True}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	def _execute(self, cursor, statement: str, parameters=()) -> None:
		cursor.execute(statement, parameters)
		while cursor.nextset():
			if cursor.with_rows:
				cursor.fetchall()

	def _load_stage(self, cursor, session: str, bars: tuple[ExactArchiveBar, ...], file_hash: str, records_id: str) -> int:
		stage = quote_identifier(STAGE_TABLE)
		cursor.execute(f"DELETE FROM {stage} WHERE session_date = %s", (session,))
		rows = [(
			session, bar.ticker, *(None if getattr(bar, field) is None else format(getattr(bar, field), "f") for field in ("open", "high", "low", "close", "volume", "vwap")),
			bar.transactions, file_hash, records_id,
		) for bar in bars]
		cursor.executemany(f"INSERT INTO {stage} (session_date, ticker_label, `open`, `high`, `low`, `close`, volume, vwap, transactions, file_sha256, records_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)
		return len(rows)

	def _difference_count(self, cursor, session: str) -> int:
		stage = quote_identifier(STAGE_TABLE)
		prices = quote_identifier(self.names.prices)
		tickers = quote_identifier(self.names.tickers)
		sessions = quote_identifier(self.names.sessions)
		cursor.execute(f"""SELECT COUNT(*) FROM {stage} s
LEFT JOIN {sessions} d ON d.transaction_date = s.session_date
LEFT JOIN {tickers} t ON t.label = s.ticker_label
LEFT JOIN {prices} p ON p.date_id = d.id AND p.ticker_id = t.id
WHERE s.session_date = %s AND (p.ticker_id IS NULL OR NOT (p.`open` <=> s.`open`) OR NOT (p.`high` <=> s.`high`) OR NOT (p.`low` <=> s.`low`) OR NOT (p.`close` <=> s.`close`) OR NOT (p.volume <=> s.volume) OR NOT (p.vwap <=> s.vwap) OR NOT (p.transactions <=> s.transactions))""", (session,))
		return int(cursor.fetchone()[0])

	def _mapping(self, cursor, labels: tuple[str, ...]) -> dict[str, int]:
		tickers = quote_identifier(self.names.tickers)
		if not labels:
			return {}
		placeholders = ",".join("%s" for _ in labels)
		cursor.execute(f"SELECT label, id FROM {tickers} WHERE label IN ({placeholders})", labels)
		return {str(label): int(identifier) for label, identifier in cursor.fetchall()}

	def _call(self, cursor, session: str) -> None:
		self._execute(cursor, f"CALL {quote_identifier(ROUTINE_NAME)}(%s)", (session,))

	def write_raw(self, session: str, *, checkpoint: Path | None = None, interruption: str | None = None) -> dict[str, object]:
		bars, file_hash, item = self._archive(session)
		records_id = str(item["records_id"])
		self._validate_checkpoint(checkpoint, session, records_id)
		connection = self.target.connect()
		cursor = connection.cursor()
		committed = False
		started = perf_counter()
		try:
			self.target.validate(connection)
			connection.autocommit = False
			loader_started = perf_counter()
			loaded = self._load_stage(cursor, session, bars, file_hash, records_id)
			loader_seconds = perf_counter() - loader_started
			before_difference = self._difference_count(cursor, session)
			labels = tuple(bar.ticker for bar in bars)
			before_mapping = self._mapping(cursor, labels)
			self._call(cursor, session)
			after_difference = self._difference_count(cursor, session)
			if after_difference != 0:
				raise ValueError("SQL raw adoption did not reconcile the staged session")
			if interruption == "before_commit":
				raise RuntimeError("Injected interruption before commit")
			connection.commit()
			committed = True
			if interruption == "after_commit":
				raise RuntimeError("Injected interruption after commit")
			self._write_checkpoint(checkpoint, session, records_id)
			connection.autocommit = True
			return {"session": session, "rows": len(bars), "loaded_rows": loaded, "loader_seconds": loader_seconds, "loader_batch_size": loaded, "changed_rows": after_difference if before_difference == 0 else before_difference, "before_difference": before_difference, "file_sha256": file_hash, "records_id": records_id, "new_ticker_count": len(self._mapping(cursor, labels)) - len(before_mapping), "seconds": perf_counter() - started, "committed": True}
		except Exception:
			if not committed:
				connection.rollback()
			raise
		finally:
			cursor.close()
			connection.close()

	def query(self, sql: str, parameters=(), *, write: bool = False):
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor()
			cursor.execute(sql, parameters)
			if write:
				return cursor.rowcount
			return cursor.fetchall()
		finally:
			connection.close()

	def explain(self, session: str) -> list[dict[str, object]]:
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor(dictionary=True)
			stage = quote_identifier(STAGE_TABLE)
			tickers = quote_identifier(self.names.tickers)
			cursor.execute(f"EXPLAIN SELECT s.ticker_label, t.id FROM {stage} s LEFT JOIN {tickers} t ON t.label = s.ticker_label WHERE s.session_date = %s", (session,))
			return [{str(key): (None if value is None else str(value)) for key, value in row.items()} for row in cursor.fetchall()]
		finally:
			connection.close()

	def snapshot(self) -> str:
		values = []
		for table in (self.names.prices, self.names.tickers, self.names.sessions, self.names.imports, "stage6_completion", STAGE_TABLE):
			rows = self.query(f"SELECT * FROM {quote_identifier(table)} ORDER BY 1, 2")
			values.append([[None if value is None else str(value) for value in row] for row in rows])
		return content_id(values)

def _fixture_manifest(manifest: dict, directory: Path) -> Path:
	directory.mkdir(parents=True, exist_ok=True)
	fixture = deepcopy(manifest)
	fixture["files"] = []
	for index, session in enumerate(("2024-07-01", "2024-07-02", "2024-07-03")):
		timestamp = int(datetime.fromisoformat(session).replace(tzinfo=timezone.utc).timestamp() * 1000) + 16 * 3600000
		path = directory / f"{session}.ndjson"
		path.write_text(
			'{"T":"AAP","o":1702500000000000.0,"h":1702500000000000.1,"l":1702500000000000.0,"c":1702500000000000.1,"v":61812.9999,"vw":null,"n":10,"t":' + str(timestamp) + '}\n'
			+ '{"T":"AAp","o":10,"h":12,"l":9,"c":11,"v":14982.4,"vw":10.5,"n":null,"t":' + str(timestamp) + '}\n', encoding="utf-8")
		bars, file_hash = read_exact_archive(path)
		fixture["files"].append({"session": session, "path": str(path.resolve()), "valid": True, "file_sha256": file_hash, "records_id": exact_records_id(bars)})
	fixture["targets"] = [{"session": "2024-07-02", "derived_dependencies": ["2024-07-01", "2024-07-03"], "roles": ["fixture"]}]
	fixture["mappings"] = {"ticker_mappings": [{"id": 11, "label": "AAP"}, {"id": 13, "label": "AAp"}], "date_mappings": [{"id": 1001 + index, "session": session} for index, session in enumerate(("2024-07-01", "2024-07-02", "2024-07-03"))]}
	fixture.pop("manifest_id", None)
	fixture["manifest_id"] = content_id(fixture)
	path = directory / "manifest.json"
	path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return path


class SQLRawTrial:
	def __init__(self, manifest_path: Path, dbinfo_file: Path, schema: str, directory: Path) -> None:
		self.manifest_path = manifest_path
		self.dbinfo_file = dbinfo_file
		self.directory = directory
		self.manifest = load_manifest(manifest_path)
		self.target = SQLRawTarget(dbinfo_file, schema, self.manifest)
		self.schema = self.target.prepare()
		self.candidate = SQLRawCandidate(self.manifest, self.target, directory / "sql-raw-reference.json")

	def check_raw(self, session: str) -> int:
		bars, _, _ = self.candidate._archive(session)
		sessions = quote_identifier(self.candidate.names.sessions)
		tickers = quote_identifier(self.candidate.names.tickers)
		prices = quote_identifier(self.candidate.names.prices)
		rows = self.candidate.query(f"SELECT t.label, p.`open`, p.`high`, p.`low`, p.`close`, p.volume, p.vwap, p.transactions FROM {prices} p JOIN {tickers} t ON t.id = p.ticker_id JOIN {sessions} s ON s.id = p.date_id WHERE s.transaction_date = %s", (session,))
		actual = {row[0]: row[1:] for row in rows}
		if len(actual) != len(bars):
			raise AssertionError("SQL raw row count differs from archive")
		for bar in bars:
			if actual.get(bar.ticker) != (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions):
				raise AssertionError(f"SQL raw value differs for {bar.ticker}")
		return len(bars)

	def identity(self) -> dict[str, object]:
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor()
			cursor.execute("SELECT ROUTINE_NAME, ROUTINE_DEFINITION, SQL_DATA_ACCESS, SECURITY_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s AND ROUTINE_NAME = %s", (self.target.schema, ROUTINE_NAME))
			row = cursor.fetchone()
			if row is None:
				raise ValueError("Stage 8a SQL routine is missing")
			return {"manifest_id": self.manifest["manifest_id"], "registry": self.manifest["authoritative_registry"], "corrected_schema_hash": self.manifest["corrected_schema_hash"], "schema_hash": self.schema["schema_hash"], "objects_hash": self.target.objects_fingerprint(connection), "staging_table": STAGE_TABLE, "routine": {"name": row[0], "definition_hash": content_id(row[1]), "data_access": row[2], "security_type": row[3]}}
		finally:
			connection.close()

	def recovery(self) -> list[str]:
		checks = []
		session = "2024-07-02"
		checkpoint = self.directory / "raw-checkpoint.json"
		before = self.candidate.snapshot()
		try:
			self.candidate.write_raw(session, checkpoint=checkpoint, interruption="before_commit")
		except RuntimeError as error:
			if str(error) != "Injected interruption before commit":
				raise
		if self.candidate.snapshot() != before or checkpoint.exists():
			raise AssertionError("SQL raw before-commit rollback failed")
		checks.append("raw_before_commit_rollback")
		try:
			self.candidate.write_raw(session, checkpoint=checkpoint, interruption="after_commit")
		except RuntimeError as error:
			if str(error) != "Injected interruption after commit":
				raise
		self.check_raw(session)
		if checkpoint.exists():
			raise AssertionError("SQL raw checkpoint was written after injected interruption")
		checks.append("raw_after_commit_durable_without_checkpoint")
		result = self.candidate.write_raw(session, checkpoint=checkpoint)
		if result["changed_rows"] != 0 or not checkpoint.exists():
			raise AssertionError("SQL raw restart was not idempotent")
		checks.append("raw_restart_idempotent")
		names = self.candidate.names
		prices = quote_identifier(names.prices)
		sessions = quote_identifier(names.sessions)
		tickers = quote_identifier(names.tickers)
		date_id = self.candidate.query(f"SELECT id FROM {sessions} WHERE transaction_date = %s", (session,))[0][0]
		first = self.candidate.query(f"SELECT ticker_id FROM {prices} WHERE date_id = %s ORDER BY ticker_id LIMIT 1", (date_id,))[0][0]
		second = self.candidate.query(f"SELECT ticker_id FROM {prices} WHERE date_id = %s AND ticker_id <> %s ORDER BY ticker_id LIMIT 1", (date_id, first))[0][0]
		self.candidate.query(f"DELETE FROM {prices} WHERE date_id = %s AND ticker_id = %s", (date_id, first), write=True)
		self.candidate.query(f"UPDATE {prices} SET `close` = 10 WHERE date_id = %s AND ticker_id = %s", (date_id, second), write=True)
		result = self.candidate.write_raw(session)
		self.check_raw(session)
		if result["changed_rows"] != 2:
			raise AssertionError("SQL raw repair count did not identify the differing row")
		checks.append("missing_and_different_raw_repair_exact_decimals_and_case")
		imports = quote_identifier(names.imports)
		self.candidate.query(f"UPDATE {imports} SET derived_dependency_id = 'dependency-sentinel', derived_content_id = 'content-sentinel' WHERE session_date = %s", (session,), write=True)
		self.candidate.write_raw(session)
		if self.candidate.query(f"SELECT derived_dependency_id, derived_content_id FROM {imports} WHERE session_date = %s", (session,)) != [("dependency-sentinel", "content-sentinel")]:
			raise AssertionError("SQL raw adoption did not preserve derived metadata")
		checks.append("matching_adoption_preserves_derived_metadata")
		archive = next(Path(item["path"]) for item in self.manifest["files"] if item["session"] == session)
		original = archive.read_bytes()
		try:
			archive.write_bytes(original.replace(b"14982.4", b"14982.5"))
			before = self.candidate.snapshot()
			try:
				self.candidate.write_raw(session)
			except ValueError as error:
				if str(error) != f"Changed Stage 6 input: {session}":
					raise
			if self.candidate.snapshot() != before:
				raise AssertionError("Changed source altered SQL target")
		finally:
			archive.write_bytes(original)
		checks.append("changed_input_rejected")
		stale = self.directory / "stale-checkpoint.json"
		stale.write_text('{"schema_version":1,"manifest_id":"stale"}', encoding="utf-8")
		before = self.candidate.snapshot()
		try:
			self.candidate.write_raw(session, checkpoint=stale)
		except ValueError as error:
			if str(error) != "Stale Stage 8a checkpoint":
				raise
		if self.candidate.snapshot() != before:
			raise AssertionError("Stale checkpoint altered SQL target")
		checks.append("stale_checkpoint_rejected")
		return checks


def run_trial(manifest_path: Path, dbinfo_file: Path, schema: str, output: Path) -> dict:
	manifest = load_manifest(manifest_path)
	output.parent.mkdir(parents=True, exist_ok=True)
	repository = Path(__file__).resolve().parents[3]
	code_files = [Path(__file__), Path(__file__).with_name("mysql_candidates.py"), Path(__file__).with_name("candidates.py")]
	report = {"schema_version": 1, "stage": "8a", "backend": "sql", "storage_engine": "mysql", "status": "incomplete", "manifest_id": manifest["manifest_id"], "recovery_checks": [], "sessions": [], "comparison_eligible": False, "production_access": {"ddl": False, "dml": False}, "protocol": {"writers": 1, "preparers": 1, "queued_sessions_max": 3, "loader": "validated parameterized executemany; no file-loading privilege assumed", "transaction_unit": "one session; stored procedure does not commit", "timings": "correctness trial; Stage 9 supplies startup-inclusive measurements"}, "code_hashes": {str(path.relative_to(repository)): _file_hash(path) for path in code_files}}
	def save() -> None:
		output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	try:
		fixture_path = _fixture_manifest(manifest, output.parent / f"{schema}_fixtures")
		fixture = SQLRawTrial(fixture_path, dbinfo_file, schema + "_fx", fixture_path.parent)
		report["fixture_schema"] = fixture.schema
		report["identity"] = fixture.identity()
		report["fixture_explain"] = fixture.candidate.explain("2024-07-02")
		report["recovery_checks"] = fixture.recovery()
		sql_trial = SQLRawTrial(manifest_path, dbinfo_file, schema, output.parent)
		report["schema"] = sql_trial.schema
		report["identity"] = sql_trial.identity()
		report["explain"] = sql_trial.candidate.explain(manifest["target_blocks"]["fixed_25"][0])
		python = MySQLPythonBaseline(manifest, sql_trial.target, output.parent / "sql-raw-python-seed.json")
		for target in manifest["target_blocks"]["fixed_25"]:
			python._raw_session(target, seed=True)
			result = sql_trial.candidate.write_raw(target)
			rows = sql_trial.check_raw(target)
			if result["changed_rows"] != 0:
				raise AssertionError(f"SQL matching adoption changed rows for {target}")
			report["sessions"].append({"session": target, "raw_rows": rows, "matching_changed_rows": result["changed_rows"], "loaded_rows": result["loaded_rows"], "loader_seconds": result["loader_seconds"], "loader_batch_size": result["loader_batch_size"]})
			save()
		report["status"] = "complete"
		save()
	except Exception as error:
		report["status"] = "failed"
		report["failure_type"] = type(error).__name__
		report["failure_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
		save()
		raise RuntimeError("Disposable MySQL SQL raw trial failed; inspect its sanitized report") from None
	return report
