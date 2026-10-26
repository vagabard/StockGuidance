from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import file_digest
from pathlib import Path
from typing import Any, Iterator
import json
import logging
import sys

from stockguidance.config.source import DataConfiguration, SourceIdentity
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.table_names import TableRole, quote_identifier
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, data_configuration_key, encode_json, source_key
from stockguidance.performance.archive_read import ArchiveBar, inventory, streaming_connection
from stockguidance.legacy_migration.evidence import ArchiveEvidenceStore
from stockguidance.legacy_migration.precision import ArchiveFileInventory, ArchiveInventory, ExactArchiveBar, exact_records_id, read_exact_archive, scan_archive


ARCHIVE_SOURCE = DataConfiguration("polygon", "/v2/aggs/grouped/locale/us/market/stocks/{date}", "us-stocks", "us-stocks-excluding-otc", True, False).to_source_identity()
LOGGER = logging.getLogger(__name__)
RAW_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "transactions")


@dataclass
class DatabaseSession:
	rows: list[ArchiveBar] = field(default_factory=list)
	invalid_count: int = 0
	invalid_examples: list[dict[str, object]] = field(default_factory=list)
	duplicate_count: int = 0
	keys: set[str] = field(default_factory=set)


def _close_audit(connection: Any, cursor: Any) -> None:
	failed = sys.exc_info()[0] is not None
	try:
		if not failed:
			try:
				if cursor is not None:
					cursor.close()
			finally:
				connection.rollback()
	finally:
		try:
			connection.close()
		except Exception:
			if not failed:
				raise
			LOGGER.exception("Closing the failed read-only audit connection also failed")


def _source_payload(source: SourceIdentity) -> dict[str, object]:
	return {"provider": source.provider, "endpoint": source.endpoint, "market": source.market, "universe": source.universe, "adjustment": source.adjustment.value, "interval": source.interval.value}


def read_database_tickers(database_config: Path) -> tuple[tuple[int, str], ...]:
	connection = streaming_connection(database_config)
	try:
		names = load_database_configuration(database_config).load_table_names()
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT id, label FROM {quote_identifier(names.table(TableRole.TICKERS))} ORDER BY id", ())
			rows = cursor.fetchall()
		finally:
			cursor.close()
		result: list[tuple[int, str]] = []
		for row in rows:
			if len(row) != 2 or type(row[0]) is not int or not isinstance(row[1], str):
				raise ValueError("Invalid database ticker mapping")
			result.append((row[0], row[1]))
		return tuple(result)
	finally:
		connection.rollback()
		connection.close()


def _database_session_dates(cursor: Any, names: Any) -> set[date]:
	cursor.execute(f"SELECT transaction_date FROM {quote_identifier(names.table(TableRole.SESSIONS))} ORDER BY transaction_date", ())
	rows = cursor.fetchall()
	if any(len(row) != 1 or type(row[0]) is not date for row in rows):
		raise ValueError("Invalid database session mapping")
	dates = {row[0] for row in rows}
	if len(dates) != len(rows):
		raise ValueError("Duplicate database session mapping")
	return dates


def _database_identity(cursor: Any, names: Any) -> dict[str, object]:
	configuration = ARCHIVE_SOURCE.to_data_configuration()
	configuration_key = data_configuration_key(configuration)
	expected = (configuration_key, configuration_key, configuration.source_family, configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value, configuration.raw_price_split_adjusted, configuration.raw_price_dividend_adjusted, configuration.raw_price_vendor_defined_adjustment)
	cursor.execute(f"SELECT configuration_key, source_key, source_family, endpoint, market, universe, interval_code, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment FROM {quote_identifier(names.source)}", ())
	if cursor.fetchall() != [expected]:
		raise ValueError("Database source binding conflicts with the retained archive")
	tables = tuple(names.table(role) for role in (TableRole.SOURCE, TableRole.SESSIONS, TableRole.TICKERS, TableRole.PRICES, TableRole.DELTAS))
	placeholders = ", ".join("%s" for _ in tables)
	cursor.execute(f"SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME", tables)
	engines = cursor.fetchall()
	if len(engines) != len(tables) or any(row[1] != "InnoDB" for row in engines):
		raise ValueError("Audit tables must exist and use InnoDB for a consistent read-only snapshot")
	cursor.execute(f"SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME, ORDINAL_POSITION", tables)
	columns = cursor.fetchall()
	cursor.execute("SELECT @@server_uuid, DATABASE(), CONNECTION_ID()", ())
	snapshot = cursor.fetchall()
	return {"schema_hash": content_id({"engines": engines, "columns": columns}), "table_names": names.tables(), "snapshot": snapshot, "source_key": source_key(ARCHIVE_SOURCE), "configuration_key": configuration_key}


