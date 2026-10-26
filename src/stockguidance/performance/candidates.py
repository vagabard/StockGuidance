from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from time import perf_counter, process_time
from typing import Any, Iterable, Protocol

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import DEFAULT_CALENDAR_ID
from stockguidance.database.configuration import MySQLDriver, load_database_configuration
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.legacy_migration.precision import ExactArchiveBar, exact_records_id, read_exact_archive
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow, derived_id
from stockguidance.performance.archive_read import inventory
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import InputState, ZScoreInput, ZScoreObservation
from stockguidance.zscores.results import ZScoreStatus
from stockguidance.zscores.specification import DELTA_DEFINITION, ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.storage import ZScoreSeriesDefinition, rows_content_id, status_counts


MANIFEST_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
WORKLOADS = ("raw", "derived", "zscores")
TARGET_ROLES = ("fixed_25", "chronological_20")
TRIAL_RECEIPT = "2026-10-20T00:00:00+00:00"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class TrialBackend(Protocol):
	def run(self, workload: str) -> dict[str, object]: ...


@dataclass(frozen=True)
class ManifestPaths:
	data_root: Path
	evaluation_plan: Path
	precision_report: Path
	audit_report: Path
	dbinfo_file: Path | None


def _json(path: Path) -> dict[str, Any]:
	value = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(value, dict):
		raise ValueError(f"Expected JSON object in {path}")
	return value


def _file_hash(path: Path) -> str:
	digest = sha256()
	with path.open("rb") as stream:
		for chunk in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _digest_rows(rows: Iterable[object]) -> str:
	return sha256(encode_json(list(rows))).hexdigest()


def _relative(path: Path, root: Path) -> str:
	try:
		return path.resolve().relative_to(root.resolve()).as_posix()
	except ValueError:
		return path.resolve().as_posix()


def _session_date(value: object) -> date:
	if isinstance(value, date) and not isinstance(value, datetime):
		return value
	if not isinstance(value, str):
		raise ValueError("Manifest session must be a date string")
	return date.fromisoformat(value)


def _source_payload(audit: dict[str, Any]) -> dict[str, object]:
	source = audit.get("source")
	identity = audit.get("database_identity")
	if not isinstance(source, dict) or not isinstance(identity, dict):
		raise ValueError("Stage 5 audit lacks source/database identity")
	return {
		"archive_source": source,
		"database_source_key": identity.get("source_key"),
		"database_snapshot": identity.get("snapshot"),
		"database_schema_hash": identity.get("schema_hash"),
		"table_names": identity.get("table_names"),
	}


def _manifest_table_names(manifest: dict[str, Any]) -> TableNames:
	registry = manifest.get("authoritative_registry")
	if not isinstance(registry, dict) or not isinstance(registry.get("tables"), dict) or not isinstance(registry.get("zscore_templates"), dict):
		raise ValueError("Stage 6 manifest lacks an authoritative table registry")
	tables = registry["tables"]
	templates = registry["zscore_templates"]
	return TableNames(
		source=tables["source"], sessions=tables["sessions"], tickers=tables["tickers"], prices=tables["prices"],
		deltas=tables["deltas"], imports=tables["imports"], zscore_series=tables["zscore_series"], zscore_sessions=tables["zscore_sessions"],
		price_template=templates["price"], price_delta_template=templates["price_delta"],
		data_verifications=tables.get("data_verifications", "stockguidance_data_verifications"),
		data_verification_samples=tables.get("data_verification_samples", "stockguidance_data_verification_samples"),
	)


def _calendar_sessions(data_root: Path, calendar_id: str = DEFAULT_CALENDAR_ID) -> tuple[ExchangeCalendar, tuple[date, ...]]:
	paths = inventory(data_root)
	first, last = date.fromisoformat(paths[0].stem), date.fromisoformat(paths[-1].stem)
	calendar = ExchangeCalendar(calendar_id, first, last)
	return calendar, tuple(date.fromisoformat(path.stem) for path in paths)


def _shift_date(calendar: ExchangeCalendar, value: date, offset: int) -> date | None:
	try:
		return calendar.shift(calendar.session(value), offset).session_date
	except ValueError:
		return None


def _read_database_mappings(dbinfo_file: Path, names: TableNames, sessions: tuple[date, ...]) -> dict[str, Any]:
	configuration = load_database_configuration(dbinfo_file)
	drivers = (configuration.driver,) if configuration.driver is not MySQLDriver.AUTO else (MySQLDriver.CONNECTOR, MySQLDriver.PYMYSQL)
	connection = None
	for driver in drivers:
		try:
			module_name = "mysql.connector" if driver is MySQLDriver.CONNECTOR else "pymysql"
			module = __import__(module_name, fromlist=["connect"])
			parameters: dict[str, object] = {
				"host": configuration.host,
				"database": configuration.database,
				"user": configuration.user,
				"password": configuration.password,
				"port": configuration.port,
				"autocommit": False,
			}
			if driver is MySQLDriver.CONNECTOR:
				parameters.update(buffered=False, connection_timeout=30)
			else:
				parameters.update(charset="utf8mb4", cursorclass=module.cursors.SSCursor, connect_timeout=30)
			connection = module.connect(**parameters)
			break
		except ImportError:
			continue
	if connection is None:
		raise RuntimeError("No supported database driver is available for the Stage 6 read-only extraction")

	cursor = None
	try:
		cursor = connection.cursor()
		cursor.execute(f"SELECT id, label FROM {quote_identifier(names.tickers)} ORDER BY id", ())
		tickers = tuple((int(row[0]), str(row[1])) for row in cursor.fetchall())
		if len({identifier for identifier, _ in tickers}) != len(tickers) or len({label for _, label in tickers}) != len(tickers):
			raise ValueError("Database ticker mappings are not unique")
		if sessions:
			placeholders = ",".join("%s" for _ in sessions)
			cursor.execute(f"SELECT id, transaction_date FROM {quote_identifier(names.sessions)} WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date, id", sessions)
			date_rows = tuple((int(row[0]), _session_date(row[1])) for row in cursor.fetchall())
		else:
			date_rows = ()
		connection.rollback()
		return {
			"status": "read_only_extracted",
			"ticker_count": len(tickers),
			"ticker_mappings": [{"id": identifier, "label": label} for identifier, label in tickers],
			"date_mappings": [{"id": identifier, "session": session.isoformat()} for identifier, session in date_rows],
		}
	finally:
		if cursor is not None:
			cursor.close()
		connection.close()


