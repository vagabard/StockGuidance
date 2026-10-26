from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
import shutil
import subprocess
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, cast

from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.table_names import TableNames, load_table_names, quote_identifier
from stockguidance.database.update_schema import zscore_schema_statements
from stockguidance.legacy_migration.audit import ARCHIVE_SOURCE
from stockguidance.legacy_migration.evidence import ArchiveEvidenceStore
from stockguidance.legacy_migration.precision import ArchiveFileInventory, ExactArchiveBar, exact_records_id, read_exact_archive
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS
from stockguidance.performance.candidates import _exact_derived_rows
from stockguidance.config.source import DEFAULT_CALENDAR_ID


STAGE12_SCHEMA_VERSION = 1
DISPOSABLE_SCHEMA = re.compile(r"stockguidance_disposable_[a-z0-9_]{1,39}\Z")
MIGRATION_RECEIPTS = "stockguidance_migration_receipts"
MIGRATION_COMPLETION = "stockguidance_migration_completion"
PROVENANCE_KIND = "operator_attested_retained_archive"


class Stage12Error(ValueError):
	pass


@dataclass(frozen=True)
class Stage12Context:
	decision_path: Path
	decision: dict[str, Any]
	manifest_path: Path
	manifest: dict[str, Any]
	audit_path: Path
	stage11b_path: Path
	precision_path: Path
	backup_path: Path
	decision_sha256: str
	manifest_sha256: str
	audit_sha256: str
	stage11b_sha256: str
	precision_sha256: str


def _json(path: Path) -> dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
		raise Stage12Error(f"Cannot read JSON artifact: {path}") from error
	if not isinstance(value, dict):
		raise Stage12Error(f"JSON artifact is not an object: {path}")
	return value


def _sha256(path: Path) -> str:
	digest = sha256()
	try:
		with path.open("rb") as stream:
			for chunk in iter(lambda: stream.read(1024 * 1024), b""):
				digest.update(chunk)
	except OSError as error:
		raise Stage12Error(f"Cannot hash required artifact: {path}") from error
	return digest.hexdigest()


def _require_file(path: Path, label: str) -> Path:
	path = path.resolve()
	if not path.is_file():
		raise Stage12Error(f"Missing {label}: {path}")
	return path


def _require_hash(path: Path, expected: object, label: str) -> str:
	actual = _sha256(path)
	if not isinstance(expected, str) or actual != expected:
		raise Stage12Error(f"Changed {label} identity")
	return actual


def _backup_complete(path: Path) -> bool:
	try:
		with path.open("rb") as stream:
			stream.seek(max(0, path.stat().st_size - 4096))
			return b"-- Dump completed on " in stream.read()
	except OSError:
		return False


def validate_prerequisites(
	decision_path: Path,
	manifest_path: Path,
	audit_path: Path,
	stage11b_path: Path,
	precision_path: Path,
	backup_path: Path,
) -> Stage12Context:
	decision_path = _require_file(decision_path, "Stage 10 decision")
	manifest_path = _require_file(manifest_path, "candidate manifest")
	audit_path = _require_file(audit_path, "Stage 5 audit")
	stage11b_path = _require_file(stage11b_path, "Stage 11b handoff")
	precision_path = _require_file(precision_path, "precision inventory")
	backup_path = _require_file(backup_path, "backup")
	decision = _json(decision_path)
	manifest = _json(manifest_path)
	audit = _json(audit_path)
	stage11b = _json(stage11b_path)
	precision = _json(precision_path)
	if decision.get("stage") != 10 or decision.get("status") != "complete" or decision.get("production_ready") is not False:
		raise Stage12Error("Stage 10 decision is incomplete or already marked ready")
	if decision.get("production_backend_selected") is not True or decision.get("no_production_mutations") is not True:
		raise Stage12Error("Stage 10 decision does not carry the production gate")
	if audit.get("complete") is not True or audit.get("database_available") is not True:
		raise Stage12Error("Stage 5 audit is not complete")
	if stage11b.get("stage") != "11b" or stage11b.get("status") != "complete" or stage11b.get("recovery", {}).get("status") != "passed":
		raise Stage12Error("Stage 11b recovery handoff is incomplete")
	if stage11b.get("production_access", {}).get("ddl") is not False or stage11b.get("production_access", {}).get("dml") is not False:
		raise Stage12Error("Stage 11b handoff is not disposable-only")
	inputs = decision.get("inputs", {})
	manifest_sha = _require_hash(manifest_path, inputs.get("manifest", {}).get("sha256"), "manifest")
	audit_sha = _require_hash(audit_path, inputs.get("audit", {}).get("sha256"), "audit")
	if decision.get("schema", {}).get("corrected_schema_hash") != manifest.get("corrected_schema_hash"):
		raise Stage12Error("Stage 10 schema identity is inconsistent")
	if manifest.get("stage") != 6 or manifest.get("schema_version") != 1:
		raise Stage12Error("Candidate manifest is not a valid Stage 6 manifest")
	if precision.get("schema_selection", {}).get("schema_hash") != manifest.get("corrected_schema_hash"):
		raise Stage12Error("Precision inventory does not match the selected schema")
	if not _backup_complete(backup_path):
		raise Stage12Error("Backup completion trailer is missing")
	return Stage12Context(
		decision_path, decision, manifest_path, manifest, audit_path, stage11b_path, precision_path, backup_path,
		_sha256(decision_path), manifest_sha, audit_sha, _sha256(stage11b_path), _sha256(precision_path),
	)


def _connect(dbinfo_file: Path, database: str | None = None) -> Any:
	try:
		import mysql.connector
	except ImportError as error:
		raise Stage12Error("The configured Python interpreter lacks mysql.connector") from error
	configuration = load_database_configuration(dbinfo_file)
	if database is not None:
		configuration = replace(configuration, database=database)
	parameters: dict[str, object] = {
		"host": configuration.host, "port": configuration.port, "user": configuration.user,
		"password": configuration.password, "autocommit": False, "connection_timeout": 20,
	}
	if database is not None:
		parameters["database"] = database
	return mysql.connector.connect(**parameters)