def _database_orphans(cursor: Any, names: Any) -> dict[str, int]:
	result: dict[str, int] = {}
	for role in (TableRole.PRICES, TableRole.DELTAS):
		LOGGER.info("Checking global %s orphans against session and ticker mappings", role.value)
		table = quote_identifier(names.table(role))
		sessions = quote_identifier(names.table(TableRole.SESSIONS))
		tickers = quote_identifier(names.table(TableRole.TICKERS))
		cursor.execute(f"SELECT COUNT(*), COALESCE(SUM(s.id IS NULL), 0), COALESCE(SUM(t.id IS NULL), 0) FROM {table} r LEFT JOIN {sessions} s ON s.id = r.date_id LEFT JOIN {tickers} t ON t.id = r.ticker_id WHERE s.id IS NULL OR t.id IS NULL", ())
		rows = cursor.fetchall()
		if len(rows) != 1 or len(rows[0]) != 3:
			raise ValueError("Invalid orphan count")
		result[role.value] = int(rows[0][0])
		result[f"{role.value}_missing_session"] = int(rows[0][1])
		result[f"{role.value}_missing_ticker"] = int(rows[0][2])
		LOGGER.info("Global %s orphan rows: %d", role.value, result[role.value])
	return result


def _database_groups(cursor: Any, names: Any, start: date | None, end: date | None) -> Iterator[tuple[date, DatabaseSession]]:
	where: list[str] = []
	parameters: list[object] = []
	if start is not None:
		where.append("t.transaction_date >= %s")
		parameters.append(start)
	if end is not None:
		where.append("t.transaction_date <= %s")
		parameters.append(end)
	predicate = " WHERE " + " AND ".join(where) if where else ""
	query = f"SELECT t.transaction_date, s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions, r.ticker_id FROM {quote_identifier(names.table(TableRole.PRICES))} r LEFT JOIN {quote_identifier(names.table(TableRole.TICKERS))} s ON s.id = r.ticker_id JOIN {quote_identifier(names.table(TableRole.SESSIONS))} t ON t.id = r.date_id{predicate} ORDER BY t.transaction_date"
	LOGGER.info("Starting the ordered database raw-row stream")
	cursor.execute(query, tuple(parameters))
	current_date: date | None = None
	current = DatabaseSession()
	warnings = 0
	while True:
		batch = cursor.fetchmany(1000)
		if not batch:
			break
		for row in batch:
			if len(row) != 10 or type(row[0]) is not date:
				raise ValueError("Invalid database raw audit row")
			if current_date is None:
				current_date = row[0]
			if row[0] != current_date:
				yield current_date, current
				current_date, current = row[0], DatabaseSession()
				values = tuple(Decimal(str(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else value for value in row[2:8])
				if not isinstance(row[1], str) or len(values) != 6:
					raise ValueError("Invalid stored archive row")
				bar = ArchiveBar(row[1], values[0], values[1], values[2], values[3], values[4], values[5], row[8])
			current.rows.append(bar)
			try:
					ExactArchiveBar(bar.ticker, values[0], values[1], values[2], values[3], values[4], values[5], bar.transactions)
			except (ValueError, TypeError, ArithmeticError) as error:
				current.invalid_count += 1
				finding = {"ticker": row[1], "ticker_id": row[9], "error": str(error), "values": {name: None if getattr(bar, name) is None else str(getattr(bar, name)) for name in RAW_FIELDS}}
				if len(current.invalid_examples) < 100:
					current.invalid_examples.append(finding)
				if warnings < 5:
					LOGGER.warning("Invalid stored row on %s: %s", current_date, finding)
					warnings += 1
			if isinstance(bar.ticker, str):
				current.duplicate_count += int(bar.ticker in current.keys)
				current.keys.add(bar.ticker)
	if current_date is not None:
		yield current_date, current


def _field_differences(expected: ExactArchiveBar, actual: ArchiveBar) -> dict[str, dict[str, str | None]]:
	result: dict[str, dict[str, str | None]] = {}
	for name in RAW_FIELDS:
		expected_value, actual_value = getattr(expected, name), getattr(actual, name)
		if expected_value != actual_value:
			result[name] = {"archive": None if expected_value is None else str(expected_value), "database": None if actual_value is None else str(actual_value)}
	return result


def _database_findings(database: DatabaseSession | None) -> dict[str, object]:
	return {"database_count": None if database is None else len(database.rows), "invalid_database_row_count": 0 if database is None else database.invalid_count, "invalid_database_row_examples": [] if database is None else database.invalid_examples, "duplicate_database_key_count": 0 if database is None else database.duplicate_count}


def _database_only(session: date, database: DatabaseSession) -> dict[str, object]:
	return {"session": session.isoformat(), "state": "database_only", **_database_findings(database), "matches": False, "missing_keys": [], "extra_keys": sorted(database.keys)[:100], "extra_key_count": len(database.keys)}


def _compare_session(session: str, path: Path, item: ArchiveFileInventory, database: DatabaseSession | None, evidence_store: ArchiveEvidenceStore | None = None) -> dict[str, object]:
	result: dict[str, object] = {"session": session, "path": str(path), "file_sha256": item.file_sha256, "file_valid": item.valid, "archive_count": item.record_count, **_database_findings(database), "missing_keys": [], "extra_keys": [], "field_difference_counts": {name: 0 for name in RAW_FIELDS}, "difference_examples": [], "state": "invalid_file" if not item.valid else "database_unavailable" if database is None else "complete", "matches": False}
	if not item.valid:
		with path.open("rb") as stream:
			digest = file_digest(stream, "sha256").hexdigest()
		if digest != item.file_sha256:
			result["state"] = "changed_file"
		return result
	try:
		bars, digest = read_exact_archive(path)
	except (ValueError, TypeError, ArithmeticError) as error:
		result.update(state="changed_file", error=str(error))
		return result
	if digest != item.file_sha256:
		result.update(state="changed_file", matches=False)
		return result
	if len(bars) != item.record_count or exact_records_id(bars) != item.records_id:
		result.update(state="inventory_mismatch")
		return result
	if evidence_store is not None:
		try:
			result["evidence_id"] = evidence_store.verify(ARCHIVE_SOURCE, session, item).evidence_id
		except ValueError as error:
			result.update(state="invalid_evidence", error=str(error))
			return result
	if database is None:
		return result
	expected_by_key, actual_by_key = {bar.ticker: bar for bar in bars}, {bar.ticker: bar for bar in database.rows if isinstance(bar.ticker, str)}
	missing = sorted(set(expected_by_key) - set(actual_by_key))
	extra = sorted(set(actual_by_key) - set(expected_by_key))
	result["missing_keys"], result["extra_keys"] = missing[:100], extra[:100]
	result["missing_key_count"], result["extra_key_count"] = len(missing), len(extra)
	field_counts = result["field_difference_counts"]
	if not isinstance(field_counts, dict):
		raise ValueError("Invalid field difference count state")
	field_counts = {str(name): int(value) for name, value in field_counts.items()}
	examples: list[dict[str, object]] = []
	differing_rows = 0
	for ticker in sorted(set(expected_by_key) & set(actual_by_key)):
		differences = _field_differences(expected_by_key[ticker], actual_by_key[ticker])
		for name in differences:
			field_counts[name] += 1
		differing_rows += int(bool(differences))
		if differences and len(examples) < 100:
			examples.append({"ticker": ticker, "fields": differences})
	result["difference_examples"] = examples
	result["omitted_missing_keys"] = max(0, len(missing) - 100)
	result["omitted_extra_keys"] = max(0, len(extra) - 100)
	result["different_row_count"] = differing_rows
	result["omitted_difference_examples"] = max(0, differing_rows - len(examples))
	result["matches"] = not missing and not extra and not differing_rows and not database.invalid_count and not database.duplicate_count
	return result


def audit_archive(root: Path, database_config: Path | None = None, *, start: date | None = None, end: date | None = None, checkpoint_path: Path | None = None, decision_path: Path | None = None, evidence_path: Path | None = None, archive_inventory: ArchiveInventory | None = None) -> dict[str, object]:
	if start is not None and end is not None and start > end:
		raise ValueError("Audit start date must not follow its end date")
	if decision_path is not None and decision_path.exists():
		decision = json.loads(decision_path.read_text(encoding="utf-8"))
		if not isinstance(decision, dict) or decision.get("mode") != "audit" or decision.get("production_apply") is not False:
			raise ValueError("Only a provisional audit decision is accepted in Stage 5")
	else:
		decision = {"schema_version": 1, "mode": "audit", "production_apply": False, "provisional": True}
	if evidence_path is not None and not evidence_path.exists():
		raise ValueError("Archive evidence path does not exist")
	archive = archive_inventory or scan_archive(root, start, end)
	selected_files = [item for item in archive.files if (start is None or date.fromisoformat(item.session) >= start) and (end is None or date.fromisoformat(item.session) <= end)]
	paths = {path.resolve() for path in inventory(root) if (start is None or date.fromisoformat(path.stem) >= start) and (end is None or date.fromisoformat(path.stem) <= end)}
	if not selected_files or len({item.session for item in selected_files}) != len(selected_files) or {Path(item.path).resolve() for item in selected_files} != paths:
		raise ValueError("Archive inventory does not cover exactly the selected retained files")
	archive = ArchiveInventory(root, files=sorted(selected_files, key=lambda item: item.session))
	evidence_store = None if evidence_path is None else ArchiveEvidenceStore(evidence_path)
	reports: list[dict[str, object]] = []
	database_available = database_config is not None
	database_revision = datetime.now(timezone.utc).isoformat() if database_available else None
	database_session_dates: set[date] = set()
	database_groups: Iterator[tuple[date, DatabaseSession]] = iter(())
	orphans: dict[str, int] | None = None
	database_identity: dict[str, object] | None = None
	connection = None
	cursor = None
	try:
		if database_config is not None:
			connection = streaming_connection(database_config)
			names = load_database_configuration(database_config).load_table_names()
			cursor = connection.cursor()
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			cursor.execute("START TRANSACTION READ ONLY")
			database_identity = _database_identity(cursor, names)
			LOGGER.info("Checking global mappings and raw/derived orphan integrity once")
			database_session_dates = _database_session_dates(cursor, names)
			orphans = _database_orphans(cursor, names)
			LOGGER.info("Global orphan counts: %s", orphans)
			database_groups = _database_groups(cursor, names, start, end)
		current_group = next(database_groups, None)
		archive_dates = {date.fromisoformat(item.session) for item in archive.files}
		for index, item in enumerate(archive.files, 1):
			path = Path(item.path)
			session = date.fromisoformat(item.session)
			while current_group is not None and current_group[0] < session:
				reports.append(_database_only(*current_group))
				current_group = next(database_groups, None)
			database_rows = current_group[1] if current_group is not None and current_group[0] == session else DatabaseSession()
			reports.append(_compare_session(item.session, path, item, database_rows if database_available else None, evidence_store))
			if index == 1 or index % 25 == 0:
				LOGGER.info("Audited %d/%d archive sessions; current=%s state=%s database_rows=%s", index, len(archive.files), item.session, reports[-1]["state"], reports[-1]["database_count"])
			if current_group is not None and current_group[0] == session:
				current_group = next(database_groups, None)
		while current_group is not None:
			reports.append(_database_only(*current_group))
			current_group = next(database_groups, None)
		for session in sorted(database_session_dates - archive_dates):
			if (start is None or session >= start) and (end is None or session <= end) and not any(item.get("session") == session.isoformat() for item in reports):
				reports.append(_database_only(session, DatabaseSession()))
	finally:
		if connection is not None:
			_close_audit(connection, cursor)
	valid_archive = len(archive.valid_files) == len(archive.files)
	complete = database_available and len(reports) >= len(archive.files) and all(item.get("state") in {"complete", "invalid_file", "database_only"} for item in reports)
	result = {
		"schema_version": 1,
		"operation": "legacy_archive_audit",
		"checked_at": datetime.now(timezone.utc).isoformat(),
		"source": _source_payload(ARCHIVE_SOURCE),
		"scope": {"start": None if start is None else start.isoformat(), "end": None if end is None else end.isoformat()},
		"decision": decision,
		"evidence_path": None if evidence_path is None else str(evidence_path),
		"database_available": database_available,
		"database_revision": database_revision,
		"database_identity": database_identity,
		"complete": complete,
		"matches": complete and valid_archive and not any((orphans or {}).values()) and all(item.get("matches") is True for item in reports),
		"archive_file_count": len(archive.files),
		"valid_archive_file_count": len(archive.valid_files),
		"invalid_archive_files": [item.__dict__ for item in archive.invalid_files],
		"database_only_dates": sorted(str(item.get("session")) for item in reports if item.get("state") == "database_only"),
		"orphan_counts": orphans,
		"session_count": len(reports),
		"database_row_count": sum(int(str(item.get("database_count") or 0)) for item in reports),
		"invalid_database_row_count": sum(int(str(item.get("invalid_database_row_count", 0) or 0)) for item in reports),
		"duplicate_database_key_count": sum(int(str(item.get("duplicate_database_key_count", 0) or 0)) for item in reports),
		"sessions": sorted(reports, key=lambda item: str(item.get("session"))),
		"checkpoint_reuse": 0,
		"checkpoint_policy": "Recompare every session in the new database snapshot; no prior database comparisons are trusted",
		"scope_identity": content_id({"source": _source_payload(ARCHIVE_SOURCE), "files": [(item.session, item.file_sha256) for item in archive.files], "database_identity": database_identity, "database_revision": database_revision}),
	}
	if checkpoint_path is not None:
		checkpoint_payload = {"schema_version": 1, "source": _source_payload(ARCHIVE_SOURCE), "scope_identity": result["scope_identity"], "sessions": {item["session"]: item for item in reports}}
		atomic_write(checkpoint_path, encode_json(checkpoint_payload))
	return result