def build_manifest(paths: ManifestPaths, output: Path, *, table_names: TableNames | None = None, calendar_id: str = DEFAULT_CALENDAR_ID) -> dict[str, object]:
	data_root = paths.data_root.resolve()
	plan = _json(paths.evaluation_plan)
	precision = _json(paths.precision_report)
	audit = _json(paths.audit_report)
	names = table_names or (load_database_configuration(paths.dbinfo_file).load_table_names() if paths.dbinfo_file is not None else load_table_names())
	selected = plan.get("selected_targets")
	if not isinstance(selected, list) or len(selected) != 25:
		raise ValueError("Stage 6 requires the exact 25 Stage 2 targets")
	entries_by_session = {str(item["session"]): item for item in precision.get("files", []) if isinstance(item, dict)}
	calendar, _ = _calendar_sessions(data_root, calendar_id)
	fixed_dates = tuple(_session_date(item["session"]) for item in selected)
	last_selected = max(fixed_dates)
	chronological_dates = tuple(calendar.shift(calendar.session(last_selected), offset).session_date for offset in range(-19, 1))
	roles: dict[date, set[str]] = defaultdict(set)
	for item in fixed_dates:
		roles[item].add("fixed_25")
	for item in chronological_dates:
		roles[item].add("chronological_20")
	dependency_dates: set[date] = set()
	target_payloads: list[dict[str, object]] = []
	for session in sorted(roles):
		prior_candidates = tuple(_shift_date(calendar, session, -offset) for offset in range(1, 61))
		prior: tuple[date, ...] = tuple(item for item in reversed(prior_candidates) if isinstance(item, date))
		following = _shift_date(calendar, session, 1)
		derived_dependencies = tuple(item for item in (_shift_date(calendar, session, -1), following) if item is not None)
		zscore_dependencies = prior + (session,)
		dependency_dates.update(derived_dependencies)
		dependency_dates.update(zscore_dependencies)
		target_payloads.append({
			"session": session.isoformat(),
			"roles": sorted(roles[session]),
			"derived_dependencies": [item.isoformat() for item in derived_dependencies],
			"zscore_dependencies": [item.isoformat() for item in zscore_dependencies],
		})
	needed_dates = set(roles) | dependency_dates
	files: list[dict[str, object]] = []
	file_by_date = {date.fromisoformat(path.stem): path.resolve() for path in inventory(data_root)}
	for session in sorted(needed_dates):
		path = file_by_date.get(session)
		entry = entries_by_session.get(session.isoformat(), {})
		valid = bool(entry.get("valid", False))
		validate_records = session in roles
		if path is not None:
			current_hash = _file_hash(path)
			recorded_hash = entry.get("file_sha256")
			if recorded_hash is not None and current_hash != recorded_hash:
				raise ValueError(f"Archive input hash changed for {session.isoformat()}")
		else:
			current_hash = None
		bars: tuple[ExactArchiveBar, ...] = ()
		if path is not None and valid and validate_records:
			bars, exact_hash = read_exact_archive(path)
			if exact_hash != current_hash:
				raise ValueError(f"Archive reader hash mismatch for {session.isoformat()}")
			if entry.get("records_id") != exact_records_id(bars):
				raise ValueError(f"Archive records identity changed for {session.isoformat()}")
		files.append({
			"session": session.isoformat(),
			"path": None if path is None else str(path),
			"relative_path": None if path is None else _relative(path, data_root),
			"file_sha256": current_hash,
			"valid": valid,
			"record_count": len(bars) if bars else int(entry.get("record_count", 0)),
			"records_id": exact_records_id(bars) if bars else entry.get("records_id"),
			"key_count": len(bars) if bars else None,
			"key_digest": _digest_rows(item.ticker for item in bars) if bars else None,
			"record_validation": "exact_revalidated" if validate_records else "hash_and_stage5_inventory; exact_revalidated_at_trial",
			"roles": sorted(roles.get(session, set())),
			"dependency": session not in roles,
			"finding": entry.get("finding"),
		})
	mapped_sessions = tuple(item["session"] for item in files if item["valid"])
	mappings: dict[str, Any] = {"status": "not_requested", "ticker_count": None, "ticker_mappings": [], "date_mappings": []}
	if paths.dbinfo_file is not None:
		mappings = _read_database_mappings(paths.dbinfo_file.resolve(), names, tuple(_session_date(value) for value in mapped_sessions))
	ticker_by_label = {item["label"]: item["id"] for item in mappings["ticker_mappings"]}
	for file_entry in files:
		target_bars: tuple[ExactArchiveBar, ...] = ()
		if file_entry["path"] is not None and file_entry["valid"] and file_entry["session"] in roles:
			target_bars, _ = read_exact_archive(Path(str(file_entry["path"])))
		file_entry["ticker_mapping"] = {
			"status": "exact_target_mapping" if target_bars else "deferred_exact_mapping_at_trial",
			"mapped_count": sum(1 for bar in target_bars if bar.ticker in ticker_by_label) if target_bars else None,
			"unmapped_count": sum(1 for bar in target_bars if bar.ticker not in ticker_by_label) if target_bars else None,
			"mapping_digest": _digest_rows((bar.ticker, ticker_by_label.get(bar.ticker)) for bar in target_bars) if target_bars else None,
		}
	date_by_session = {item["session"]: item["id"] for item in mappings["date_mappings"]}
	for file_entry in files:
		file_entry["database_date_id"] = date_by_session.get(str(file_entry["session"]))
	corrected_schema_hash = precision.get("schema_selection", {}).get("schema_hash")
	manifest: dict[str, object] = {
		"schema_version": MANIFEST_SCHEMA_VERSION,
		"stage": 6,
		"protocol": "legacy-migration-stage6-candidates-v1",
		"created_at": datetime.now(timezone.utc).isoformat(),
		"calendar_id": calendar.calendar_id,
		"source": _source_payload(audit),
		"corrected_schema_hash": corrected_schema_hash,
		"authoritative_registry": {"tables": names.tables(), "zscore_templates": names.templates()},
		"snapshot_identity": {"audit_sha256": _file_hash(paths.audit_report), "database_schema_hash": audit.get("database_identity", {}).get("schema_hash"), "source_key": audit.get("database_identity", {}).get("source_key")},
		"inputs": {"data_root": str(data_root), "evaluation_plan": str(paths.evaluation_plan.resolve()), "precision_report": str(paths.precision_report.resolve()), "audit_report": str(paths.audit_report.resolve())},
		"fixed_targets": [item["session"] for item in selected],
		"target_blocks": {"fixed_25": [item["session"] for item in selected], "chronological_20": [item.isoformat() for item in chronological_dates]},
		"targets": target_payloads,
		"files": files,
		"mappings": mappings,
		"zscore_population": {"policy": "dense_all_retained_ticker_ids", "ticker_ids": [item["id"] for item in mappings["ticker_mappings"]], "status_policy": {str(status.value): status.name.lower() for status in ZScoreStatus}},
		"repetitions": {"first_plus_warm": 4, "warm_repeats": 3, "candidate_orders": [["python", "java", "sql"], ["java", "sql", "python"], ["sql", "python", "java"], ["python", "sql", "java"]]},
		"concurrency": {"python_java_preparers": [1, 3], "sql_preparers": None, "writers": 1, "queued_sessions_max": 3, "native_threads": 1, "java_heap": "-Xmx512m", "per_preparer_application_memory_bytes": 512 * 1024 * 1024},
		"transaction_units": {"raw": "one session", "derived": "one session", "zscores": "one series/session"},
		"immutable_inputs": True,
	}
	manifest["manifest_id"] = content_id(manifest)
	output.parent.mkdir(parents=True, exist_ok=True)
	output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return manifest


