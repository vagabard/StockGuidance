from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import math
from pathlib import Path
import traceback
from time import perf_counter

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.market_data.identity import content_id
from stockguidance.performance.candidates import _file_hash, _manifest_table_names
from stockguidance.performance.mysql_candidates import MySQLPythonBaseline, MySQLTarget, load_manifest
from stockguidance.performance.sql_derived_candidate import sql_derived_objects, sql_derived_schema_statements
from stockguidance.performance.sql_raw_candidate import (
	SQLRawCandidate,
	sql_raw_objects,
	sql_raw_schema_statements,
)
from stockguidance.validation.derived import _equal
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import InputState, ZScoreInput, ZScoreObservation
from stockguidance.zscores.specification import DELTA_DEFINITION, ZScoreFamily, ZScoreSpecification


TRIAL_RECEIPT = "2026-10-20T00:00:00+00:00"
DEPENDENCY_TABLE = "stage8c_zscore_dependencies"
TICKER_SCOPE_TABLE = "stage8c_zscore_ticker_scope"
SCORE_TABLE = "stage8c_zscore_stage"
COMPUTE_ROUTINE_NAME = "stage8c_compute_zscores"
PUBLISH_ROUTINE_NAME = "stage8c_publish_zscores"
CHECKPOINT_SCHEMA_VERSION = 1
CALCULATION_VERSION = "rolling-population-zscore-v1"
PRECISION = "float32"
MISSING_VALUE_POLICY = "null_status_v1"
SUPPORTED_WINDOWS = (20, 60)


def _series_definition(manifest: dict, family: str, window: int) -> dict[str, object]:
	if family not in ("price", "price_delta") or window not in SUPPORTED_WINDOWS:
		raise ValueError("Unsupported SQL z-score series")
	return {
		"schema_version": 1,
		"source_scope": content_id(manifest["source"]["archive_source"]),
		"calendar_id": "XNYS",
		"family": family,
		"window": window,
		"calculation_version": CALCULATION_VERSION,
		"delta_definition": DELTA_DEFINITION if family == "price_delta" else "not_applicable",
		"precision": PRECISION,
		"missing_value_policy": MISSING_VALUE_POLICY,
	}


def _series_key(manifest: dict, family: str, window: int) -> str:
	return content_id(_series_definition(manifest, family, window))


def _stage_schema_statements(manifest: dict) -> tuple[str, ...]:
	names = _manifest_table_names(manifest)
	registry = {name.casefold() for name in names.tables(include_verification=True).values()}
	if any(name.casefold() in registry for name in (DEPENDENCY_TABLE, TICKER_SCOPE_TABLE, SCORE_TABLE)):
		raise ValueError("Stage 8c staging table collides with the selected registry")
	dependency = f"""CREATE TABLE `{DEPENDENCY_TABLE}` (
target_date DATE NOT NULL,
family VARCHAR(16) NOT NULL,
`window` INT NOT NULL,
sequence_no INT NOT NULL,
session_date DATE NOT NULL,
expected_raw_id CHAR(64) NULL,
PRIMARY KEY (target_date, family, `window`, sequence_no),
UNIQUE KEY stage8c_dependency_session (target_date, family, `window`, session_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"""
	ticker_scope = f"""CREATE TABLE `{TICKER_SCOPE_TABLE}` (
ticker_id BIGINT NOT NULL,
label VARCHAR(16) COLLATE utf8mb4_bin NOT NULL,
PRIMARY KEY (ticker_id),
UNIQUE KEY stage8c_ticker_scope_label (label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"""
	scores = f"""CREATE TABLE `{SCORE_TABLE}` (
target_date DATE NOT NULL,
family VARCHAR(16) NOT NULL,
`window` INT NOT NULL,
ticker_id BIGINT NOT NULL,
zscore FLOAT NULL,
status TINYINT NOT NULL,
PRIMARY KEY (target_date, family, `window`, ticker_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"""
	return dependency, ticker_scope, scores


