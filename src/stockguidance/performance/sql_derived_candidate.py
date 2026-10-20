from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import traceback
from time import perf_counter

from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow, derived_id
from stockguidance.performance.candidates import _exact_derived_rows, _file_hash, _manifest_table_names
from stockguidance.performance.mysql_candidates import MySQLPythonBaseline, MySQLTarget, load_manifest
from stockguidance.performance.sql_raw_candidate import (
	SQLRawCandidate,
	_fixture_manifest,
	sql_raw_objects,
	sql_raw_schema_statements,
)
from stockguidance.validation.derived import _equal


TRIAL_RECEIPT = "2026-10-20T00:00:00+00:00"
STAGE_TABLE = "stage8b_derived_stage"
ROUTINE_NAME = "stage8b_populate_derived"
SPECIFICATION = "legacy-open-to-close-v1"
CHECKPOINT_SCHEMA_VERSION = 1


def _stage_table_statement(manifest: dict) -> str:
	names = _manifest_table_names(manifest)
	if STAGE_TABLE.casefold() in {name.casefold() for name in names.tables().values()}:
		raise ValueError("Stage 8b staging table collides with the selected registry")
	return f"""CREATE TABLE {quote_identifier(STAGE_TABLE)} (
target_date DATE NOT NULL,
prior_date DATE NOT NULL,
following_date DATE NOT NULL,
target_raw_id CHAR(64) NOT NULL,
prior_raw_id CHAR(64) NOT NULL,
following_raw_id CHAR(64) NOT NULL,
derived_dependency_id CHAR(64) NOT NULL,
derived_content_id CHAR(64) NOT NULL,
expected_rows BIGINT NOT NULL,
PRIMARY KEY (target_date),
KEY stage8b_derived_dependencies (prior_date, following_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"""