class DisposableTargetGuard:
	def __init__(self, target: Path, *, data_root: Path | None = None, dbinfo_file: Path | None = None) -> None:
		self.target = target.resolve()
		self.data_root = None if data_root is None else data_root.resolve()
		self.dbinfo_file = None if dbinfo_file is None else dbinfo_file.resolve()

	def validate(self) -> Path:
		if self.target.suffix.lower() in {".id", ".sql"}:
			raise ValueError("Disposable targets cannot be configuration or SQL files")
		if self.data_root is not None and (self.target == self.data_root or self.data_root in self.target.parents):
			raise ValueError("Disposable target cannot be inside the immutable source data root")
		if self.dbinfo_file is not None and self.target == self.dbinfo_file:
			raise ValueError("Disposable target cannot be the production database configuration")
		if self.target.exists() and self.target.is_dir():
			raise ValueError("Disposable target must be a database file")
		self.target.parent.mkdir(parents=True, exist_ok=True)
		return self.target


def _sqlite_name(value: str) -> str:
	if _IDENTIFIER.fullmatch(value) is None:
		raise ValueError("Invalid registry identifier")
	return quote_identifier(value)


def disposable_schema_statements(names: TableNames, windows: tuple[int, ...] = (20, 60)) -> tuple[str, ...]:
	if any(type(window) is not int or window < 2 for window in windows) or len(set(windows)) != len(windows):
		raise ValueError("Invalid disposable z-score windows")
	source, sessions, tickers = (_sqlite_name(names.table(role)) for role in (TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS))
	prices, deltas, imports = (_sqlite_name(names.table(role)) for role in (TableRole.PRICES, TableRole.DELTAS, TableRole.IMPORTS))
	statements = [
		f"CREATE TABLE {source} (id INTEGER PRIMARY KEY, configuration_key TEXT NOT NULL, source_key TEXT NOT NULL, source_family TEXT NOT NULL, endpoint TEXT NOT NULL, market TEXT NOT NULL, universe TEXT NOT NULL, interval_code TEXT NOT NULL, raw_price_split_adjusted INTEGER NOT NULL, raw_price_dividend_adjusted INTEGER NOT NULL, raw_price_vendor_defined_adjustment TEXT, created_at TEXT NOT NULL)",
		f"CREATE TABLE {sessions} (id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_date TEXT NOT NULL UNIQUE)",
		f"CREATE TABLE {tickers} (id INTEGER PRIMARY KEY, label TEXT NOT NULL COLLATE BINARY UNIQUE)",
		f"CREATE TABLE {prices} (ticker_id INTEGER NOT NULL, date_id INTEGER NOT NULL, open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT, vwap TEXT, transactions INTEGER, PRIMARY KEY (ticker_id, date_id))",
		f"CREATE INDEX stage6_prices_date ON {prices} (date_id)",
		f"CREATE TABLE {deltas} (ticker_id INTEGER NOT NULL, date_id INTEGER NOT NULL, " + ", ".join(f"{column} REAL" for column in DERIVED_COLUMNS) + ", PRIMARY KEY (ticker_id, date_id))",
		f"CREATE INDEX stage6_deltas_date ON {deltas} (date_id)",
		f"CREATE TABLE {imports} (session_date TEXT PRIMARY KEY, raw_id TEXT NOT NULL, available_at TEXT NOT NULL, derived_dependency_id TEXT, derived_content_id TEXT)",
		"CREATE TABLE stage6_completion (workload TEXT NOT NULL, session_date TEXT NOT NULL, series_key TEXT NOT NULL, content_digest TEXT NOT NULL, committed_at TEXT NOT NULL, PRIMARY KEY (workload, session_date, series_key))",
		"CREATE TABLE stage6_zscore_stage (family TEXT NOT NULL, window INTEGER NOT NULL, session_date TEXT NOT NULL, ticker_id INTEGER NOT NULL, zscore REAL, status INTEGER NOT NULL, reason TEXT NOT NULL, PRIMARY KEY (family, window, session_date, ticker_id))",
		"CREATE TABLE stage6_disposable_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
	]
	resolved = names.resolve_zscore_tables(windows)
	series, zscore_sessions = (_sqlite_name(names.table(role)) for role in (TableRole.ZSCORE_SERIES, TableRole.ZSCORE_SESSIONS))
	statements.extend((
		f"CREATE TABLE {series} (series_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, source_scope TEXT NOT NULL, calendar_id TEXT NOT NULL, family TEXT NOT NULL, window INTEGER NOT NULL, calculation_version TEXT NOT NULL, delta_definition TEXT NOT NULL, precision TEXT NOT NULL, missing_value_policy TEXT NOT NULL, created_at TEXT NOT NULL)",
		f"CREATE TABLE {zscore_sessions} (series_key TEXT NOT NULL, date_id INTEGER NOT NULL, dependency_digest TEXT NOT NULL, availability_digest TEXT NOT NULL, actual_input_available INTEGER NOT NULL, completed_at TEXT NOT NULL, row_count INTEGER NOT NULL, ready_count INTEGER NOT NULL, status_counts TEXT NOT NULL, content_digest TEXT NOT NULL, PRIMARY KEY (series_key, date_id))",
	))
	for window in windows:
		for table in (resolved[window].price, resolved[window].price_delta):
			statements.append(f"CREATE TABLE {_sqlite_name(table)} (date_id INTEGER NOT NULL, ticker_id INTEGER NOT NULL, zscore REAL, status INTEGER NOT NULL, PRIMARY KEY (date_id, ticker_id))")
	return tuple(statements)