def _quote(name: str) -> str:
	return quote_identifier(name)


def _manifest_names(manifest: dict[str, Any]) -> TableNames:
	registry = manifest.get("authoritative_registry")
	if not isinstance(registry, dict):
		raise Stage12Error("Manifest registry is missing")
	try:
		from stockguidance.database.table_names import _load_payload
		selected = dict(registry)
		selected["schema_version"] = 1
		return _load_payload(json.dumps(selected))
	except ValueError as error:
		raise Stage12Error("Invalid authoritative table registry") from error


def _table_inventory(connection: Any, names: TableNames) -> dict[str, Any]:
	cursor = connection.cursor()
	try:
		cursor.execute("SELECT TABLE_NAME, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, CREATE_OPTIONS FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME", ())
		tables = [list(row) for row in cursor.fetchall()]
		physical = tuple(names.tables(include_verification=True).values())
		placeholders = ",".join("%s" for _ in physical)
		cursor.execute(f"SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA, COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME, ORDINAL_POSITION", physical)
		columns = [list(row) for row in cursor.fetchall()]
		cursor.execute(f"SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, INDEX_TYPE FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX", physical)
		indexes = [list(row) for row in cursor.fetchall()]
		return {"tables": tables, "columns": columns, "indexes": indexes}
	finally:
		cursor.close()


def _mapping_digest(connection: Any, names: TableNames) -> dict[str, Any]:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id, label FROM {_quote(names.tickers)} ORDER BY id", ())
		tickers = [(int(row[0]), str(row[1])) for row in cursor.fetchall()]
		cursor.execute(f"SELECT id, transaction_date FROM {_quote(names.sessions)} ORDER BY id", ())
		dates = [(int(row[0]), row[1].isoformat()) for row in cursor.fetchall()]
		return {"tickers": {"count": len(tickers), "digest": content_id(tickers), "min_id": min((row[0] for row in tickers), default=None), "max_id": max((row[0] for row in tickers), default=None)}, "dates": {"count": len(dates), "digest": content_id(dates), "min_id": min((row[0] for row in dates), default=None), "max_id": max((row[0] for row in dates), default=None)}}
	finally:
		cursor.close()


def _existing_tables(connection: Any, names: TableNames, include_zscores: bool = False) -> set[str]:
	known = set(names.tables(include_verification=True).values())
	if include_zscores:
		known.update((names.zscore_series, names.zscore_sessions))
		for resolved in names.resolve_zscore_tables((20, 60)).values():
			known.update((resolved.price, resolved.price_delta))
	if not known:
		return set()
	placeholders = ",".join("%s" for _ in known)
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders})", tuple(sorted(known)))
		return {str(row[0]) for row in cursor.fetchall()}
	finally:
		cursor.close()


def _counts(connection: Any, names: TableNames, include_zscores: bool = False) -> dict[str, int]:
	tables = [names.tickers, names.sessions, names.prices, names.deltas, names.imports]
	if include_zscores:
		tables += [names.zscore_series, names.zscore_sessions]
		for resolved in names.resolve_zscore_tables((20, 60)).values():
			tables.extend((resolved.price, resolved.price_delta))
	existing = _existing_tables(connection, names, include_zscores)
	cursor = connection.cursor()
	try:
		result: dict[str, int] = {}
		for table in tables:
			if table not in existing:
				continue
			cursor.execute(f"SELECT COUNT(*) FROM {_quote(table)}", ())
			result[table] = int(cursor.fetchone()[0])
		return result
	finally:
		cursor.close()


def _source_binding(connection: Any, names: TableNames) -> list[list[Any]]:
	if names.source not in _existing_tables(connection, names):
		return []
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id, configuration_key, source_key, source_family, endpoint, market, universe, interval_code, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment FROM {_quote(names.source)} ORDER BY id", ())
		return [list(row) for row in cursor.fetchall()]
	finally:
		cursor.close()


def _schema_snapshot(connection: Any, names: TableNames, *, include_zscores: bool = False) -> dict[str, Any]:
	existing = _existing_tables(connection, names, include_zscores)
	known = set(names.tables(include_verification=True).values())
	if include_zscores:
		known.update((names.zscore_series, names.zscore_sessions))
		for resolved in names.resolve_zscore_tables((20, 60)).values():
			known.update((resolved.price, resolved.price_delta))
	return {"inventory": _table_inventory(connection, names), "mapping_identity": _mapping_digest(connection, names), "counts": _counts(connection, names, include_zscores), "source_binding": _source_binding(connection, names), "missing_tables": sorted(known - existing)}


def _disk_snapshot(datadir: str | None) -> dict[str, Any]:
	if not datadir:
		return {"path": None, "total_bytes": None, "free_bytes": None, "used_bytes": None}
	try:
		usage = shutil.disk_usage(datadir)
	except OSError:
		return {"path": datadir, "total_bytes": None, "free_bytes": None, "used_bytes": None}
	return {"path": datadir, "total_bytes": usage.total, "free_bytes": usage.free, "used_bytes": usage.used}


def _server_disk(connection: Any) -> dict[str, Any]:
	cursor = connection.cursor()
	try:
		cursor.execute("SELECT @@datadir", ())
		return _disk_snapshot(cursor.fetchone()[0])
	finally:
		cursor.close()


def _active_writers(connection: Any) -> list[list[Any]]:
	cursor = connection.cursor()
	try:
		cursor.execute("SELECT CONNECTION_ID()", ())
		own = int(cursor.fetchone()[0])
		cursor.execute("SELECT ID, DB, COMMAND, TIME FROM information_schema.PROCESSLIST WHERE DB = DATABASE() AND COMMAND <> 'Sleep' AND ID <> %s ORDER BY ID", (own,))
		return [list(row) for row in cursor.fetchall()]
	finally:
		cursor.close()