def _routine_statement(manifest: dict) -> str:
	names = _manifest_table_names(manifest)
	stage = quote_identifier(STAGE_TABLE)
	sessions = quote_identifier(names.sessions)
	tickers = quote_identifier(names.tickers)
	prices = quote_identifier(names.prices)
	deltas = quote_identifier(names.deltas)
	imports = quote_identifier(names.imports)
	completion = quote_identifier("stage6_completion")
	routine = quote_identifier(ROUTINE_NAME)
	derived_columns = ", ".join(quote_identifier(column) for column in DERIVED_COLUMNS)
	updates = ", ".join(f"{quote_identifier(column)} = VALUES({quote_identifier(column)})" for column in DERIVED_COLUMNS)
	return f"""CREATE PROCEDURE {routine}(IN p_session_date DATE)
SQL SECURITY INVOKER
MODIFIES SQL DATA
BEGIN
 DECLARE v_stage_count BIGINT DEFAULT 0;
 DECLARE v_target_count BIGINT DEFAULT 0;
 DECLARE v_prior_count BIGINT DEFAULT 0;
 DECLARE v_following_count BIGINT DEFAULT 0;
 DECLARE v_import_count BIGINT DEFAULT 0;
 DECLARE v_current_rows BIGINT DEFAULT 0;
 DECLARE v_published_rows BIGINT DEFAULT 0;
 DECLARE v_expected_rows BIGINT DEFAULT 0;
 DECLARE v_target_id BIGINT DEFAULT NULL;
 DECLARE v_prior_id BIGINT DEFAULT NULL;
 DECLARE v_following_id BIGINT DEFAULT NULL;
 DECLARE v_prior_date DATE DEFAULT NULL;
 DECLARE v_following_date DATE DEFAULT NULL;
 DECLARE v_target_raw_id CHAR(64) DEFAULT NULL;
 DECLARE v_prior_raw_id CHAR(64) DEFAULT NULL;
 DECLARE v_following_raw_id CHAR(64) DEFAULT NULL;
 DECLARE v_dependency_id CHAR(64) DEFAULT NULL;
 DECLARE v_content_id CHAR(64) DEFAULT NULL;

 SELECT COUNT(*), MAX(prior_date), MAX(following_date), MAX(target_raw_id), MAX(prior_raw_id),
        MAX(following_raw_id), MAX(derived_dependency_id), MAX(derived_content_id), MAX(expected_rows)
 INTO v_stage_count, v_prior_date, v_following_date, v_target_raw_id, v_prior_raw_id,
      v_following_raw_id, v_dependency_id, v_content_id, v_expected_rows
 FROM {stage}
 WHERE target_date = p_session_date;
 IF v_stage_count <> 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8b derived staging identity is unavailable';
 END IF;

 SELECT COUNT(*), MIN(id) INTO v_target_count, v_target_id
 FROM {sessions} WHERE transaction_date = p_session_date;
 SELECT COUNT(*), MIN(id) INTO v_prior_count, v_prior_id
 FROM {sessions} WHERE transaction_date = v_prior_date;
 SELECT COUNT(*), MIN(id) INTO v_following_count, v_following_id
 FROM {sessions} WHERE transaction_date = v_following_date;
 IF v_target_count <> 1 OR v_prior_count <> 1 OR v_following_count <> 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8b exchange-session mapping is unavailable';
 END IF;

 SELECT COUNT(*) INTO v_import_count
 FROM {imports}
 WHERE (session_date = p_session_date AND raw_id = v_target_raw_id)
    OR (session_date = v_prior_date AND raw_id = v_prior_raw_id)
    OR (session_date = v_following_date AND raw_id = v_following_raw_id);
 IF v_import_count <> 3 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8b raw dependency identity is unavailable';
 END IF;

 SELECT COUNT(*) INTO v_current_rows FROM {prices} WHERE date_id = v_target_id;
 IF v_current_rows <> v_expected_rows THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8b current raw population is incomplete';
 END IF;

 DELETE FROM {deltas} WHERE date_id = v_target_id;

 INSERT INTO {deltas} (ticker_id, date_id, {derived_columns})
 WITH base_rows AS (
  SELECT c.ticker_id, t.label,
         c.`open` AS current_open, c.`high` AS current_high, c.`low` AS current_low,
         c.`close` AS current_close, c.volume AS current_volume, c.vwap AS current_vwap,
         c.transactions AS current_transactions,
         p.`open` AS prior_open, p.`high` AS prior_high, p.`low` AS prior_low,
         p.`close` AS prior_close, p.volume AS prior_volume, p.vwap AS prior_vwap,
         p.transactions AS prior_transactions,
         n.`open` AS following_open, n.`close` AS following_close
  FROM {prices} c
  JOIN {tickers} t ON t.id = c.ticker_id
  LEFT JOIN {prices} p ON p.ticker_id = c.ticker_id AND p.date_id = v_prior_id
  LEFT JOIN {prices} n ON n.ticker_id = c.ticker_id AND n.date_id = v_following_id
  WHERE c.date_id = v_target_id
 ), scored_rows AS (
  SELECT base_rows.*,
		 1e0 * current_open / NULLIF(prior_open, 0) AS open_ratio,
		 1e0 * current_high / NULLIF(prior_high, 0) AS high_ratio,
		 1e0 * current_low / NULLIF(prior_low, 0) AS low_ratio,
		 1e0 * current_close / NULLIF(prior_close, 0) AS close_ratio,
		 1e0 * current_volume / NULLIF(prior_volume, 0) AS volume_ratio,
		 1e0 * current_vwap / NULLIF(prior_vwap, 0) AS vwap_ratio,
		 1e0 * current_transactions / NULLIF(prior_transactions, 0) AS transactions_ratio,
		 1e0 * current_close / NULLIF(current_open, 0) AS current_change,
		 1e0 * following_close / NULLIF(following_open, 0) AS next_change
  FROM base_rows
 ), ranked_rows AS (
  SELECT scored_rows.*,
         ROW_NUMBER() OVER (
          ORDER BY CASE WHEN next_change IS NULL THEN 1 ELSE 0 END,
                   next_change DESC, label
         ) AS next_rank
  FROM scored_rows
 )
 SELECT ticker_id, v_target_id,
        open_ratio,
        high_ratio,
        low_ratio,
        close_ratio,
        volume_ratio,
        vwap_ratio,
        transactions_ratio,
        current_change,
        next_change,
        CASE WHEN next_change IS NULL THEN NULL ELSE next_change - 1 END,
        CASE WHEN next_change IS NULL OR next_change = -1 THEN NULL
             ELSE (CASE WHEN next_change >= 1 THEN 0.5 ELSE 0 END) + next_change / (2 * (next_change + 1))
        END,
        CASE WHEN next_change IS NULL THEN NULL WHEN next_change >= 1 THEN 1.0 ELSE 0.0 END,
        CASE WHEN next_change IS NULL THEN NULL WHEN next_rank <= 100 THEN 1.0 ELSE 0.0 END,
        CASE WHEN next_change IS NULL OR next_change = -1 THEN NULL
             ELSE 2 * ((CASE WHEN next_change >= 1 THEN 0.5 ELSE 0 END) + next_change / (2 * (next_change + 1))) - 1
        END,
        CASE WHEN next_change IS NULL THEN NULL WHEN next_rank <= 100 THEN 1.0 ELSE -1.0 END,
        CASE WHEN next_change IS NULL THEN NULL WHEN next_change >= 1 THEN 1.0 ELSE -1.0 END
 FROM ranked_rows
 ON DUPLICATE KEY UPDATE {updates};

 SELECT COUNT(*) INTO v_published_rows FROM {deltas} WHERE date_id = v_target_id;
 IF v_published_rows <> v_expected_rows THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8b derived row population is incomplete';
 END IF;

 INSERT INTO {imports} (session_date, raw_id, available_at, derived_dependency_id, derived_content_id)
 VALUES (p_session_date, v_target_raw_id, '{TRIAL_RECEIPT}', v_dependency_id, v_content_id)
 ON DUPLICATE KEY UPDATE
  raw_id = VALUES(raw_id), available_at = VALUES(available_at),
  derived_dependency_id = VALUES(derived_dependency_id), derived_content_id = VALUES(derived_content_id);
 INSERT INTO {completion} (workload, session_date, series_key, content_digest, committed_at)
 VALUES ('derived', p_session_date, '{SPECIFICATION}', v_content_id, '{TRIAL_RECEIPT}')
 ON DUPLICATE KEY UPDATE content_digest = VALUES(content_digest), committed_at = VALUES(committed_at);
END"""