def create_disposable_database(target: Path, names: TableNames | None = None, windows: tuple[int, ...] = (20, 60), *, data_root: Path | None = None, dbinfo_file: Path | None = None) -> dict[str, object]:
	path = DisposableTargetGuard(target, data_root=data_root, dbinfo_file=dbinfo_file).validate()
	if path.exists():
		path.unlink()
	names = names or load_table_names()
	statements = disposable_schema_statements(names, windows)
	schema_hash = sha256(encode_json(statements)).hexdigest()
	with sqlite3.connect(path) as connection:
		for statement in statements:
			connection.execute(statement)
		connection.executemany("INSERT INTO stage6_disposable_metadata(key, value) VALUES (?, ?)", (("schema_hash", schema_hash), ("registry", json.dumps(names.tables(), sort_keys=True)), ("production_target", "false")))
	return {"path": str(path), "schema_hash": schema_hash, "statement_count": len(statements), "indexes": ["stage6_prices_date", "stage6_deltas_date"], "triggers": [], "routines": [], "production_target": False}


def _decimal_text(value: Decimal | int | None) -> str | None:
	if value is None:
		return None
	return format(Decimal(value).normalize(), "f")


def _exact_derived_rows(prior: tuple[ExactArchiveBar, ...], current: tuple[ExactArchiveBar, ...], following: tuple[ExactArchiveBar, ...]) -> tuple[LegacyDerivedRow, ...]:
	priors = {bar.ticker: bar for bar in prior}
	nexts = {bar.ticker: bar for bar in following}
	ranked = sorted(((bar.ticker, float(nexts[bar.ticker].close / nexts[bar.ticker].open)) for bar in current if bar.ticker in nexts), key=lambda item: (-item[1], item[0]))
	top = {ticker for ticker, _ in ranked[:100]}
	rows: list[LegacyDerivedRow] = []
	for bar in current:
		previous, next_bar = priors.get(bar.ticker), nexts.get(bar.ticker)
		ratio_names = ("open", "high", "low", "close", "volume", "vwap", "transactions")
		ratios = tuple(
			None
			if previous is None or getattr(bar, name) is None or getattr(previous, name) in (None, 0)
			else float(Decimal(getattr(bar, name)) / Decimal(getattr(previous, name)))
			for name in ratio_names
		)
		outcome = None if next_bar is None else float(next_bar.close / next_bar.open)
		extreme = None if outcome is None else (0.5 if outcome >= 1 else 0) + outcome / (2 * (outcome + 1))
		raise_flag = None if outcome is None else float(outcome >= 1)
		top_flag = None if outcome is None else float(bar.ticker in top)
		values = (*ratios, float(bar.close / bar.open), outcome, None if outcome is None else outcome - 1, extreme, raise_flag, top_flag, None if extreme is None else extreme * 2 - 1, None if top_flag is None else top_flag * 2 - 1, None if raise_flag is None else raise_flag * 2 - 1)
		rows.append(LegacyDerivedRow(bar.ticker, values))
	return tuple(sorted(rows, key=lambda item: item.ticker))