def _decimal_limits(connection: Any, names: TableNames) -> dict[str, str | None]:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT MAX(`open`), MAX(`high`), MAX(`low`), MAX(`close`), MAX(volume), MAX(vwap) FROM {_quote(names.prices)}", ())
		row = cursor.fetchone()
		return {name: None if value is None else str(value) for name, value in zip(("open", "high", "low", "close", "volume", "vwap"), row, strict=True)}
	finally:
		cursor.close()



def _migration_ddl(names: TableNames, engines: dict[str, str], inventory: dict[str, Any] | None = None) -> tuple[str, ...]:
	if inventory is None:
		columns = {"open": ("decimal(19,4)", None), "high": ("decimal(19,4)", None), "low": ("decimal(19,4)", None), "close": ("decimal(19,4)", None), "volume": ("bigint unsigned", None), "vwap": ("decimal(19,4)", None), "label": ("varchar(16)", "utf8mb4_0900_ai_ci")}
	else:
		columns = {str(row[1]): (str(row[2]), row[6]) for row in inventory["columns"]}
	statements: tuple[str, ...] = ()
	raw_expected = {"open": "decimal(28,8)", "high": "decimal(28,8)", "low": "decimal(28,8)", "close": "decimal(28,8)", "volume": "decimal(20,8)", "vwap": "decimal(42,22)"}
	if any(str(columns.get(column, (None, None))[0]).casefold() != expected for column, expected in raw_expected.items()):
		statements += (f"ALTER TABLE {_quote(names.prices)} MODIFY `open` DECIMAL(28,8) NOT NULL, MODIFY `high` DECIMAL(28,8) NOT NULL, MODIFY `low` DECIMAL(28,8) NOT NULL, MODIFY `close` DECIMAL(28,8) NOT NULL, MODIFY `volume` DECIMAL(20,8) NOT NULL, MODIFY `vwap` DECIMAL(42,22) NULL",)
	ticker_type, ticker_collation = columns.get("label", (None, None))
	if str(ticker_type).casefold() != "varchar(16)" or str(ticker_collation).casefold() != "utf8mb4_bin":
		statements += (f"ALTER TABLE {_quote(names.tickers)} MODIFY `label` VARCHAR(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL",)
	if engines.get(names.sessions) != "InnoDB":
		statements += (f"ALTER TABLE {_quote(names.sessions)} ENGINE=InnoDB",)
	statements += zscore_schema_statements(names, (20, 60))
	statements += (
		f"CREATE TABLE IF NOT EXISTS {_quote(MIGRATION_RECEIPTS)} (session_date DATE PRIMARY KEY, manifest_id CHAR(64) NOT NULL, source_key CHAR(64) NOT NULL, source_provider VARCHAR(32) NOT NULL, source_path VARCHAR(1024) NOT NULL, file_sha256 CHAR(64) NOT NULL, records_id CHAR(64) NOT NULL, record_count BIGINT NOT NULL, provenance_kind VARCHAR(64) NOT NULL, adopted_at VARCHAR(40) NOT NULL, original_receipt_at VARCHAR(40) NULL, cutoff VARCHAR(40) NOT NULL, evidence_id CHAR(64) NOT NULL, content_digest CHAR(64) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
		f"CREATE TABLE IF NOT EXISTS {_quote(MIGRATION_COMPLETION)} (workload VARCHAR(32) NOT NULL, session_date DATE NOT NULL, series_key VARCHAR(128) NOT NULL, content_digest CHAR(64) NOT NULL, committed_at VARCHAR(40) NOT NULL, PRIMARY KEY (workload, session_date, series_key)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
	)
	return statements


def _schema_engines(inventory: dict[str, Any]) -> dict[str, str]:
	return {str(row[0]): str(row[1]) for row in inventory["tables"] if row[1] is not None}


def _execute_ddl(connection: Any, statements: Iterable[str]) -> list[dict[str, Any]]:
	results = []
	for statement in statements:
		started = perf_counter()
		cursor = connection.cursor()
		try:
			cursor.execute(statement, ())
			results.append({"statement": statement, "seconds": perf_counter() - started, "status": "applied"})
		finally:
			cursor.close()
	return results


def _strip_dump_comments(statement: str) -> str:
	value = statement.lstrip()
	while value.startswith("--"):
		line_end = value.find("\n")
		if line_end < 0:
			return ""
		value = value[line_end + 1:].lstrip()
	return value


def _dump_statements(path: Path) -> Iterable[str]:
	delimiter = ";"
	buffer: list[str] = []
	with path.open("r", encoding="utf-8", errors="strict") as stream:
		for line in stream:
			stripped = line.strip()
			if stripped.upper().startswith("DELIMITER "):
				delimiter = stripped.split(None, 1)[1]
				continue
			if not buffer and stripped.startswith("--"):
				continue
			buffer.append(line)
			line_without_comment = re.sub(r"\s+--.*$", "", line.rstrip())
			if not line_without_comment.endswith(delimiter):
				continue
			statement = "".join(buffer).rstrip()
			buffer = []
			statement = re.sub(r"\s+--[^\r\n]*$", "", statement).rstrip()
			if statement.endswith(delimiter):
				statement = statement[:-len(delimiter)]
			statement = _strip_dump_comments(statement).strip()
			if not statement:
				continue
			statement = re.sub(r"DEFINER\s*=\s*(?:`[^`]+`|[^\s]+)@(?:`[^`]+`|[^\s]+)", "DEFINER=CURRENT_USER", statement, flags=re.IGNORECASE)
			upper = statement.upper()
			if "SET @@GLOBAL.GTID_PURGED" in upper or upper.startswith("CREATE DATABASE") or upper.startswith("USE "):
				continue
			yield statement
	if buffer and _strip_dump_comments("".join(buffer)).strip():
		raise Stage12Error("Backup ended with an unterminated SQL statement")