def sql_derived_schema_statements(manifest: dict) -> tuple[str, ...]:
	return (_stage_table_statement(manifest),)


def sql_derived_objects(manifest: dict) -> tuple[str, ...]:
	return (_routine_statement(manifest),)


class SQLDerivedTarget(MySQLTarget):
	def __init__(self, dbinfo_file: Path, schema: str, manifest: dict) -> None:
		super().__init__(
			dbinfo_file,
			schema,
			manifest,
			extra_statements=sql_raw_schema_statements(manifest) + sql_derived_schema_statements(manifest),
			extra_objects=sql_raw_objects(manifest) + sql_derived_objects(manifest),
		)


class SQLDerivedCandidate(SQLRawCandidate):
	def __init__(self, manifest: dict, target: SQLDerivedTarget, output: Path) -> None:
		super().__init__(manifest, target, output)
		self.target = target

	def _target_dependencies(self, session: str) -> tuple[dict, str, str, tuple[object, ...]]:
		target = next((item for item in self.manifest.get("targets", ()) if item.get("session") == session), None)
		if not isinstance(target, dict):
			raise ValueError(f"Unknown derived target: {session}")
		dependencies = tuple(target.get("derived_dependencies", ()))
		if len(dependencies) != 2 or any(not isinstance(value, str) for value in dependencies):
			raise ValueError(f"Stage 8b dependencies are unavailable: {session}")
		prior, _, _ = self._archive(dependencies[0])
		current, _, _ = self._archive(session)
		following, _, _ = self._archive(dependencies[1])
		expected = _exact_derived_rows(prior, current, following)
		dependency_id = content_id({
			"prior": dependencies[0],
			"following": dependencies[1],
			"raw": [self._archive(value)[2]["records_id"] for value in dependencies],
		})
		return target, dependencies[0], dependencies[1], (expected, dependency_id)

	def _execute(self, cursor, statement: str, parameters=()) -> None:
		cursor.execute(statement, parameters)
		while cursor.nextset():
			if cursor.with_rows:
				cursor.fetchall()

	def _raw_rows(self, cursor, session: str) -> dict[str, tuple[object, ...]]:
		names = self.names
		cursor.execute(
			f"""SELECT t.label, p.`open`, p.`high`, p.`low`, p.`close`, p.volume, p.vwap, p.transactions
FROM {quote_identifier(names.prices)} p
JOIN {quote_identifier(names.tickers)} t ON t.id = p.ticker_id
JOIN {quote_identifier(names.sessions)} s ON s.id = p.date_id
WHERE s.transaction_date = %s
ORDER BY t.label""",
			(session,),
		)
		return {str(row[0]): tuple(row[1:]) for row in cursor.fetchall()}

	def _assert_raw_rows(self, cursor, session: str, expected) -> None:
		actual = self._raw_rows(cursor, session)
		required = {
			bar.ticker: (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)
			for bar in expected
		}
		def equal_value(observed, expected) -> bool:
			if observed is None or expected is None:
				return observed is expected
			return Decimal(str(observed)) == Decimal(str(expected))

		if actual.keys() != required.keys() or any(
			any(not equal_value(observed, expected) for observed, expected in zip(actual[label], required[label], strict=True))
			for label in required
		):
			raise ValueError(f"Stored raw dependency differs from validated archive: {session}")

	def _derived_rows(self, cursor, session: str) -> dict[str, tuple[object, ...]]:
		columns = ", ".join(f"d.{quote_identifier(column)}" for column in DERIVED_COLUMNS)
		cursor.execute(
			f"""SELECT t.label, {columns}
FROM {quote_identifier(self.names.deltas)} d
JOIN {quote_identifier(self.names.tickers)} t ON t.id = d.ticker_id
JOIN {quote_identifier(self.names.sessions)} s ON s.id = d.date_id
WHERE s.transaction_date = %s
ORDER BY t.label""",
			(session,),
		)
		return {str(row[0]): tuple(row[1:]) for row in cursor.fetchall()}

	def _assert_derived_rows(self, cursor, session: str, expected: tuple[LegacyDerivedRow, ...]) -> dict[str, tuple[object, ...]]:
		actual = self._derived_rows(cursor, session)
		if set(actual) != {row.ticker for row in expected}:
			raise ValueError("SQL derived ticker population differs from the reference")
		for row in expected:
			values = actual[row.ticker]
			if len(values) != len(row.values):
				raise ValueError(f"SQL derived column count differs for {row.ticker}")
			for index, (observed, required) in enumerate(zip(values, row.values, strict=True)):
				if not _equal(observed, required):
					raise ValueError(f"SQL derived value differs for {row.ticker}/{DERIVED_COLUMNS[index]}: {observed!r} != {required!r}")
		return actual

	def _checkpoint_path(self, checkpoint: Path | None) -> Path:
		return checkpoint or self.output.with_name(f"{self.output.stem}-checkpoint.json")

	def _validate_checkpoint(self, checkpoint: Path | None, session: str, content_digest: str) -> None:
		if checkpoint is None or not checkpoint.exists():
			return
		try:
			value = json.loads(checkpoint.read_text(encoding="utf-8"))
		except (OSError, UnicodeError, json.JSONDecodeError):
			raise ValueError("Invalid Stage 8b checkpoint") from None
		expected = {
			"schema_version": CHECKPOINT_SCHEMA_VERSION,
			"workload": "derived",
			"session": session,
			"content_digest": content_digest,
			"after_commit": True,
		}
		if value != expected:
			raise ValueError("Stale Stage 8b checkpoint")

	def _write_checkpoint(self, checkpoint: Path | None, session: str, content_digest: str) -> None:
		path = self._checkpoint_path(checkpoint)
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(
			json.dumps({
				"schema_version": CHECKPOINT_SCHEMA_VERSION,
				"workload": "derived",
				"session": session,
				"content_digest": content_digest,
				"after_commit": True,
			}, indent=2, sort_keys=True) + "\n",
			encoding="utf-8",
		)

	def write_derived(self, session: str, *, checkpoint: Path | None = None, interruption: str | None = None) -> dict[str, object]:
		_, prior_session, following_session, expected_data = self._target_dependencies(session)
		expected, dependency_id = expected_data
		content_digest = derived_id(expected)
		self._validate_checkpoint(checkpoint, session, content_digest)
		connection = self.target.connect()
		cursor = connection.cursor(buffered=True)
		committed = False
		started = perf_counter()
		try:
			self.target.validate(connection)
			connection.autocommit = False
			for dependency_session, dependency_bars in (
				(prior_session, self._archive(prior_session)[0]),
				(session, self._archive(session)[0]),
				(following_session, self._archive(following_session)[0]),
			):
				self._assert_raw_rows(cursor, dependency_session, dependency_bars)
			stage = quote_identifier(STAGE_TABLE)
			cursor.execute(f"DELETE FROM {stage} WHERE target_date = %s", (session,))
			cursor.execute(
				f"""INSERT INTO {stage}
(target_date, prior_date, following_date, target_raw_id, prior_raw_id, following_raw_id,
 derived_dependency_id, derived_content_id, expected_rows)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
				(
					session,
					prior_session,
					following_session,
					self._archive(session)[2]["records_id"],
					self._archive(prior_session)[2]["records_id"],
					self._archive(following_session)[2]["records_id"],
					dependency_id,
					content_digest,
					len(expected),
				),
			)
			before = self._derived_rows(cursor, session)
			self._execute(cursor, f"CALL {quote_identifier(ROUTINE_NAME)}(%s)", (session,))
			actual = self._assert_derived_rows(cursor, session, expected)
			if interruption == "before_commit":
				raise RuntimeError("Injected interruption before commit")
			connection.commit()
			committed = True
			if interruption == "after_commit":
				raise RuntimeError("Injected interruption after commit")
			self._write_checkpoint(checkpoint, session, content_digest)
			connection.autocommit = True
			return {
				"session": session,
				"rows": len(expected),
				"changed_rows": 0 if before == actual else len(expected),
				"dependency_id": dependency_id,
				"content_id": content_digest,
				"seconds": perf_counter() - started,
				"committed": True,
			}
		except Exception:
			if not committed:
				connection.rollback()
			raise
		finally:
			cursor.close()
			connection.close()

	def snapshot(self) -> str:
		values = []
		for table in (
			self.names.prices,
			self.names.deltas,
			self.names.imports,
			"stage6_completion",
			"stage8a_raw_stage",
			STAGE_TABLE,
		):
			rows = self.query(f"SELECT * FROM {quote_identifier(table)} ORDER BY 1, 2")
			values.append([[None if value is None else str(value) for value in row] for row in rows])
		return content_id(values)

	def explain(self, session: str) -> list[dict[str, object]]:
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor(dictionary=True)
			cursor.execute(
				f"""EXPLAIN SELECT c.ticker_id, p.ticker_id, n.ticker_id
FROM {quote_identifier(self.names.prices)} c
LEFT JOIN {quote_identifier(self.names.prices)} p ON p.ticker_id = c.ticker_id
LEFT JOIN {quote_identifier(self.names.prices)} n ON n.ticker_id = c.ticker_id
JOIN {quote_identifier(self.names.sessions)} s ON s.id = c.date_id
WHERE s.transaction_date = %s""",
				(session,),
			)
			return [{str(key): (None if value is None else str(value)) for key, value in row.items()} for row in cursor.fetchall()]
		finally:
			connection.close()


class SQLDerivedTrial:
	def __init__(self, manifest_path: Path, dbinfo_file: Path, schema: str, directory: Path) -> None:
		self.manifest_path = manifest_path
		self.dbinfo_file = dbinfo_file
		self.directory = directory
		self.manifest = load_manifest(manifest_path)
		self.target = SQLDerivedTarget(dbinfo_file, schema, self.manifest)
		self.schema = self.target.prepare()
		self.candidate = SQLDerivedCandidate(self.manifest, self.target, directory / "sql-derived-reference.json")
		self.python = MySQLPythonBaseline(self.manifest, self.target, directory / "sql-derived-python-seed.json")

	def _seed(self, sessions: set[str]) -> None:
		for session in sorted(sessions):
			self.python._raw_session(session, seed=True)

	def check_derived(self, session: str) -> int:
		_, prior, following, expected_data = self.candidate._target_dependencies(session)
		expected, _ = expected_data
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor(buffered=True)
			self.candidate._assert_raw_rows(cursor, prior, self.candidate._archive(prior)[0])
			self.candidate._assert_raw_rows(cursor, session, self.candidate._archive(session)[0])
			self.candidate._assert_raw_rows(cursor, following, self.candidate._archive(following)[0])
			self.candidate._assert_derived_rows(cursor, session, expected)
			cursor.execute(
				f"SELECT derived_dependency_id, derived_content_id FROM {quote_identifier(self.candidate.names.imports)} WHERE session_date = %s",
				(session,),
			)
			receipt = cursor.fetchone()
			dependency_id = content_id({
				"prior": prior,
				"following": following,
				"raw": [self.candidate._archive(value)[2]["records_id"] for value in (prior, following)],
			})
			if receipt != (dependency_id, derived_id(expected)):
				raise AssertionError("SQL derived receipt identity differs from the reference")
			return len(expected)
		finally:
			connection.close()

	def identity(self) -> dict[str, object]:
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor()
			cursor.execute(
				"SELECT ROUTINE_NAME, ROUTINE_DEFINITION, SQL_DATA_ACCESS, SECURITY_TYPE "
				"FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s AND ROUTINE_NAME IN (%s, %s) ORDER BY ROUTINE_NAME",
				(self.target.schema, "stage8a_adopt_raw", ROUTINE_NAME),
			)
			routines = [
				{"name": row[0], "definition_hash": content_id(row[1]), "data_access": row[2], "security_type": row[3]}
				for row in cursor.fetchall()
			]
			return {
				"manifest_id": self.manifest["manifest_id"],
				"registry": self.manifest["authoritative_registry"],
				"corrected_schema_hash": self.manifest["corrected_schema_hash"],
				"schema_hash": self.schema["schema_hash"],
				"objects_hash": self.target.objects_fingerprint(connection),
				"stage_table": STAGE_TABLE,
				"routines": routines,
			}
		finally:
			connection.close()

	def recovery(self) -> list[str]:
		checks = []
		session = "2024-07-02"
		checkpoint = self.directory / "derived-checkpoint.json"
		before = self.candidate.snapshot()
		try:
			self.candidate.write_derived(session, checkpoint=checkpoint, interruption="before_commit")
		except RuntimeError as error:
			if str(error) != "Injected interruption before commit":
				raise
		if self.candidate.snapshot() != before or checkpoint.exists():
			raise AssertionError("SQL derived before-commit rollback failed")
		checks.append("derived_before_commit_rollback")
		try:
			self.candidate.write_derived(session, checkpoint=checkpoint, interruption="after_commit")
		except RuntimeError as error:
			if str(error) != "Injected interruption after commit":
				raise
		self.check_derived(session)
		if checkpoint.exists():
			raise AssertionError("SQL derived checkpoint was written after injected interruption")
		checks.append("derived_after_commit_durable_without_checkpoint")
		before = self.candidate.snapshot()
		result = self.candidate.write_derived(session, checkpoint=checkpoint)
		if result["changed_rows"] != 0 or self.candidate.snapshot() != before or not checkpoint.exists():
			raise AssertionError("SQL derived restart was not idempotent")
		checks.append("derived_restart_idempotent")
		stale = self.directory / "stale-derived-checkpoint.json"
		stale.write_text('{"schema_version":1,"manifest_id":"stale"}', encoding="utf-8")
		before = self.candidate.snapshot()
		try:
			self.candidate.write_derived(session, checkpoint=stale)
		except ValueError as error:
			if str(error) != "Stale Stage 8b checkpoint":
				raise
		if self.candidate.snapshot() != before:
			raise AssertionError("Stale derived checkpoint altered SQL target")
		checks.append("stale_checkpoint_rejected")
		archive = next(Path(item["path"]) for item in self.manifest["files"] if item["session"] == session)
		original = archive.read_bytes()
		try:
			archive.write_bytes(original.replace(b"14982.4", b"14982.5"))
			before = self.candidate.snapshot()
			try:
				self.candidate.write_derived(session)
			except ValueError as error:
				if str(error) != f"Changed Stage 6 input: {session}":
					raise
			if self.candidate.snapshot() != before:
				raise AssertionError("Changed source altered SQL derived target")
		finally:
			archive.write_bytes(original)
		checks.append("changed_input_rejected")
		names = self.candidate.names
		prices = quote_identifier(names.prices)
		sessions = quote_identifier(names.sessions)
		date_id = self.candidate.query(
			f"SELECT id FROM {sessions} WHERE transaction_date = %s",
			("2024-07-01",),
		)[0][0]
		self.candidate.query(
			f"UPDATE {prices} SET `close` = 10 WHERE date_id = %s",
			(date_id,),
			write=True,
		)
		before = self.candidate.snapshot()
		try:
			self.candidate.write_derived(session)
		except ValueError as error:
			if not str(error).startswith("Stored raw dependency differs"):
				raise
		if self.candidate.snapshot() != before:
			raise AssertionError("Stored dependency corruption altered SQL derived target")
		checks.append("stored_dependency_corruption_rejected")
		self.python._raw_session("2024-07-01", seed=True)
		missing = deepcopy(self.manifest)
		next(item for item in missing["files"] if item["session"] == "2024-07-01")["valid"] = False
		missing.pop("manifest_id")
		missing["manifest_id"] = content_id(missing)
		missing_path = self.directory / "missing-manifest.json"
		missing_path.write_text(json.dumps(missing), encoding="utf-8")
		missing_candidate = SQLDerivedCandidate(missing, self.target, self.directory / "missing-reference.json")
		before = self.candidate.snapshot()
		try:
			missing_candidate.write_derived(session)
		except ValueError as error:
			if str(error) != "Stage 8a source session is unavailable: 2024-07-01":
				raise
		if self.candidate.snapshot() != before:
			raise AssertionError("Missing dependency altered SQL derived target")
		checks.append("missing_dependency_not_published")
		return checks


def run_trial(manifest_path: Path, dbinfo_file: Path, schema: str, output: Path) -> dict:
	manifest = load_manifest(manifest_path)
	output.parent.mkdir(parents=True, exist_ok=True)
	repository = Path(__file__).resolve().parents[3]
	code_files = [
		Path(__file__),
		Path(__file__).with_name("sql_raw_candidate.py"),
		Path(__file__).with_name("mysql_candidates.py"),
		Path(__file__).with_name("candidates.py"),
	]
	report = {
		"schema_version": 1,
		"stage": "8b",
		"backend": "sql",
		"storage_engine": "mysql",
		"status": "incomplete",
		"manifest_id": manifest["manifest_id"],
		"recovery_checks": [],
		"sessions": [],
		"comparison_eligible": False,
		"production_access": {"ddl": False, "dml": False},
		"protocol": {
			"writers": 1,
			"preparers": 1,
			"queued_sessions_max": 3,
			"transaction_unit": "one derived session",
			"procedure": "set-based joins and window ranking; no client row publication loop",
			"timings": "correctness trial; Stage 9 supplies startup-inclusive measurements",
		},
		"code_hashes": {str(path.relative_to(repository)): _file_hash(path) for path in code_files},
	}

	def save() -> None:
		output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	try:
		fixture_path = _fixture_manifest(manifest, output.parent / f"{schema}_fixtures")
		fixture = SQLDerivedTrial(fixture_path, dbinfo_file, schema + "_fx", fixture_path.parent)
		fixture._seed({"2024-07-01", "2024-07-02", "2024-07-03"})
		report["fixture_schema"] = fixture.schema
		report["fixture_identity"] = fixture.identity()
		report["fixture_explain"] = fixture.candidate.explain("2024-07-02")
		report["recovery_checks"] = fixture.recovery()
		sql_trial = SQLDerivedTrial(manifest_path, dbinfo_file, schema, output.parent)
		dependency_sessions = {
			dependency
			for target in manifest["targets"]
			if "fixed_25" in target.get("roles", ())
			for dependency in target.get("derived_dependencies", ())
		}
		dependency_sessions.update(manifest["target_blocks"]["fixed_25"])
		sql_trial._seed(dependency_sessions)
		report["schema"] = sql_trial.schema
		report["identity"] = sql_trial.identity()
		report["explain"] = sql_trial.candidate.explain(manifest["target_blocks"]["fixed_25"][0])
		for target in manifest["target_blocks"]["fixed_25"]:
			result = sql_trial.candidate.write_derived(target)
			rows = sql_trial.check_derived(target)
			report["sessions"].append({
				"session": target,
				"derived_rows": rows,
				"changed_rows": result["changed_rows"],
				"dependency_id": result["dependency_id"],
				"content_id": result["content_id"],
			})
			save()
		report["status"] = "complete"
		save()
	except Exception as error:
		report["status"] = "failed"
		report["failure_type"] = type(error).__name__
		report["failure_frames"] = [
			{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
			for frame in traceback.extract_tb(error.__traceback__)
		]
		save()
		raise RuntimeError("Disposable MySQL SQL derived trial failed; inspect its sanitized report") from None
	return report