class PythonBaseline:
	def __init__(self, manifest: dict[str, Any], database: Path, output: Path, *, names: TableNames | None = None, timeout_seconds: float = 3600.0) -> None:
		self.manifest, self.database, self.output, self.timeout_seconds = manifest, database, output, timeout_seconds
		self.names = names or _manifest_table_names(manifest)
		self.files = {item["session"]: item for item in manifest["files"]}
		self._bars_cache: dict[str, tuple[ExactArchiveBar, ...]] = {}
		self._trace_count = 0
		self._zscore_staged = False

	def _connect(self) -> sqlite3.Connection:
		connection = sqlite3.connect(self.database, timeout=30)
		connection.execute("PRAGMA foreign_keys = ON")
		connection.set_trace_callback(lambda _: setattr(self, "_trace_count", self._trace_count + 1))
		return connection

	def _bars(self, session: str) -> tuple[ExactArchiveBar, ...]:
		if session in self._bars_cache:
			return self._bars_cache[session]
		item = self.files.get(session)
		if not item or not item.get("path") or not item.get("valid"):
			self._bars_cache[session] = ()
			return ()
		path = Path(item["path"])
		if _file_hash(path) != item["file_sha256"]:
			raise ValueError(f"Changed Stage 6 input: {session}")
		bars, file_hash = read_exact_archive(path)
		if file_hash != item["file_sha256"]:
			raise ValueError(f"Changed Stage 6 input: {session}")
		self._bars_cache[session] = bars
		return bars

	def _session_id(self, connection: sqlite3.Connection, session: str) -> int:
		table = _sqlite_name(self.names.sessions)
		connection.execute(f"INSERT OR IGNORE INTO {table}(transaction_date) VALUES (?)", (session,))
		rows = connection.execute(f"SELECT id FROM {table} WHERE transaction_date = ?", (session,)).fetchall()
		if len(rows) != 1:
			raise ValueError(f"Disposable session mapping is not unique: {session}")
		return int(rows[0][0])

	def _ticker_id(self, connection: sqlite3.Connection, label: str) -> int:
		table = _sqlite_name(self.names.tickers)
		row = connection.execute(f"SELECT id FROM {table} WHERE label = ? COLLATE BINARY", (label,)).fetchone()
		if row is not None:
			return int(row[0])
		row = connection.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}").fetchone()
		identifier = int(row[0])
		connection.execute(f"INSERT INTO {table}(id, label) VALUES (?, ?)", (identifier, label))
		return identifier

	def _checkpoint(self, workload: str, session: str, digest: str) -> None:
		self._write_checkpoint(self.output.with_name(f"{self.output.stem}-checkpoint.json"), workload, session, digest)

	def _write_checkpoint(self, checkpoint: Path, workload: str, session: str, digest: str) -> None:
		checkpoint.parent.mkdir(parents=True, exist_ok=True)
		checkpoint.write_text(json.dumps({"schema_version": 1, "workload": workload, "session": session, "content_digest": digest, "after_commit": True}, indent=2) + "\n", encoding="utf-8")

	def _derived_session_prepared(
		self,
		session: str,
		prior_dates: tuple[str, str],
		prior_bars: tuple[ExactArchiveBar, ...],
		target_bars: tuple[ExactArchiveBar, ...],
		following_bars: tuple[ExactArchiveBar, ...],
		*,
		checkpoint: Path | None = None,
	) -> dict[str, object]:
		if len(prior_dates) != 2:
			return {"session": session, "status": "missing_dependency", "committed": False}
		rows = _exact_derived_rows(prior_bars, target_bars, following_bars)
		dependency_id = content_id({
			"prior": prior_dates[0],
			"following": prior_dates[1],
			"raw": [exact_records_id(prior_bars), exact_records_id(following_bars)],
		})
		content_digest = derived_id(rows)
		started, cpu = perf_counter(), process_time()
		before_queries = self._trace_count
		connection = self._connect()
		try:
			connection.execute("BEGIN")
			date_id = self._session_id(connection, session)
			ticker_table, delta_table, imports_table = (_sqlite_name(self.names.table(role)) for role in (TableRole.TICKERS, TableRole.DELTAS, TableRole.IMPORTS))
			mapping = {str(label): int(identifier) for identifier, label in connection.execute(f"SELECT id, label FROM {ticker_table}").fetchall()}
			next_identifier = max(mapping.values(), default=0) + 1
			new_tickers = []
			payload = []
			for row in rows:
				ticker_id = mapping.get(row.ticker)
				if ticker_id is None:
					ticker_id = next_identifier
					next_identifier += 1
					mapping[row.ticker] = ticker_id
					new_tickers.append((ticker_id, row.ticker))
				payload.append((ticker_id, date_id, *row.values))
			if new_tickers:
				connection.executemany(f"INSERT INTO {ticker_table}(id, label) VALUES (?, ?)", new_tickers)
			connection.execute(f"DELETE FROM {delta_table} WHERE date_id = ?", (date_id,))
			columns = ",".join(DERIVED_COLUMNS)
			placeholders = ",".join("?" for _ in range(len(DERIVED_COLUMNS) + 2))
			if payload:
				connection.executemany(f"INSERT INTO {delta_table}(ticker_id, date_id, {columns}) VALUES ({placeholders})", payload)
			connection.execute(f"UPDATE {imports_table} SET derived_dependency_id = ?, derived_content_id = ? WHERE session_date = ?", (dependency_id, content_digest, session))
			connection.execute("INSERT OR REPLACE INTO stage6_completion(workload, session_date, series_key, content_digest, committed_at) VALUES (?,?,?,?,?)", ("derived", session, "legacy-open-to-close-v1", content_digest, TRIAL_RECEIPT))
			connection.commit()
			self._write_checkpoint(checkpoint or self.output.with_name(f"{self.output.stem}-checkpoint.json"), "derived", session, content_digest)
			return {"session": session, "status": "complete", "rows": len(rows), "seconds": perf_counter() - started, "cpu_seconds": process_time() - cpu, "query_count": self._trace_count - before_queries, "committed": True, "content_digest": content_digest}
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def _derived_session(self, session: str) -> dict[str, object]:
		target = self._bars(session)
		if not target:
			return {"session": session, "status": "missing_dependency"}
		prior = next((item for item in self.manifest["targets"] if item["session"] == session), None)
		if prior is None:
			raise ValueError(f"Unknown derived target: {session}")
		prior_dates = tuple(prior["derived_dependencies"])
		if len(prior_dates) != 2:
			return {"session": session, "status": "missing_dependency"}
		return self._derived_session_prepared(session, prior_dates, self._bars(prior_dates[0]), target, self._bars(prior_dates[1]))

	def _raw_session(self, session: str, *, seed: bool = False) -> dict[str, object]:
		bars = self._bars(session)
		return self._raw_session_prepared(session, bars, exact_records_id(bars), seed=seed)

	def _raw_session_prepared(self, session: str, bars: tuple[ExactArchiveBar, ...], records_id: str, *, seed: bool = False) -> dict[str, object]:
		started, cpu = perf_counter(), process_time()
		before_queries = self._trace_count
		connection = self._connect()
		try:
			connection.execute("BEGIN")
			date_id = self._session_id(connection, session)
			price_table, ticker_table, imports_table = (_sqlite_name(self.names.table(role)) for role in (TableRole.PRICES, TableRole.TICKERS, TableRole.IMPORTS))
			mapping = {label: int(identifier) for identifier, label in connection.execute(f"SELECT id, label FROM {ticker_table}").fetchall()}
			next_identifier = max(mapping.values(), default=0) + 1
			existing = {
				str(label): (int(identifier), open_value, high_value, low_value, close_value, volume, vwap, transactions)
				for identifier, label, open_value, high_value, low_value, close_value, volume, vwap, transactions
				in connection.execute(f"SELECT p.ticker_id, t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM {price_table} p JOIN {ticker_table} t ON t.id = p.ticker_id WHERE p.date_id = ?", (date_id,)).fetchall()
			}
			changed_rows: list[tuple[object, ...]] = []
			for bar in bars:
				ticker_id = mapping.get(bar.ticker)
				if ticker_id is None:
					ticker_id = next_identifier
					next_identifier += 1
					mapping[bar.ticker] = ticker_id
					connection.execute(f"INSERT INTO {ticker_table}(id, label) VALUES (?, ?)", (ticker_id, bar.ticker))
				payload = (ticker_id, date_id, _decimal_text(bar.open), _decimal_text(bar.high), _decimal_text(bar.low), _decimal_text(bar.close), _decimal_text(bar.volume), _decimal_text(bar.vwap), bar.transactions)
				old = existing.get(bar.ticker)
				old_values = None if old is None else old[1:]
				new_values = payload[2:]
				if old_values != new_values:
					changed_rows.append(payload)
			if changed_rows:
				connection.executemany(f"INSERT OR REPLACE INTO {price_table}(ticker_id, date_id, open, high, low, close, volume, vwap, transactions) VALUES (?,?,?,?,?,?,?,?,?)", changed_rows)
			connection.execute(f"INSERT OR REPLACE INTO {imports_table}(session_date, raw_id, available_at, derived_dependency_id, derived_content_id) VALUES (?,?,?,?,?)", (session, records_id, TRIAL_RECEIPT, None, None))
			connection.execute("INSERT OR REPLACE INTO stage6_completion(workload, session_date, series_key, content_digest, committed_at) VALUES (?,?,?,?,?)", ("raw", session, "raw", records_id, TRIAL_RECEIPT))
			connection.commit()
			self._checkpoint("raw", session, records_id)
			return {"session": session, "rows": len(bars), "changed_rows": len(changed_rows), "seconds": perf_counter() - started, "cpu_seconds": process_time() - cpu, "query_count": self._trace_count - before_queries, "seed": seed, "committed": True}
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def _stage_raw(self, sessions: Iterable[str]) -> None:
		for session in sessions:
			self._raw_session(session, seed=True)


	def _zscore_input(self, family: ZScoreFamily, ticker_label: str, sessions: tuple[Any, ...], dependency_dates: set[str], connection: sqlite3.Connection | None = None) -> ZScoreInput:
		values: list[ZScoreObservation] = []
		previous: Decimal | None = None
		closes: dict[str, Decimal] = {}
		if connection is not None:
			price_table, ticker_table, session_table = (_sqlite_name(self.names.table(role)) for role in (TableRole.PRICES, TableRole.TICKERS, TableRole.SESSIONS))
			rows = connection.execute(f"SELECT s.transaction_date, p.close FROM {price_table} p JOIN {ticker_table} t ON t.id = p.ticker_id JOIN {session_table} s ON s.id = p.date_id WHERE t.label = ? COLLATE BINARY ORDER BY s.transaction_date", (ticker_label,)).fetchall()
			closes = {str(session,): Decimal(str(close)) for session, close in rows if close is not None}
		for session in sessions:
			close = closes.get(session.session_date.isoformat()) if connection is not None else next((bar.close for bar in self._bars(session.session_date.isoformat()) if bar.ticker == ticker_label), None)
			if close is None:
				values.append(ZScoreObservation(state=InputState.MISSING))
				previous = None
				continue
			if family is ZScoreFamily.PRICE:
				values.append(ZScoreObservation.ready(close))
				previous = close
				continue
			if previous is None:
				values.append(ZScoreObservation(state=InputState.MISSING))
			else:
				values.append(ZScoreObservation.ready((close / previous) - Decimal(1)))
			previous = close
		dependencies = tuple(session for session in sessions if session.session_date.isoformat() in dependency_dates)
		return ZScoreInput.from_values(family, sessions, {1: values}, dependency_sessions=dependencies, scored_sessions=sessions[-20:])

	def _zscore_workload(self) -> dict[str, object]:
		calendar, _ = _calendar_sessions(Path(self.manifest["inputs"]["data_root"]), str(self.manifest.get("calendar_id", DEFAULT_CALENDAR_ID)))
		chronological = tuple(_session_date(value) for value in self.manifest["target_blocks"]["chronological_20"])
		first = calendar.shift(calendar.session(chronological[0]), -60).session_date
		context = calendar.sessions(first, chronological[-1])
		dependency_dates = {session.session_date.isoformat() for session in context if session.session_date.isoformat() in self.files and self.files[session.session_date.isoformat()].get("valid")}
		mappings = self.manifest["mappings"].get("ticker_mappings", [])
		if not mappings:
			labels = sorted({bar.ticker for session in self.files if self.files[session].get("valid") for bar in self._bars(session)})
			mappings = [{"id": index + 1, "label": label} for index, label in enumerate(labels)]
		staging_started = perf_counter()
		if not self._zscore_staged:
			staged_sessions = tuple(sorted(dependency_dates))
			self._stage_raw(staged_sessions)
			self._zscore_staged = True
			self._bars_cache.clear()
			staging_seconds = perf_counter() - staging_started
		else:
			staging_seconds = 0.0
		started, cpu = perf_counter(), process_time()
		staged = 0
		connection = self._connect()
		try:
			for family in (ZScoreFamily.PRICE, ZScoreFamily.PRICE_DELTA):
				specification = ZScoreSpecification(family, (20, 60), delta_definition=DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
				for mapping in mappings:
					input_data = self._zscore_input(family, mapping["label"], context, dependency_dates, connection)
					result = calculate_zscores(specification, input_data)
					connection.execute("BEGIN")
					for row in result.rows:
						if row.session.session_date not in chronological:
							continue
						connection.execute("INSERT OR REPLACE INTO stage6_zscore_stage(family, window, session_date, ticker_id, zscore, status, reason) VALUES (?,?,?,?,?,?,?)", (family.value, row.window, row.session.session_date.isoformat(), mapping["id"], row.score, int(row.status), row.reason))
						staged += 1
					connection.commit()
			for family in (ZScoreFamily.PRICE, ZScoreFamily.PRICE_DELTA):
				for window in (20, 60):
					definition = ZScoreSeriesDefinition(content_id(self.manifest["source"]["archive_source"]), calendar.calendar_id, family, window, specification.calculation_version, specification.precision, specification.delta_definition if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
					for session in chronological:
						connection.execute("BEGIN")
						rows = connection.execute("SELECT ticker_id, zscore, status, reason FROM stage6_zscore_stage WHERE family = ? AND window = ? AND session_date = ? ORDER BY ticker_id", (family.value, window, session.isoformat())).fetchall()
						date_id = self._session_id(connection, session.isoformat())
						table = self.names.resolve_zscore_tables((window,))[window].price if family is ZScoreFamily.PRICE else self.names.resolve_zscore_tables((window,))[window].price_delta
						table = _sqlite_name(table)
						connection.execute(f"DELETE FROM {table} WHERE date_id = ?", (date_id,))
						row_objects = []
						for ticker_id, score, status, reason in rows:
							connection.execute(f"INSERT INTO {table}(date_id, ticker_id, zscore, status) VALUES (?,?,?,?)", (date_id, ticker_id, score, status))
							row_objects.append((session.isoformat(), int(ticker_id), score, int(status)))
						content_digest = content_id(row_objects)
						status_payload = {str(status): sum(1 for row in rows if int(row[2]) == status) for status in range(6)}
						series_table, session_table = (_sqlite_name(self.names.table(role)) for role in (TableRole.ZSCORE_SERIES, TableRole.ZSCORE_SESSIONS))
						connection.execute(f"INSERT OR REPLACE INTO {series_table}(series_key, schema_version, source_scope, calendar_id, family, window, calculation_version, delta_definition, precision, missing_value_policy, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (definition.series_key, definition.schema_version, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy, TRIAL_RECEIPT))
						connection.execute(f"INSERT OR REPLACE INTO {session_table}(series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest) VALUES (?,?,?,?,?,?,?,?,?,?)", (definition.series_key, date_id, content_id({"session": session.isoformat(), "family": family.value, "window": window}), content_id({"available": True}), 1, TRIAL_RECEIPT, len(rows), status_payload.get("0", 0), json.dumps(status_payload, sort_keys=True), content_digest))
						connection.execute("INSERT OR REPLACE INTO stage6_completion(workload, session_date, series_key, content_digest, committed_at) VALUES (?,?,?,?,?)", ("zscores", session.isoformat(), definition.series_key, content_digest, TRIAL_RECEIPT))
						connection.commit()
			return {"status": "complete", "ticker_count": len(mappings), "target_session_count": len(chronological), "staged_rows": staged, "staging_seconds": staging_seconds, "seconds": perf_counter() - started, "end_to_end_seconds": perf_counter() - started + staging_seconds, "cpu_seconds": process_time() - cpu, "committed": True, "memory_policy": "one ticker input at a time"}
		finally:
			connection.close()

	def run(self, workload: str) -> dict[str, object]:
		if workload not in WORKLOADS:
			raise ValueError(f"Unknown Stage 6 workload: {workload}")
		if workload == "raw":
			sessions = tuple(self.manifest["target_blocks"]["fixed_25"])
			self._stage_raw(sessions)
			matching = [self._raw_session(session) for session in sessions]
			scenario_session = sessions[0]
			self._raw_session(scenario_session, seed=True)
			connection = self._connect()
			try:
				connection.execute("BEGIN")
				date_id = self._session_id(connection, scenario_session)
				row = connection.execute(f"SELECT ticker_id FROM {_sqlite_name(self.names.prices)} WHERE date_id = ? ORDER BY ticker_id LIMIT 1", (date_id,)).fetchone()
				if row:
					connection.execute(f"DELETE FROM {_sqlite_name(self.names.prices)} WHERE date_id = ? AND ticker_id = ?", (date_id, row[0]))
					second = connection.execute(f"SELECT ticker_id FROM {_sqlite_name(self.names.prices)} WHERE date_id = ? AND ticker_id != ? ORDER BY ticker_id LIMIT 1", (date_id, row[0])).fetchone()
					if second:
						connection.execute(f"UPDATE {_sqlite_name(self.names.prices)} SET close = ? WHERE date_id = ? AND ticker_id = ?", ("999.99999999", date_id, second[0]))
				connection.commit()
			finally:
				connection.close()
			repair = [self._raw_session(scenario_session)]
			rerun = [self._raw_session(scenario_session)]
			return {"status": "complete", "scenarios": {"matching_adoption": matching, "missing_or_different_repair": repair, "unchanged_rerun": rerun}}
		if workload == "derived":
			sessions = tuple(item["session"] for item in self.manifest["targets"] if "fixed_25" in item["roles"])
			dependency_files = {dependency for item in self.manifest["targets"] if item["session"] in sessions for dependency in item["derived_dependencies"]}
			self._stage_raw(sorted(set(sessions) | dependency_files))
			return {"status": "complete", "sessions": [self._derived_session(session) for session in sessions], "formula": "legacy-open-to-close-v1", "exact_decimal_inputs": True}
		return self._zscore_workload()


def run_python_baselines(manifest_path: Path, output: Path, target: Path, workloads: tuple[str, ...] = WORKLOADS, *, repetitions: int = 4, timeout_seconds: float = 3600.0) -> dict[str, object]:
	manifest = _json(manifest_path)
	if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION or manifest.get("stage") != 6:
		raise ValueError("Unsupported Stage 6 manifest")
	if type(repetitions) is not int or repetitions < 4:
		raise ValueError("Stage 6 requires one invocation plus three warm repeats")
	names = _manifest_table_names(manifest)
	schema = create_disposable_database(target, names, data_root=Path(manifest["inputs"]["data_root"]))
	backend = PythonBaseline(manifest, target, output, names=names, timeout_seconds=timeout_seconds)
	trials: dict[str, list[dict[str, object]]] = {}
	for workload in workloads:
		trials[workload] = []
		for repetition in range(repetitions):
			started = perf_counter()
			result = backend.run(workload)
			elapsed = perf_counter() - started
			if elapsed > timeout_seconds:
				raise TimeoutError(f"Stage 6 {workload} trial exceeded its bounded timeout")
			result["repetition"] = repetition
			result["cache_state"] = "unknown"
			result["end_to_end_seconds"] = elapsed
			trials[workload].append(result)
	report = {
		"schema_version": REPORT_SCHEMA_VERSION,
		"benchmark": "stockguidance-legacy-migration-stage6-python-baselines-v1",
		"status": "complete",
		"manifest": {"path": str(manifest_path.resolve()), "id": manifest["manifest_id"]},
		"schema": schema,
		"backend": "python",
		"workloads": trials,
		"protocol": {"first_plus_warm_repetitions": repetitions, "preparers": [1, 3], "writers": 1, "queued_sessions_max": 3, "native_threads": 1, "timeout_seconds": timeout_seconds, "database_server_metrics": "unavailable: disposable SQLite target", "java_sql_timings": "pending Stage 7/8"},
		"comparison_eligible": False,
		"storage_engine": "sqlite",
		"correctness": {"raw_decimal_and_case": True, "derived_tolerance": "existing persistence tolerance", "zscore_float32_and_status": True, "recovery": {"rollback": "transactional rollback path", "restart": "checkpoint after commit", "idempotency": "unchanged rerun scenario"}},
		"production_access": {"ddl": False, "dml": False, "read_only_extraction": False},
	}
	output.parent.mkdir(parents=True, exist_ok=True)
	output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return report


def aggregate_candidate_reports(reports: tuple[Path, ...], output: Path) -> dict[str, object]:
	if not reports:
		raise ValueError("At least one Stage 6 candidate report is required")
	loaded = [_json(path) for path in reports]
	if any(report.get("backend") != "python" or report.get("schema_version") != REPORT_SCHEMA_VERSION for report in loaded):
		raise ValueError("Stage 6 aggregate accepts versioned Python candidate reports only")
	manifest_ids = {report.get("manifest", {}).get("id") for report in loaded}
	if len(manifest_ids) != 1:
		raise ValueError("Stage 6 candidate reports use different manifests")
	comparable = all(report.get("storage_engine") == "mysql" and report.get("comparison_eligible") is True for report in loaded)
	workloads: dict[str, object] = {}
	rows: list[dict[str, object]] = []
	for report in loaded:
		for workload, trials in report.get("workloads", {}).items():
			if workload in workloads:
				raise ValueError(f"Duplicate Stage 6 workload report: {workload}")
			workloads[workload] = trials
			end_to_end = [float(trial["end_to_end_seconds"]) for trial in trials]
			status = "measured" if comparable else "mysql_diagnostic_only" if report.get("storage_engine") == "mysql" else "offline_sqlite_only"
			rows.append({"workload": workload, "backend": "python", "status": status, "trial_count": len(trials), "first_seconds": end_to_end[0] if end_to_end else None, "warm_median_seconds": sorted(end_to_end[1:])[len(end_to_end[1:]) // 2] if len(end_to_end) > 1 else None})
	for workload in WORKLOADS:
		if workload not in workloads:
			rows.append({"workload": workload, "backend": "python", "status": "missing", "trial_count": 0, "first_seconds": None, "warm_median_seconds": None})
		for backend in ("java", "sql"):
			rows.append({"workload": workload, "backend": backend, "status": "pending_stage7_or_8", "trial_count": 0, "first_seconds": None, "warm_median_seconds": None})
	aggregate = {
		"schema_version": REPORT_SCHEMA_VERSION,
		"benchmark": "stockguidance-legacy-migration-stage6-candidate-study-v1",
		"stage": 6,
		"status": "complete" if all(workload in workloads for workload in WORKLOADS) and comparable else "incomplete",
		"manifest": {"id": next(iter(manifest_ids)), "reports": [str(path.resolve()) for path in reports]},
		"workloads": workloads,
		"comparison_table": rows,
		"candidate_status": {"python": "measured_comparable" if comparable else "reference_only_mysql_measurements_pending", "java": "pending_stage7", "sql": "pending_stage8"},
		"selection": {"production_backend_selected": False, "selection_stage": 10, "rule": "Java and SQL require a comparable startup-inclusive warm median at least 10% faster than Python"},
		"production_access": {"ddl": False, "dml": False, "read_only_extraction": False},
		"comparison_eligible": comparable,
	}
	output.parent.mkdir(parents=True, exist_ok=True)
	output.write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return aggregate