def _restore_backup(dbinfo_file: Path, backup_path: Path, target_schema: str) -> dict[str, Any]:
	if not DISPOSABLE_SCHEMA.fullmatch(target_schema):
		raise Stage12Error("Restore target must be a distinct disposable schema")
	configuration = load_database_configuration(dbinfo_file)
	if target_schema.casefold() == configuration.database.casefold():
		raise Stage12Error("Restore target must not be production")
	connection = _connect(dbinfo_file)
	started = perf_counter()
	count = 0
	try:
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (target_schema,))
			if cursor.fetchone() is not None:
				raise Stage12Error("Restore target already exists; refusing to drop or overwrite it")
			cursor.execute(f"CREATE DATABASE {_quote(target_schema)} CHARACTER SET utf8mb4 COLLATE utf8mb4_bin", ())
			cursor.execute(f"USE {_quote(target_schema)}", ())
		finally:
			cursor.close()
		for statement in _dump_statements(backup_path):
			cursor = connection.cursor()
			try:
				cursor.execute(statement, ())
			finally:
				cursor.close()
			count += 1
			if count % 1000 == 0:
				connection.commit()
		connection.commit()
	finally:
		connection.close()
	verify = _connect(dbinfo_file, target_schema)
	try:
		cursor = verify.cursor()
		try:
			cursor.execute("SELECT VERSION(), DATABASE(), @@default_storage_engine", ())
			server = list(cursor.fetchone())
		finally:
			cursor.close()
		return {"schema": target_schema, "statement_count": count, "seconds": perf_counter() - started, "server": server, "status": "restored"}
	finally:
		verify.rollback()
		verify.close()


def _bar_matches(old: Iterable[Any], bar: ExactArchiveBar) -> bool:
	new = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)
	return all((left is None and right is None) or (left is not None and right is not None and Decimal(str(left)) == Decimal(str(right))) for left, right in zip(old, new, strict=True))


def _file_item(manifest: dict[str, Any], session: str) -> dict[str, Any]:
	items = [item for item in manifest.get("files", ()) if item.get("session") == session and item.get("valid") is True and item.get("records_id")]
	if len(items) != 1:
		raise Stage12Error(f"Session is not a uniquely validated archive file: {session}")
	return cast(dict[str, Any], items[0])