def _compute_routine_statement(manifest: dict) -> str:
	names = _manifest_table_names(manifest)
	dependencies = f"`{DEPENDENCY_TABLE}`"
	scores = f"`{SCORE_TABLE}`"
	sessions = f"`{names.sessions}`"
	tickers = f"`{TICKER_SCOPE_TABLE}`"
	prices = f"`{names.prices}`"
	deltas = f"`{names.deltas}`"
	imports = f"`{names.imports}`"
	return f"""CREATE PROCEDURE `{COMPUTE_ROUTINE_NAME}`(IN p_family VARCHAR(16), IN p_window INT, IN p_session_date DATE)
SQL SECURITY INVOKER
MODIFIES SQL DATA
BEGIN
 DECLARE v_stage_count BIGINT DEFAULT 0;
 DECLARE v_min_sequence BIGINT DEFAULT -1;
 DECLARE v_max_sequence BIGINT DEFAULT -1;
 DECLARE v_target_count BIGINT DEFAULT 0;
 DECLARE v_mapping_count BIGINT DEFAULT 0;
 DECLARE v_required_count BIGINT DEFAULT 0;
 IF p_family NOT IN ('price', 'price_delta') OR p_window NOT IN (20, 60) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8c z-score series is unsupported';
 END IF;
 SELECT COUNT(*), COALESCE(MIN(sequence_no), -1), COALESCE(MAX(sequence_no), -1),
        SUM(session_date = p_session_date)
 INTO v_stage_count, v_min_sequence, v_max_sequence, v_target_count
 FROM {dependencies}
 WHERE target_date = p_session_date AND family = p_family AND `window` = p_window;
 SET v_required_count = p_window + IF(p_family = 'price_delta', 1, 0);
 IF v_stage_count < v_required_count OR v_min_sequence <> 0 OR v_max_sequence + 1 <> v_stage_count OR v_target_count <> 1 OR v_max_sequence < v_required_count - 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8c dependency sessions are incomplete or non-consecutive';
 END IF;
 SELECT COUNT(*) INTO v_mapping_count
 FROM {dependencies} d
 JOIN {sessions} s ON s.transaction_date = d.session_date
 WHERE d.target_date = p_session_date AND d.family = p_family AND d.`window` = p_window;
 IF v_mapping_count <> v_stage_count THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8c exchange-session mapping is unavailable';
 END IF;
 IF (SELECT COUNT(*) FROM {tickers}) = 0 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8c retained ticker scope is empty';
 END IF;
 DELETE FROM {scores}
 WHERE target_date = p_session_date AND family = p_family AND `window` = p_window;
 INSERT INTO {scores} (target_date, family, `window`, ticker_id, zscore, status)
 WITH dependency_rows AS (
  SELECT d.sequence_no, d.session_date, s.id AS date_id, d.expected_raw_id,
         CASE WHEN d.expected_raw_id IS NOT NULL
                   AND i.raw_id = d.expected_raw_id
                   AND i.available_at IS NOT NULL
                   AND i.available_at <> ''
              THEN 1 ELSE 0 END AS verified
  FROM {dependencies} d
  JOIN {sessions} s ON s.transaction_date = d.session_date
  LEFT JOIN {imports} i ON i.session_date = d.session_date
  WHERE d.target_date = p_session_date AND d.family = p_family AND d.`window` = p_window
 ), raw_values AS (
  SELECT t.ticker_id AS ticker_id, d.sequence_no, d.session_date, d.date_id, d.verified,
         p.`close` AS close_value,
         CASE WHEN d.verified = 0 THEN 3
              WHEN p.`close` IS NULL THEN 2
              WHEN p.`close` <= 0 THEN 4
              ELSE 0 END AS close_issue
  FROM {tickers} t
  CROSS JOIN dependency_rows d
  LEFT JOIN {prices} p ON p.ticker_id = t.ticker_id AND p.date_id = d.date_id
 ), paired_values AS (
  SELECT current_row.*, prior_row.close_value AS previous_close,
         prior_row.close_issue AS previous_issue,
         delta.close_ratio_to_prior AS stored_ratio
  FROM raw_values current_row
  LEFT JOIN raw_values prior_row
    ON prior_row.ticker_id = current_row.ticker_id
   AND prior_row.sequence_no = current_row.sequence_no - 1
  LEFT JOIN {deltas} delta
    ON delta.ticker_id = current_row.ticker_id
   AND delta.date_id = current_row.date_id
 ), classified AS (
  SELECT paired_values.*,
         CASE
          WHEN p_family = 'price' THEN close_issue
          WHEN sequence_no = 0 THEN 1
          WHEN close_issue <> 0 THEN close_issue
          WHEN previous_issue <> 0 THEN previous_issue
          WHEN stored_ratio IS NULL THEN 2
          WHEN stored_ratio <= 0
            OR ABS(1e0 * stored_ratio - 1e0 * close_value / NULLIF(previous_close, 0))
                > GREATEST(1e-12, 1e-10 * ABS(1e0 * close_value / NULLIF(previous_close, 0))) THEN 4
          ELSE 0
         END AS issue,
         CASE
          WHEN p_family = 'price' AND close_issue = 0 THEN 1e0 * close_value
          WHEN p_family = 'price_delta' AND sequence_no > 0 AND close_issue = 0 AND previous_issue = 0
            AND stored_ratio IS NOT NULL AND stored_ratio > 0
            AND ABS(1e0 * stored_ratio - 1e0 * close_value / NULLIF(previous_close, 0))
                <= GREATEST(1e-12, 1e-10 * ABS(1e0 * close_value / NULLIF(previous_close, 0)))
            THEN 1e0 * stored_ratio - 1e0
          ELSE NULL
         END AS numeric_value
  FROM paired_values
 ), bounds AS (
  SELECT MAX(sequence_no) AS target_sequence
  FROM dependency_rows
 ), window_values AS (
  SELECT classified.*
  FROM classified
  CROSS JOIN bounds
  WHERE classified.sequence_no >= bounds.target_sequence - p_window + 1
 ), grouped_values AS (
  SELECT ticker_id, COUNT(*) AS value_count, MAX(issue) AS highest_issue,
         AVG(numeric_value) AS average_value,
         STDDEV_POP(numeric_value) AS population_stddev,
         MAX(CASE WHEN sequence_no = (SELECT target_sequence FROM bounds) THEN numeric_value END) AS target_value
  FROM window_values
  GROUP BY ticker_id
 )
 SELECT p_session_date, p_family, p_window, ticker_id,
        CASE WHEN highest_issue <> 0 OR value_count <> p_window OR population_stddev = 0 THEN NULL
             ELSE (target_value - average_value) / population_stddev END,
        CASE WHEN value_count <> p_window THEN 1
             WHEN highest_issue <> 0 THEN highest_issue
             WHEN population_stddev = 0 THEN 5
             ELSE 0 END
 FROM grouped_values;
END"""


def _publish_routine_statement(manifest: dict) -> str:
	names = _manifest_table_names(manifest)
	scores = f"`{SCORE_TABLE}`"
	sessions = f"`{names.sessions}`"
	series = f"`{names.zscore_series}`"
	metadata = f"`{names.zscore_sessions}`"
	completion = "`stage6_completion`"
	physical = names.resolve_zscore_tables(SUPPORTED_WINDOWS)
	branches = {
		("price", 20): f"`{physical[20].price}`",
		("price", 60): f"`{physical[60].price}`",
		("price_delta", 20): f"`{physical[20].price_delta}`",
		("price_delta", 60): f"`{physical[60].price_delta}`",
	}
	branch_sql = []
	for index, ((family, window), table) in enumerate(branches.items()):
		prefix = "IF" if index == 0 else "ELSEIF"
		branch_sql.append(f""" {prefix} p_family = '{family}' AND p_window = {window} THEN
  DELETE FROM {table} WHERE date_id = v_date_id;
  INSERT INTO {table} (date_id, ticker_id, zscore, status)
  SELECT v_date_id, ticker_id, zscore, status FROM {scores}
  WHERE target_date = p_session_date AND family = p_family AND `window` = p_window;""")
	return f"""CREATE PROCEDURE `{PUBLISH_ROUTINE_NAME}`(
 IN p_family VARCHAR(16), IN p_window INT, IN p_session_date DATE,
 IN p_series_key CHAR(64), IN p_dependency_digest CHAR(64), IN p_availability_digest CHAR(64),
 IN p_actual_input_available TINYINT, IN p_completed_at VARCHAR(40), IN p_row_count INT,
 IN p_ready_count INT, IN p_status_counts VARCHAR(256), IN p_content_digest CHAR(64))
SQL SECURITY INVOKER
MODIFIES SQL DATA
BEGIN
 DECLARE v_date_count BIGINT DEFAULT 0;
 DECLARE v_date_id BIGINT DEFAULT NULL;
 DECLARE v_stage_count BIGINT DEFAULT 0;
 DECLARE v_stage_ready BIGINT DEFAULT 0;
 DECLARE v_series_count BIGINT DEFAULT 0;
 IF p_family NOT IN ('price', 'price_delta') OR p_window NOT IN (20, 60) THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8c publication series is unsupported';
 END IF;
 SELECT COUNT(*), MIN(id) INTO v_date_count, v_date_id FROM {sessions} WHERE transaction_date = p_session_date;
 IF v_date_count <> 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8c publication session mapping is unavailable';
 END IF;
 SELECT COUNT(*), SUM(status = 0) INTO v_stage_count, v_stage_ready
 FROM {scores} WHERE target_date = p_session_date AND family = p_family AND `window` = p_window;
 IF v_stage_count <> p_row_count OR v_stage_ready <> p_ready_count OR v_stage_count = 0 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8c publication population is incomplete';
 END IF;
 SELECT COUNT(*) INTO v_series_count FROM {series} WHERE series_key = p_series_key;
 IF v_series_count <> 1 THEN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Stage 8c series metadata is unavailable';
 END IF;
""" + "\n".join(branch_sql) + f"""
 END IF;
 DELETE FROM {metadata} WHERE series_key = p_series_key AND date_id = v_date_id;
 INSERT INTO {metadata} (series_key, date_id, dependency_digest, availability_digest, actual_input_available,
                         completed_at, row_count, ready_count, status_counts, content_digest)
 VALUES (p_series_key, v_date_id, p_dependency_digest, p_availability_digest, p_actual_input_available,
         p_completed_at, p_row_count, p_ready_count, p_status_counts, p_content_digest);
 INSERT INTO {completion} (workload, session_date, series_key, content_digest, committed_at)
 VALUES ('zscores', p_session_date, p_series_key, p_content_digest, p_completed_at)
 ON DUPLICATE KEY UPDATE content_digest = VALUES(content_digest), committed_at = VALUES(committed_at);
END"""


def sql_zscore_schema_statements(manifest: dict) -> tuple[str, ...]:
	return _stage_schema_statements(manifest)


def sql_zscore_objects(manifest: dict) -> tuple[str, ...]:
	return _compute_routine_statement(manifest), _publish_routine_statement(manifest)


class SQLZScoreTarget(MySQLTarget):
	def __init__(self, dbinfo_file: Path, schema: str, manifest: dict) -> None:
		extra_statements = sql_raw_schema_statements(manifest) + sql_derived_schema_statements(manifest) + sql_zscore_schema_statements(manifest)
		extra_objects = sql_raw_objects(manifest) + sql_derived_objects(manifest) + sql_zscore_objects(manifest)
		super().__init__(dbinfo_file, schema, manifest, extra_statements=extra_statements, extra_objects=extra_objects)