def _adopt_raw_session(
	connection: Any,
	context: Any,
	names: TableNames,
	session: str,
	adoption_time: str,
	affected_derived: set[str],
	affected_zscores: set[str],
	cutoff: str | None = None,
) -> dict[str, Any]:
	item = _file_item(context.manifest, session)
	path = Path(str(item["path"])).resolve()
	bars, file_hash = read_exact_archive(path)
	records_id = exact_records_id(bars)
	if file_hash != item["file_sha256"] or records_id != item["records_id"]:
		raise Stage12Error(f"Changed archive identity: {session}")
	evidence = ArchiveEvidenceStore(context.decision_path.parent / "evidence").verify(ARCHIVE_SOURCE, session, ArchiveFileInventory(str(path), session, file_hash, True, len(bars), records_id, tuple(bar.ticker for bar in bars)))
	cursor = connection.cursor()
	try:
		connection.start_transaction()
		cursor.execute(f"SELECT id FROM {_quote(names.sessions)} WHERE transaction_date=%s FOR UPDATE", (session,))
		date_rows = cursor.fetchall()
		if len(date_rows) != 1:
			raise Stage12Error(f"Session mapping is not unique: {session}")
		date_id = int(date_rows[0][0])
		cursor.execute(f"SELECT id, label FROM {_quote(names.tickers)} ORDER BY id FOR UPDATE", ())
		ids = {str(row[1]): int(row[0]) for row in cursor.fetchall()}
		if len(ids) != len(set(ids.values())):
			raise Stage12Error("Ticker mapping is not unique")
		cursor.execute(f"SELECT p.ticker_id, t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM {_quote(names.prices)} p JOIN {_quote(names.tickers)} t ON t.id=p.ticker_id WHERE p.date_id=%s FOR UPDATE", (date_id,))
		stored = {str(row[1]): (int(row[0]), *row[2:]) for row in cursor.fetchall()}
		if len(stored) != len(set(stored)):
			raise Stage12Error(f"Raw database keys are not unique: {session}")
		new_ids: list[tuple[int, str]] = []
		next_id = max(ids.values(), default=0) + 1
		for bar in bars:
			if bar.ticker not in ids:
				ids[bar.ticker] = next_id
				new_ids.append((next_id, bar.ticker))
				next_id += 1
		if new_ids:
			cursor.executemany(f"INSERT INTO {_quote(names.tickers)} (id, label) VALUES (%s,%s)", new_ids)
		changed = 0
		bar_labels = {bar.ticker for bar in bars}
		for bar in bars:
			identifier = ids[bar.ticker]
			old = stored.get(bar.ticker)
			if old is not None and _bar_matches(old[1:], bar):
				continue
			values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)
			if old is None:
				cursor.execute(f"INSERT INTO {_quote(names.prices)} (ticker_id,date_id,`open`,`high`,`low`,`close`,volume,vwap,transactions) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (identifier, date_id, *values))
			else:
				cursor.execute(f"UPDATE {_quote(names.prices)} SET `open`=%s,`high`=%s,`low`=%s,`close`=%s,volume=%s,vwap=%s,transactions=%s WHERE ticker_id=%s AND date_id=%s", (*values, identifier, date_id))
			changed += 1
		for label, old in stored.items():
			if label not in bar_labels:
				cursor.execute(f"DELETE FROM {_quote(names.prices)} WHERE ticker_id=%s AND date_id=%s", (old[0], date_id))
				changed += 1
		if changed:
			for affected in sorted(affected_derived):
				cursor.execute(f"SELECT id FROM {_quote(names.sessions)} WHERE transaction_date=%s FOR UPDATE", (affected,))
				rows = cursor.fetchall()
				if len(rows) != 1:
					raise Stage12Error(f"Derived dependency mapping is not unique: {affected}")
				cursor.execute(f"DELETE FROM {_quote(names.deltas)} WHERE date_id=%s", (int(rows[0][0]),))
				cursor.execute(f"UPDATE {_quote(names.imports)} SET derived_dependency_id=NULL,derived_content_id=NULL WHERE session_date=%s", (affected,))
				cursor.execute(f"DELETE FROM {_quote(MIGRATION_COMPLETION)} WHERE workload='derived' AND session_date=%s", (affected,))
			for affected in sorted(affected_zscores):
				cursor.execute(f"SELECT id FROM {_quote(names.sessions)} WHERE transaction_date=%s FOR UPDATE", (affected,))
				rows = cursor.fetchall()
				if len(rows) != 1:
					raise Stage12Error(f"Z-score dependency mapping is not unique: {affected}")
				for tables in names.resolve_zscore_tables((20, 60)).values():
					for table in (tables.price, tables.price_delta):
						cursor.execute(f"DELETE FROM {_quote(table)} WHERE date_id=%s", (int(rows[0][0]),))
				cursor.execute(f"DELETE FROM {_quote(names.zscore_sessions)} WHERE date_id=%s", (int(rows[0][0]),))
		content = content_id({"session": session, "records_id": records_id, "file_sha256": file_hash})
		cursor.execute(f"SELECT raw_id,available_at,derived_dependency_id,derived_content_id FROM {_quote(names.imports)} WHERE session_date=%s FOR UPDATE", (session,))
		old_import = cursor.fetchone()
		available_at = str(old_import[1]) if old_import is not None and old_import[0] == records_id and not changed else adoption_time
		dependency_id = None if changed or old_import is None else old_import[2]
		derived_id = None if changed or old_import is None else old_import[3]
		if old_import is None:
			cursor.execute(f"INSERT INTO {_quote(names.imports)} (session_date,raw_id,available_at,derived_dependency_id,derived_content_id) VALUES (%s,%s,%s,%s,%s)", (session, records_id, available_at, dependency_id, derived_id))
		else:
			cursor.execute(f"UPDATE {_quote(names.imports)} SET raw_id=%s,available_at=%s,derived_dependency_id=%s,derived_content_id=%s WHERE session_date=%s", (records_id, available_at, dependency_id, derived_id, session))
		cursor.execute(f"SELECT adopted_at,content_digest FROM {_quote(MIGRATION_RECEIPTS)} WHERE session_date=%s FOR UPDATE", (session,))
		old_receipt = cursor.fetchone()
		receipt_time = str(old_receipt[0]) if old_receipt is not None and old_receipt[1] == content else adoption_time
		cutoff_value = cutoff or adoption_time
		receipt_values = (session, context.manifest["manifest_id"], context.manifest["snapshot_identity"]["source_key"], evidence.source.provider, str(path), file_hash, records_id, len(bars), PROVENANCE_KIND, receipt_time, None, cutoff_value, evidence.evidence_id, content)
		if old_receipt is None:
			cursor.execute(f"INSERT INTO {_quote(MIGRATION_RECEIPTS)} (session_date,manifest_id,source_key,source_provider,source_path,file_sha256,records_id,record_count,provenance_kind,adopted_at,original_receipt_at,cutoff,evidence_id,content_digest) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", receipt_values)
		else:
			cursor.execute(f"UPDATE {_quote(MIGRATION_RECEIPTS)} SET manifest_id=%s,source_key=%s,source_provider=%s,source_path=%s,file_sha256=%s,records_id=%s,record_count=%s,provenance_kind=%s,adopted_at=%s,original_receipt_at=NULL,cutoff=%s,evidence_id=%s,content_digest=%s WHERE session_date=%s", (context.manifest["manifest_id"], context.manifest["snapshot_identity"]["source_key"], evidence.source.provider, str(path), file_hash, records_id, len(bars), PROVENANCE_KIND, receipt_time, cutoff_value, evidence.evidence_id, content, session))
		cursor.execute(f"SELECT content_digest FROM {_quote(MIGRATION_COMPLETION)} WHERE workload='raw' AND session_date=%s AND series_key='raw-adoption-v1' FOR UPDATE", (session,))
		completion = cursor.fetchone()
		if completion is None:
			cursor.execute(f"INSERT INTO {_quote(MIGRATION_COMPLETION)} (workload,session_date,series_key,content_digest,committed_at) VALUES ('raw',%s,'raw-adoption-v1',%s,%s)", (session, records_id, adoption_time))
		elif completion[0] != records_id:
			cursor.execute(f"UPDATE {_quote(MIGRATION_COMPLETION)} SET content_digest=%s,committed_at=%s WHERE workload='raw' AND session_date=%s AND series_key='raw-adoption-v1'", (records_id, adoption_time, session))
		connection.commit()
		return {"session": session, "record_count": len(bars), "original_row_count": len(stored), "current_row_count": len(bars), "records_id": records_id, "content_digest": content, "changed_rows": changed, "new_tickers": len(new_ids), "repaired": changed > 0, "matching": changed == 0, "receipt_adopted_at": receipt_time, "cutoff": cutoff_value, "committed": True}
	except Exception:
		connection.rollback()
		raise
	finally:
		cursor.close()


def _pilot_sessions(manifest: dict[str, Any], data_root: Path, zscore_sample_count: int) -> dict[str, Any]:
	try:
		import exchange_calendars as xcals  # type: ignore[import-untyped]
	except ImportError as error:
		raise Stage12Error("The configured Python interpreter lacks exchange_calendars") from error
	valid = {date.fromisoformat(str(item["session"])) for item in manifest.get("files", ()) if item.get("valid") is True and item.get("records_id")}
	if not valid or zscore_sample_count < 1:
		raise Stage12Error("Manifest has no valid sessions or the z-score sample count is invalid")
	calendar = xcals.get_calendar(str(manifest.get("calendar_id", DEFAULT_CALENDAR_ID)))
	all_expected = [item.date() for item in calendar.sessions_in_range(min(valid).isoformat(), max(valid).isoformat())]
	def has_neighbors(candidate: list[date]) -> bool:
		if not candidate:
			return False
		start = all_expected.index(candidate[0])
		end = all_expected.index(candidate[-1])
		return start > 0 and end + 1 < len(all_expected) and all(item in valid for item in all_expected[start - 1:end + 2])
	def sampled_targets(candidate: list[date]) -> list[date]:
		if zscore_sample_count >= len(candidate):
			return candidate
		indices = sorted({0, len(candidate) // 2, len(candidate) - 1})[:zscore_sample_count]
		return [candidate[index] for index in indices]
	def has_zscore_history(candidate: list[date]) -> bool:
		for session in sampled_targets(candidate):
			index = all_expected.index(session)
			if index < 59 or not all(item in valid for item in all_expected[index - 59:index + 1]):
				return False
		return True
	target: list[date] = []
	selection_mode = "complete_calendar_month"
	latest_month: list[date] = []
	for year, month in sorted({(item.year, item.month) for item in valid}, reverse=True):
		month_start = date(year, month, 1)
		month_end = (date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1))
		expected = [item.date() for item in calendar.sessions_in_range(month_start.isoformat(), month_end.isoformat())]
		if expected and all(item in valid for item in expected) and has_neighbors(expected):
			latest_month = expected
			break
	if latest_month and has_zscore_history(latest_month):
		target = latest_month
	if not target:
		selection_mode = "latest_20_consecutive_represented_exchange_sessions"
		for index in range(len(all_expected) - 20, -1, -1):
			candidate = all_expected[index:index + 20]
			if len(candidate) == 20 and all(item in valid for item in candidate) and has_neighbors(candidate) and has_zscore_history(candidate):
				target = candidate
				break
	if not target:
		raise Stage12Error("No complete archive month or 20-session fallback is available")
	def neighbors(session: date) -> tuple[date, date]:
		index = all_expected.index(session)
		if index == 0 or index + 1 >= len(all_expected) or all_expected[index - 1] not in valid or all_expected[index + 1] not in valid:
			raise Stage12Error(f"Neighboring archive dependencies are incomplete: {session}")
		return all_expected[index - 1], all_expected[index + 1]
	derived_dependencies = {dependency for session in target for dependency in neighbors(session)}
	zscore_targets = sampled_targets(target)
	zscore_dependencies: set[date] = set()
	for session in zscore_targets:
		index = all_expected.index(session)
		if index < 59:
			raise Stage12Error(f"Insufficient 60-session history for z-score sample: {session}")
		dependencies = all_expected[index - 59:index + 1]
		if not all(item in valid for item in dependencies):
			raise Stage12Error(f"Z-score archive dependencies are incomplete: {session}")
		zscore_dependencies.update(dependencies)
	return {"mode": selection_mode, "target_sessions": [item.isoformat() for item in target], "derived_dependencies": sorted(item.isoformat() for item in derived_dependencies), "zscore_sessions": [item.isoformat() for item in zscore_targets], "zscore_dependencies": sorted(item.isoformat() for item in zscore_dependencies), "valid_archive_sessions": len(valid), "data_root": str(data_root.resolve())}


def _java_classpath() -> str:
	root = Path(__file__).resolve().parents[3] / "java"
	return ";".join(str(root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))


def _run_java(command: list[str], timeout: float) -> dict[str, Any]:
	try:
		completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
	except (OSError, subprocess.TimeoutExpired) as error:
		raise Stage12Error("Selected Java Stage 12 backend could not run") from error
	try:
		result = json.loads(completed.stdout)
	except (TypeError, ValueError, json.JSONDecodeError) as error:
		raise Stage12Error("Selected Java Stage 12 backend returned invalid JSON") from error
	if completed.returncode != 0 or result.get("failed"):
		raise Stage12Error("Selected Java Stage 12 backend failed")
	return cast(dict[str, Any], result)


def _run_derived(dbinfo_file: Path, manifest_path: Path, sessions: list[str], checkpoint: Path, timeout: float) -> dict[str, Any]:
	command = ["java", "-Xmx512m", "-cp", _java_classpath(), "stockguidance.migration.JavaDerivedBenchmarkMain", "--manifest", str(manifest_path.resolve()), "--dbinfo-file", str(dbinfo_file.resolve()), "--production", "true", "--preparers", "1", "--sessions", ",".join(sessions), "--checkpoint-directory", str(checkpoint.resolve())]
	return _run_java(command, timeout)


def _run_zscores(dbinfo_file: Path, manifest_path: Path, sessions: list[str], checkpoint: Path, timeout: float) -> dict[str, Any]:
	command = ["java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "-cp", _java_classpath(), "stockguidance.migration.JavaZScoreBenchmarkMain", "--manifest", str(manifest_path.resolve()), "--dbinfo-file", str(dbinfo_file.resolve()), "--production", "true", "--sessions", ",".join(sessions), "--checkpoint-directory", str(checkpoint.resolve())]
	return _run_java(command, timeout)


def _audit_raw(connection: Any, context: Stage12Context, names: TableNames, sessions: Iterable[str]) -> dict[str, Any]:
	results = []
	for session in sessions:
		item = _file_item(context.manifest, session)
		path = Path(str(item["path"])).resolve()
		bars, file_hash = read_exact_archive(path)
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT id FROM {_quote(names.sessions)} WHERE transaction_date=%s", (session,))
			date_rows = cursor.fetchall()
			if len(date_rows) != 1:
				raise Stage12Error(f"Raw audit date mapping is not unique: {session}")
			cursor.execute(f"SELECT t.label,p.open,p.high,p.low,p.close,p.volume,p.vwap,p.transactions FROM {_quote(names.prices)} p JOIN {_quote(names.tickers)} t ON t.id=p.ticker_id WHERE p.date_id=%s ORDER BY t.label", (int(date_rows[0][0]),))
			actual = cursor.fetchall()
			if len(actual) != len(bars):
				raise Stage12Error(f"Raw pilot row count differs: {session}")
			for row, bar in zip(actual, bars, strict=True):
				if str(row[0]) != bar.ticker or not _bar_matches(row[1:], bar):
					raise Stage12Error(f"Raw pilot value differs: {session}/{bar.ticker}")
			cursor.execute(f"SELECT records_id,file_sha256,record_count,original_receipt_at,content_digest FROM {_quote(MIGRATION_RECEIPTS)} WHERE session_date=%s", (session,))
			receipt = cursor.fetchone()
			if receipt is None or receipt[0] != item["records_id"] or receipt[1] != file_hash or int(receipt[2]) != len(bars) or receipt[3] is not None:
				raise Stage12Error(f"Raw pilot receipt differs: {session}")
			results.append({"session": session, "rows": len(actual), "records_id": item["records_id"], "receipt_content_digest": receipt[4]})
		finally:
			cursor.close()
	return {"status": "passed", "sessions": results}


def _audit_derived(connection: Any, context: Stage12Context, names: TableNames, sessions: Iterable[str]) -> dict[str, Any]:
	results = []
	valid = sorted(entry["session"] for entry in context.manifest.get("files", ()) if entry.get("valid") is True and entry.get("records_id"))
	for session in sessions:
		manifest_target = next((item for item in context.manifest.get("targets", ()) if item.get("session") == session), None)
		dependencies = tuple(manifest_target["derived_dependencies"]) if manifest_target is not None else (valid[valid.index(session) - 1], valid[valid.index(session) + 1])
		archives = []
		for value in (dependencies[0], session, dependencies[1]):
			bars, _ = read_exact_archive(Path(str(_file_item(context.manifest, value)["path"])).resolve())
			archives.append(bars)
		expected = {row.ticker: row.values for row in _exact_derived_rows(*archives)}
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT id FROM {_quote(names.sessions)} WHERE transaction_date=%s", (session,))
			date_rows = cursor.fetchall()
			if len(date_rows) != 1:
				raise Stage12Error(f"Derived audit date mapping is not unique: {session}")
			columns = ",".join(_quote(column) for column in DERIVED_COLUMNS)
			cursor.execute(f"SELECT t.label,{columns} FROM {_quote(names.deltas)} d JOIN {_quote(names.tickers)} t ON t.id=d.ticker_id WHERE d.date_id=%s ORDER BY t.label", (int(date_rows[0][0]),))
			actual = {str(row[0]): tuple(row[1:]) for row in cursor.fetchall()}
			if set(actual) != set(expected):
				raise Stage12Error(f"Derived pilot row set differs: {session}")
			for ticker, values in expected.items():
				for left, right in zip(actual[ticker], values, strict=True):
					if left is None or right is None:
						if left is not None or right is not None:
							raise Stage12Error(f"Derived pilot NULL differs: {session}/{ticker}")
					elif abs(float(left) - float(right)) > 1e-10 * max(1.0, abs(float(right))) + 1e-12:
						raise Stage12Error(f"Derived pilot value differs: {session}/{ticker}")
			cursor.execute(f"SELECT derived_dependency_id,derived_content_id FROM {_quote(names.imports)} WHERE session_date=%s", (session,))
			imports = cursor.fetchall()
			if len(imports) != 1 or imports[0][1] is None:
				raise Stage12Error(f"Derived pilot receipt is incomplete: {session}")
			results.append({"session": session, "rows": len(actual), "derived_content_id": imports[0][1]})
		finally:
			cursor.close()
	return {"status": "passed", "sessions": results}


def _audit_zscores(connection: Any, names: TableNames, sessions: Iterable[str], expected_tickers: int) -> dict[str, Any]:
	results = []
	cursor = connection.cursor()
	try:
		for session in sessions:
			cursor.execute(f"SELECT id FROM {_quote(names.sessions)} WHERE transaction_date=%s", (session,))
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise Stage12Error(f"Z-score audit date mapping is not unique: {session}")
			date_id = int(rows[0][0])
			for family in ("price", "price_delta"):
				for window, tables in names.resolve_zscore_tables((20, 60)).items():
					table = tables.price if family == "price" else tables.price_delta
					cursor.execute(f"SELECT COUNT(*),COALESCE(SUM(status=0),0),COALESCE(SUM(status),0) FROM {_quote(table)} WHERE date_id=%s", (date_id,))
					count, ready, status_sum = cursor.fetchone()
					if int(count) != expected_tickers:
						raise Stage12Error(f"Z-score pilot row count differs: {session}/{family}/{window}")
					results.append({"session": session, "family": family, "window": window, "rows": int(count), "ready_rows": int(ready), "status_sum": int(status_sum)})
		return {"status": "passed", "series": results}
	finally:
		cursor.close()


def _pilot_once(context: Stage12Context, dbinfo_file: Path, names: TableNames, selection: dict[str, Any], output_dir: Path, adoption_time: str, java_timeout: float) -> dict[str, Any]:
	all_raw = sorted(set(selection["target_sessions"]) | set(selection["derived_dependencies"]) | set(selection["zscore_dependencies"]))
	derived = list(selection["target_sessions"])
	zscores = list(selection["zscore_sessions"])
	connection = _connect(dbinfo_file, load_database_configuration(dbinfo_file).database)
	try:
		raw_results = [_adopt_raw_session(connection, context, names, session, adoption_time, set(derived), set(zscores)) for session in all_raw]
	finally:
		connection.close()
	derived_result = _run_derived(dbinfo_file, context.manifest_path, derived, output_dir / "java-derived-checkpoints", java_timeout)
	zscore_result = _run_zscores(dbinfo_file, context.manifest_path, zscores, output_dir / "java-zscore-checkpoints", java_timeout)
	connection = _connect(dbinfo_file, load_database_configuration(dbinfo_file).database)
	try:
		raw_audit = _audit_raw(connection, context, names, all_raw)
		derived_audit = _audit_derived(connection, context, names, derived)
		zscore_audit = _audit_zscores(connection, names, zscores, len(context.manifest.get("mappings", {}).get("ticker_mappings", ())))
	finally:
		connection.rollback()
		connection.close()
	return {"adoption_time": adoption_time, "raw": {"results": raw_results, "audit": raw_audit}, "derived": {"backend": "java", "result": derived_result, "audit": derived_audit}, "zscores": {"backend": "java", "result": zscore_result, "audit": zscore_audit}}


def _stable_raw_results(result: dict[str, Any]) -> list[dict[str, Any]]:
	return [{key: item[key] for key in ("session", "record_count", "records_id", "content_digest", "receipt_adopted_at")} for item in result["raw"]["results"]]


def _reuse_restored_backup(dbinfo_file: Path, backup_path: Path, restore_schema: str, names: TableNames) -> dict[str, Any]:
	if not DISPOSABLE_SCHEMA.fullmatch(restore_schema):
		raise Stage12Error("Restore target must be a distinct disposable schema")
	configuration = load_database_configuration(dbinfo_file)
	if restore_schema.casefold() == configuration.database.casefold():
		raise Stage12Error("Restore target must not be production")
	connection = _connect(dbinfo_file, restore_schema)
	started = perf_counter()
	try:
		snapshot = _schema_snapshot(connection, names)
	finally:
		connection.rollback()
		connection.close()
	return {"schema": restore_schema, "statement_count": None, "seconds": perf_counter() - started, "status": "restored_verified", "backup_sha256": _sha256(backup_path), "schema_snapshot": snapshot}


def run_stage12(
	context: Stage12Context,
	dbinfo_file: Path,
	data_root: Path,
	restore_schema: str,
	output: Path,
	*,
	zscore_sample_count: int = 3,
	java_timeout: float = 7200.0,
	reuse_restore: bool = False,
) -> dict[str, Any]:
	names = _manifest_names(context.manifest)
	restore_report = _reuse_restored_backup(dbinfo_file, context.backup_path, restore_schema, names) if reuse_restore else _restore_backup(dbinfo_file, context.backup_path, restore_schema)
	if not reuse_restore:
		restore_connection = _connect(dbinfo_file, restore_schema)
		try:
			restore_report["schema_snapshot"] = _schema_snapshot(restore_connection, names)
		finally:
			restore_connection.rollback()
			restore_connection.close()
	selection = _pilot_sessions(context.manifest, data_root, zscore_sample_count)
	production = load_database_configuration(dbinfo_file)
	connection = _connect(dbinfo_file, production.database)
	try:
		active = _active_writers(connection)
		if active:
			raise Stage12Error("Competing production writers are active")
		before = _schema_snapshot(connection, names)
		engines = _schema_engines(before["inventory"])
		disk_before = _server_disk(connection)
		raw_size = next((int(row[3] or 0) + int(row[4] or 0) for row in before["inventory"]["tables"] if row[0] == names.prices), 0)
		backup_size = context.backup_path.stat().st_size
		migration_preview = {"statements": list(_migration_ddl(names, engines, before["inventory"])), "raw_table_bytes": raw_size, "disk_before": disk_before, "backup_bytes": backup_size, "formula": "raw data+index bytes plus backup; free space is checked before DDL", "numeric_maxima_before": _decimal_limits(connection, names)}
		if disk_before["free_bytes"] is not None and disk_before["free_bytes"] < raw_size + backup_size:
			raise Stage12Error("Insufficient disk space for the measured table copy and recovery backup")
		ddl_results: list[dict[str, Any]] = _execute_ddl(connection, cast(Iterable[str], migration_preview["statements"]))
		after = _schema_snapshot(connection, names, include_zscores=True)
		disk_after = _server_disk(connection)
		if before["mapping_identity"] != after["mapping_identity"]:
			raise Stage12Error("Ticker/date mapping identity changed during schema migration")
	finally:
		connection.close()
	adoption_time = datetime.now(timezone.utc).isoformat()
	checkpoint = output.resolve().parent / "stage12-checkpoints"
	first = _pilot_once(context, dbinfo_file, names, selection, checkpoint, adoption_time, java_timeout)
	second = _pilot_once(context, dbinfo_file, names, selection, checkpoint, adoption_time, java_timeout)
	if _stable_raw_results(first) != _stable_raw_results(second) or first["derived"]["audit"] != second["derived"]["audit"] or first["zscores"]["audit"] != second["zscores"]["audit"]:
		raise Stage12Error("Stage 12 second-run idempotency audit differs")
	report = {"schema_version": STAGE12_SCHEMA_VERSION, "stage": 12, "status": "complete", "production_ready": True, "production_access": {"ddl": True, "dml": True, "database": production.database}, "prerequisites": {"decision_sha256": context.decision_sha256, "manifest_sha256": context.manifest_sha256, "audit_sha256": context.audit_sha256, "stage11b_sha256": context.stage11b_sha256, "precision_sha256": context.precision_sha256, "backup_sha256": _sha256(context.backup_path)}, "restore": restore_report, "schema": {"before": before, "after": after, "migration_preview": migration_preview, "ddl_results": ddl_results, "disk_after": disk_after}, "pilot_selection": selection, "pilot": {"first": first, "second": second, "idempotency": {"raw_results_stable": True, "derived_audit_stable": True, "zscore_audit_stable": True}}, "handoff": {"next": "Stage 13", "production_ready": True, "full_rollout": "authorized after Stage 12 gates", "selected_backends": {"raw": "python", "derived": "java", "zscores": "java"}, "zscore_sample_scope": "bounded sample of selected complete-month sessions; not full-history population"}}
	output = output.resolve()
	output.parent.mkdir(parents=True, exist_ok=True)
	atomic_write(output, encode_json(report))
	return report