class SQLZScoreCandidate:
	def __init__(self, manifest: dict, target: SQLZScoreTarget, output: Path) -> None:
		self.manifest = manifest
		self.target = target
		self.output = output
		self.names = _manifest_table_names(manifest)
		self.files = {item["session"]: item for item in manifest["files"]}
		targets = [item for item in manifest["targets"] if "chronological_20" in item.get("roles", ())]
		if not targets:
			raise ValueError("Stage 8c chronological z-score target is unavailable")
		self.target_session = targets[-1]["session"]
		self.dependencies = tuple(targets[-1].get("zscore_dependencies", ()))
		if not self.dependencies or self.dependencies[-1] != self.target_session:
			raise ValueError("Stage 8c z-score dependencies are not target-terminated")
		self.raw = SQLRawCandidate(manifest, target, output.with_name("sql-zscores-raw-reference.json"))

	def _execute(self, cursor, statement: str, parameters=()) -> None:
		cursor.execute(statement, parameters)
		while cursor.nextset():
			if cursor.with_rows:
				cursor.fetchall()

	def query(self, sql: str, parameters=(), *, write: bool = False):
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor()
			try:
				cursor.execute(sql, parameters)
				if write:
					connection.commit()
					return cursor.rowcount
				return cursor.fetchall()
			finally:
				cursor.close()
		finally:
			connection.close()

	def _verify_source_identities(self) -> None:
		for session in self.dependencies:
			item = self.files.get(session)
			if isinstance(item, dict) and item.get("valid"):
				self.raw._archive(session)

	def _expected_raw_id(self, session: str) -> str | None:
		item = self.files.get(session)
		if not isinstance(item, dict) or not item.get("valid"):
			return None
		value = item.get("records_id")
		return value if isinstance(value, str) else None

	def _stage_dependencies(self, cursor, family: str, window: int) -> None:
		stage = f"`{DEPENDENCY_TABLE}`"
		cursor.execute(
			f"DELETE FROM {stage} WHERE target_date = %s AND family = %s AND `window` = %s",
			(self.target_session, family, window),
		)
		rows = [
			(self.target_session, family, window, sequence, session, self._expected_raw_id(session))
			for sequence, session in enumerate(self.dependencies)
		]
		cursor.executemany(
			f"INSERT INTO {stage} (target_date, family, `window`, sequence_no, session_date, expected_raw_id) VALUES (%s, %s, %s, %s, %s, %s)",
			rows,
		)

	def _date_ids(self, cursor) -> dict[str, int]:
		names = f"`{self.names.sessions}`"
		placeholders = ",".join("%s" for _ in self.dependencies)
		cursor.execute(
			f"SELECT id, transaction_date FROM {names} WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date, id",
			self.dependencies,
		)
		rows = cursor.fetchall()
		result = {str(session): int(identifier) for identifier, session in rows}
		if set(result) != set(self.dependencies) or len(rows) != len(result):
			raise ValueError("Stage 8c date mappings are incomplete or duplicated")
		return result

	def _ticker_ids(self, cursor) -> tuple[int, ...]:
		table = f"`{self.names.tickers}`"
		cursor.execute(f"SELECT id, label FROM {table} ORDER BY id")
		actual = {int(identifier): str(label) for identifier, label in cursor.fetchall()}
		expected = {int(item["id"]): item["label"] for item in self.manifest["mappings"]["ticker_mappings"]}
		if any(actual.get(identifier) != label for identifier, label in expected.items()):
			raise ValueError("Stage 8c ticker mapping differs from manifest")
		return tuple(sorted(expected))

	def _stage_ticker_scope(self, cursor, ticker_ids: tuple[int, ...]) -> None:
		table = f"`{TICKER_SCOPE_TABLE}`"
		cursor.execute(f"DELETE FROM {table}")
		labels = {int(item["id"]): item["label"] for item in self.manifest["mappings"]["ticker_mappings"]}
		cursor.executemany(
			f"INSERT INTO {table} (ticker_id, label) VALUES (%s, %s)",
			[(ticker_id, labels[ticker_id]) for ticker_id in ticker_ids],
		)

	def _observations(self, cursor, family: str, date_ids: dict[str, int], ticker_ids: tuple[int, ...]) -> dict[int, tuple[tuple[object | None, InputState], ...]]:
		placeholders = ",".join("%s" for _ in date_ids)
		prices = f"`{self.names.prices}`"
		deltas = f"`{self.names.deltas}`"
		cursor.execute(
			f"SELECT date_id, ticker_id, `close` FROM {prices} WHERE date_id IN ({placeholders}) ORDER BY ticker_id, date_id",
			tuple(date_ids.values()),
		)
		raw = {(int(ticker_id), int(date_id)): value for date_id, ticker_id, value in cursor.fetchall()}
		delta = {}
		if family == "price_delta":
			cursor.execute(
				f"SELECT date_id, ticker_id, close_ratio_to_prior FROM {deltas} WHERE date_id IN ({placeholders}) ORDER BY ticker_id, date_id",
				tuple(date_ids.values()),
			)
			delta = {(int(ticker_id), int(date_id)): value for date_id, ticker_id, value in cursor.fetchall()}
		imports = f"`{self.names.imports}`"
		cursor.execute(
			f"SELECT session_date, raw_id, available_at FROM {imports} WHERE session_date IN ({placeholders}) ORDER BY session_date",
			self.dependencies,
		)
		receipts = {str(session): (raw_id, available_at) for session, raw_id, available_at in cursor.fetchall()}
		calendar = ExchangeCalendar("XNYS", date.fromisoformat(self.dependencies[0]), date.fromisoformat(self.dependencies[-1]))
		sessions = tuple(calendar.session(date.fromisoformat(session)) for session in self.dependencies)
		expected = {session: self._expected_raw_id(session) for session in self.dependencies}
		result: dict[int, tuple[tuple[object | None, InputState], ...]] = {}
		for ticker_id in ticker_ids:
			values: list[tuple[object | None, InputState]] = []
			previous: tuple[InputState, float | None] | None = None
			for index, session in enumerate(sessions):
				session_key = session.session_date.isoformat()
				receipt = receipts.get(session_key)
				verified = expected[session_key] is not None and receipt is not None and receipt[0] == expected[session_key] and bool(receipt[1])
				value = raw.get((ticker_id, date_ids[session_key]))
				if not verified:
					close = (InputState.UNVERIFIED, None)
				elif value is None:
					close = (InputState.MISSING, None)
				elif not _finite_positive(value):
					close = (InputState.INVALID, None)
				else:
					close = (InputState.AVAILABLE, float(value))
				if family == "price":
					values.append((close[1], close[0]))
				elif index == 0:
					values.append((None, InputState.INSUFFICIENT_HISTORY))
				elif close[0] is not InputState.AVAILABLE or previous is None or previous[0] is not InputState.AVAILABLE:
					priority = {InputState.INVALID: 4, InputState.UNVERIFIED: 3, InputState.MISSING: 2, InputState.INSUFFICIENT_HISTORY: 1}
					state = max((close[0], previous[0]), key=lambda item: priority.get(item, 0))
					values.append((None, state))
				else:
					ratio = delta.get((ticker_id, date_ids[session_key]))
					expected_ratio = close[1] / previous[1]
					if ratio is None:
						values.append((None, InputState.MISSING))
					elif not _finite_positive(ratio) or abs(float(ratio) - expected_ratio) > max(1e-12, 1e-10 * abs(expected_ratio)):
						values.append((None, InputState.INVALID))
					else:
						values.append((float(ratio) - 1.0, InputState.AVAILABLE))
				previous = close
			result[ticker_id] = tuple(values)
		return result

	def _oracle(self, cursor, family: str, window: int) -> dict[int, tuple[float | None, int]]:
		date_ids = self._date_ids(cursor)
		ticker_ids = self._ticker_ids(cursor)
		observations = self._observations(cursor, family, date_ids, ticker_ids)
		calendar = ExchangeCalendar("XNYS", date.fromisoformat(self.dependencies[0]), date.fromisoformat(self.dependencies[-1]))
		sessions = tuple(calendar.session(date.fromisoformat(session)) for session in self.dependencies)
		input_values = {
			ticker_id: tuple(ZScoreObservation(value=value, state=state) for value, state in values)
			for ticker_id, values in observations.items()
		}
		family_value = ZScoreFamily.PRICE if family == "price" else ZScoreFamily.PRICE_DELTA
		input_data = ZScoreInput.from_values(family_value, sessions, input_values, dependency_sessions=sessions, scored_sessions=(sessions[-1],))
		specification = ZScoreSpecification(family_value, (window,), delta_definition=DELTA_DEFINITION if family == "price_delta" else "not_applicable")
		result = calculate_zscores(specification, input_data)
		row_by_ticker = {row.ticker_id: (None if row.score is None else float(row.score), int(row.status)) for row in result.rows}
		return row_by_ticker

	@staticmethod
	def _decimal_text(value: object | None) -> str:
		if value is None:
			return "null"
		return format(float(value), ".15g")

	def _input_digests(self, cursor, family: str, date_ids: dict[str, int], ticker_ids: tuple[int, ...]) -> tuple[str, str, bool]:
		placeholders = ",".join("%s" for _ in date_ids)
		prices = f"`{self.names.prices}`"
		cursor.execute(
			f"SELECT date_id, ticker_id, `close` FROM {prices} WHERE date_id IN ({placeholders}) ORDER BY ticker_id, date_id",
			tuple(date_ids.values()),
		)
		raw = {(int(ticker_id), int(date_id)): value for date_id, ticker_id, value in cursor.fetchall()}
		delta = {}
		if family == "price_delta":
			deltas = f"`{self.names.deltas}`"
			cursor.execute(
				f"SELECT date_id, ticker_id, close_ratio_to_prior FROM {deltas} WHERE date_id IN ({placeholders}) ORDER BY ticker_id, date_id",
				tuple(date_ids.values()),
			)
			delta = {(int(ticker_id), int(date_id)): value for date_id, ticker_id, value in cursor.fetchall()}
		imports = f"`{self.names.imports}`"
		cursor.execute(
			f"SELECT session_date, raw_id, available_at FROM {imports} WHERE session_date IN ({placeholders}) ORDER BY session_date",
			self.dependencies,
		)
		receipts = {str(session): (raw_id, available_at) for session, raw_id, available_at in cursor.fetchall()}
		dependency_lines: list[str] = []
		availability_lines: list[str] = []
		actual_available = True
		for ticker_id in ticker_ids:
			for session in self.dependencies:
				date_id = date_ids[session]
				value = raw.get((ticker_id, date_id))
				ratio = delta.get((ticker_id, date_id))
				expected = self._expected_raw_id(session)
				receipt = receipts.get(session)
				verified = expected is not None and receipt is not None and receipt[0] == expected and bool(receipt[1])
				if not verified:
					state = "unverified"
				elif value is None:
					state = "missing"
				elif not _finite_positive(value):
					state = "invalid"
				else:
					state = "none"
				if state != "none":
					actual_available = False
				dependency_lines.append("|".join((session, str(date_id), str(ticker_id), self._decimal_text(value), self._decimal_text(ratio), str(receipt[0]) if receipt else "null", str(receipt[1]) if receipt else "null")))
				availability_lines.append("|".join((session, str(ticker_id), state)))
		return content_id("\n".join(dependency_lines) + "\n"), content_id("\n".join(availability_lines) + "\n"), actual_available

	def _ensure_series(self, cursor, family: str, window: int) -> str:
		key = _series_key(self.manifest, family, window)
		table = f"`{self.names.zscore_series}`"
		cursor.execute(
			f"SELECT schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy FROM {table} WHERE series_key = %s FOR UPDATE",
			(key,),
		)
		rows = cursor.fetchall()
		definition = _series_definition(self.manifest, family, window)
		if rows:
			if len(rows) != 1 or tuple(rows[0]) != tuple(definition[field] for field in ("schema_version", "source_scope", "calendar_id", "family", "window", "calculation_version", "delta_definition", "precision", "missing_value_policy")):
				raise ValueError("Conflicting SQL z-score series metadata")
			return key
		cursor.execute(
			f"INSERT INTO {table} (series_key, schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
			(key, definition["schema_version"], definition["source_scope"], definition["calendar_id"], definition["family"], definition["window"], definition["calculation_version"], definition["delta_definition"], definition["precision"], definition["missing_value_policy"], TRIAL_RECEIPT),
		)
		return key

	def _validate_checkpoint(self, checkpoint: Path | None, family: str, window: int, content_digest: str) -> None:
		if checkpoint is None or not checkpoint.exists():
			return
		try:
			value = json.loads(checkpoint.read_text(encoding="utf-8"))
		except (OSError, UnicodeError, json.JSONDecodeError):
			raise ValueError("Invalid Stage 8c checkpoint") from None
		expected = {
			"schema_version": CHECKPOINT_SCHEMA_VERSION,
			"manifest_id": self.manifest["manifest_id"],
			"workload": "zscores",
			"family": family,
			"window": window,
			"session": self.target_session,
			"content_digest": content_digest,
			"after_commit": True,
		}
		if any(value.get(key) != expected_value for key, expected_value in expected.items()):
			raise ValueError("Stale or incompatible SQL z-score checkpoint")

	def _write_checkpoint(self, checkpoint: Path | None, family: str, window: int, content_digest: str) -> None:
		if checkpoint is None:
			return
		checkpoint.parent.mkdir(parents=True, exist_ok=True)
		checkpoint.write_text(json.dumps({
			"schema_version": CHECKPOINT_SCHEMA_VERSION,
			"manifest_id": self.manifest["manifest_id"],
			"workload": "zscores",
			"family": family,
			"window": window,
			"session": self.target_session,
			"content_digest": content_digest,
			"after_commit": True,
			"committed_at": TRIAL_RECEIPT,
		}, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	def _stage_rows(self, cursor, family: str, window: int) -> dict[int, tuple[float | None, int]]:
		cursor.execute(
			f"SELECT ticker_id, zscore, status FROM `{SCORE_TABLE}` WHERE target_date = %s AND family = %s AND `window` = %s ORDER BY ticker_id",
			(self.target_session, family, window),
		)
		return {int(ticker_id): (None if score is None else float(score), int(status)) for ticker_id, score, status in cursor.fetchall()}

	def _physical_rows(self, cursor, family: str, window: int, date_id: int) -> dict[int, tuple[float | None, int]]:
		tables = self.names.resolve_zscore_tables((window,))[window]
		table = tables.price if family == "price" else tables.price_delta
		cursor.execute(f"SELECT ticker_id, zscore, status FROM `{table}` WHERE date_id = %s ORDER BY ticker_id", (date_id,))
		return {int(ticker_id): (None if score is None else float(score), int(status)) for ticker_id, score, status in cursor.fetchall()}

	def write_zscores(self, family: str, window: int, *, checkpoint: Path | None = None, interruption: str | None = None) -> dict[str, object]:
		if family not in ("price", "price_delta") or window not in SUPPORTED_WINDOWS:
			raise ValueError("Unsupported SQL z-score series")
		self._verify_source_identities()
		started = perf_counter()
		connection = self.target.connect()
		cursor = connection.cursor()
		committed = False
		try:
			self.target.validate(connection)
			connection.autocommit = False
			date_ids = self._date_ids(cursor)
			ticker_ids = self._ticker_ids(cursor)
			expected = self._oracle(cursor, family, window)
			content_digest = content_id([[self.target_session, ticker_id, score, status] for ticker_id, (score, status) in sorted(expected.items())])
			self._validate_checkpoint(checkpoint, family, window, content_digest)
			self._stage_ticker_scope(cursor, ticker_ids)
			self._stage_dependencies(cursor, family, window)
			self._execute(cursor, f"CALL `{COMPUTE_ROUTINE_NAME}`(%s, %s, %s)", (family, window, self.target_session))
			actual = self._stage_rows(cursor, family, window)
			if actual.keys() != expected.keys():
				raise ValueError("SQL z-score ticker scope differs from the retained manifest scope")
			for ticker_id, (actual_score, actual_status) in actual.items():
				expected_score, expected_status = expected[ticker_id]
				if actual_status != expected_status:
					raise ValueError(f"SQL z-score status differs for {family}/{window}/{ticker_id}")
				if expected_score is None:
					if actual_score is not None:
						raise ValueError(f"Unavailable SQL z-score has a value for {family}/{window}/{ticker_id}")
				elif actual_score is None or abs(actual_score - expected_score) > max(1e-6, 1e-5 * abs(expected_score)):
					raise ValueError(f"SQL z-score value differs for {family}/{window}/{ticker_id}")
			hash_started = perf_counter()
			dependency_digest, availability_digest, actual_input_available = self._input_digests(cursor, family, date_ids, ticker_ids)
			client_hash_seconds = perf_counter() - hash_started
			ready_count = sum(status == 0 for _, status in actual.values())
			status_counts = ",".join(f"{status}:{sum(value[1] == status for value in actual.values())}" for status in range(6))
			series_key = self._ensure_series(cursor, family, window)
			self._execute(cursor, f"CALL `{PUBLISH_ROUTINE_NAME}`(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (
				family, window, self.target_session, series_key, dependency_digest, availability_digest,
				int(actual_input_available), TRIAL_RECEIPT, len(actual), ready_count, status_counts, content_digest,
			))
			physical = self._physical_rows(cursor, family, window, date_ids[self.target_session])
			if physical != actual:
				raise ValueError("SQL z-score publication differs from set-based staging")
			if interruption == "before_commit":
				raise RuntimeError("Injected interruption before commit")
			connection.commit()
			committed = True
			if interruption == "after_commit":
				raise RuntimeError("Injected interruption after commit")
			self._write_checkpoint(checkpoint, family, window, content_digest)
			return {
				"family": family,
				"window": window,
				"session": self.target_session,
				"row_count": len(actual),
				"ready_count": ready_count,
				"content_digest": content_digest,
				"dependency_digest": dependency_digest,
				"availability_digest": availability_digest,
				"actual_input_available": actual_input_available,
				"client_hash_seconds": client_hash_seconds,
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
			self.names.zscore_series,
			self.names.zscore_sessions,
			"stage6_completion",
			DEPENDENCY_TABLE,
			TICKER_SCOPE_TABLE,
			SCORE_TABLE,
			"zscores_price_20",
			"zscores_price_60",
			"zscores_price_delta_20",
			"zscores_price_delta_60",
		):
			rows = self.query(f"SELECT * FROM `{table}` ORDER BY 1, 2")
			values.append([[None if value is None else str(value) for value in row] for row in rows])
		return content_id(values)

	def explain(self, session: str) -> list[dict[str, object]]:
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor(dictionary=True)
			cursor.execute(
				f"""EXPLAIN SELECT t.id, p.date_id, p.`close`
FROM `{self.names.tickers}` t
JOIN `{self.names.prices}` p ON p.ticker_id = t.id
JOIN `{self.names.sessions}` s ON s.id = p.date_id
WHERE s.transaction_date = %s
ORDER BY t.id""",
				(session,),
			)
			return [{str(key): (None if value is None else str(value)) for key, value in row.items()} for row in cursor.fetchall()]
		finally:
			connection.close()

	def identity(self) -> dict[str, object]:
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor()
			cursor.execute(
				"SELECT ROUTINE_NAME, ROUTINE_DEFINITION, SQL_DATA_ACCESS, SECURITY_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s AND ROUTINE_NAME IN (%s, %s, %s, %s) ORDER BY ROUTINE_NAME",
				(self.target.schema, "stage8a_adopt_raw", "stage8b_populate_derived", COMPUTE_ROUTINE_NAME, PUBLISH_ROUTINE_NAME),
			)
			routines = [{"name": row[0], "definition_hash": content_id(row[1]), "data_access": row[2], "security_type": row[3]} for row in cursor.fetchall()]
			return {
				"manifest_id": self.manifest["manifest_id"],
				"registry": self.manifest["authoritative_registry"],
				"corrected_schema_hash": self.manifest["corrected_schema_hash"],
				"schema_hash": self.target.metadata["schema_hash"],
				"objects_hash": self.target.objects_fingerprint(connection),
				"dependency_table": DEPENDENCY_TABLE,
				"score_table": SCORE_TABLE,
				"routines": routines,
			}
		finally:
			connection.close()

	def recovery(self) -> list[str]:
		checks: list[str] = []
		family, window = "price", 20
		checkpoint = self.output.with_name("sql-zscore-checkpoint.json")
		before = self.snapshot()
		try:
			self.write_zscores(family, window, checkpoint=checkpoint, interruption="before_commit")
		except RuntimeError as error:
			if str(error) != "Injected interruption before commit":
				raise
		if self.snapshot() != before or checkpoint.exists():
			raise AssertionError("SQL z-score before-commit rollback failed")
		checks.append("before_commit_rollback")
		try:
			self.write_zscores(family, window, checkpoint=checkpoint, interruption="after_commit")
		except RuntimeError as error:
			if str(error) != "Injected interruption after commit":
				raise
		if self.snapshot() == before or checkpoint.exists():
			raise AssertionError("SQL z-score after-commit durability failed")
		checks.append("after_commit_durable_without_checkpoint")
		stable = self.snapshot()
		self.write_zscores(family, window, checkpoint=checkpoint)
		if self.snapshot() != stable or not checkpoint.exists():
			raise AssertionError("SQL z-score restart was not idempotent")
		checks.append("restart_idempotent")
		checkpoint.write_text('{"schema_version":1,"manifest_id":"stale"}', encoding="utf-8")
		try:
			self.write_zscores(family, window, checkpoint=checkpoint)
		except ValueError as error:
			if str(error) != "Stale or incompatible SQL z-score checkpoint":
				raise
		if self.snapshot() != stable:
			raise AssertionError("Stale SQL z-score checkpoint changed storage")
		checks.append("stale_checkpoint_rejected")
		checkpoint.unlink()
		archive = next(Path(self.files[session]["path"]) for session in self.dependencies if self.files.get(session, {}).get("valid"))
		original = archive.read_bytes()
		try:
			archive.write_bytes(original + b"\n")
			try:
				self.write_zscores(family, window)
			except ValueError as error:
				if not str(error).startswith("Changed Stage 6 input:"):
					raise
		finally:
			archive.write_bytes(original)
		if self.snapshot() != stable:
			raise AssertionError("Changed source was published by SQL z-score candidate")
		checks.append("changed_input_rejected")
		return checks


class SQLZScoreTrial:
	def __init__(self, manifest_path: Path, dbinfo_file: Path, schema: str, directory: Path) -> None:
		self.manifest_path = manifest_path
		self.dbinfo_file = dbinfo_file
		self.directory = directory
		self.manifest = load_manifest(manifest_path)
		self.target = SQLZScoreTarget(dbinfo_file, schema, self.manifest)
		self.schema = self.target.prepare()
		self.candidate = SQLZScoreCandidate(self.manifest, self.target, directory / "sql-zscores-reference.json")

	def seed_inputs(self) -> None:
		python = MySQLPythonBaseline(self.manifest, self.target, self.directory / "sql-zscores-python-seed.json")
		valid = tuple(session for session in self.candidate.dependencies if self.candidate.files[session].get("valid"))
		for session in valid:
			python._raw_session(session, seed=True)
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor()
			try:
				date_ids = self.candidate._date_ids(cursor)
				prices = f"`{self.candidate.names.prices}`"
				deltas = f"`{self.candidate.names.deltas}`"
				for prior, current in zip(self.candidate.dependencies, self.candidate.dependencies[1:]):
					cursor.execute(
						f"""INSERT INTO {deltas} (ticker_id, date_id, close_ratio_to_prior)
SELECT current_row.ticker_id, current_row.date_id, 1e0 * current_row.`close` / prior_row.`close`
FROM {prices} current_row
JOIN {prices} prior_row ON prior_row.ticker_id = current_row.ticker_id
WHERE current_row.date_id = %s AND prior_row.date_id = %s
  AND current_row.`close` IS NOT NULL AND prior_row.`close` IS NOT NULL AND prior_row.`close` <> 0
ON DUPLICATE KEY UPDATE close_ratio_to_prior = VALUES(close_ratio_to_prior)""",
						(date_ids[current], date_ids[prior]),
					)
				connection.commit()
			finally:
				cursor.close()
		finally:
			connection.close()

	def run_series(self, family: str, window: int) -> dict[str, object]:
		return self.candidate.write_zscores(family, window)


def _finite_positive(value: object) -> bool:
	try:
		return math.isfinite(float(value)) and float(value) > 0
	except (TypeError, ValueError, OverflowError):
		return False


def run_trial(manifest_path: Path, dbinfo_file: Path, schema: str, output: Path) -> dict:
	manifest = load_manifest(manifest_path)
	output.parent.mkdir(parents=True, exist_ok=True)
	repository = Path(__file__).resolve().parents[3]
	code_files = [Path(__file__), Path(__file__).with_name("sql_raw_candidate.py"), Path(__file__).with_name("sql_derived_candidate.py"), Path(__file__).with_name("mysql_candidates.py"), Path(__file__).with_name("candidates.py")]
	report = {
		"schema_version": 1,
		"stage": "8c",
		"backend": "sql",
		"storage_engine": "mysql",
		"status": "incomplete",
		"manifest_id": manifest["manifest_id"],
		"series": [],
		"recovery_checks": [],
		"comparison_eligible": False,
		"production_access": {"ddl": False, "dml": False},
		"protocol": {
			"writers": 1,
			"preparers": 1,
			"queued_sessions_max": 3,
			"transaction_unit": "one z-score series/session",
			"procedure": "set-based dependency/status/window aggregation and INSERT SELECT publication; no client row publication loop",
			"stored_delta_seed": "bounded INSERT SELECT from verified raw closes; disposable setup only",
			"score_precision": "float32",
			"timings": "correctness trial; Stage 9 supplies startup-inclusive measurements",
		},
		"code_hashes": {str(path.relative_to(repository)): _file_hash(path) for path in code_files},
	}

	def save() -> None:
		output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	try:
		trial = SQLZScoreTrial(manifest_path, dbinfo_file, schema, output.parent)
		report["schema"] = trial.schema
		report["target_session"] = trial.candidate.target_session
		report["dependency_session_count"] = len(trial.candidate.dependencies)
		report["identity"] = trial.candidate.identity()
		report["explain"] = trial.candidate.explain(trial.candidate.target_session)
		trial.seed_inputs()
		report["recovery_checks"] = trial.candidate.recovery()
		for family in ("price", "price_delta"):
			for window in SUPPORTED_WINDOWS:
				report["series"].append(trial.run_series(family, window))
			save()
		report["status"] = "complete"
		report["comparison_eligible"] = True
	except Exception as error:
		report["status"] = "failed"
		report["failure_type"] = type(error).__name__
		report["failure_message"] = str(error)
		report["failure_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
		save()
		raise RuntimeError("Disposable MySQL SQL z-score trial failed; inspect its sanitized report") from None
	save()
	return report
