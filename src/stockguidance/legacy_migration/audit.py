from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
import json

from stockguidance.config.source import DataConfiguration, SourceIdentity
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.table_names import TableRole, quote_identifier
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.performance.archive_read import ArchiveBar, decode_archive_bar, read_archive, streaming_connection
from stockguidance.legacy_migration.precision import ArchiveFileInventory, ArchiveInventory, scan_archive


ARCHIVE_SOURCE = DataConfiguration("polygon", "/v2/aggs/grouped/locale/us/market/stocks/{date}", "us-stocks", "us-stocks-excluding-otc", True, False).to_source_identity()


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
	return {row[0] for row in cursor.fetchall() if len(row) == 1 and isinstance(row[0], date)}


def _database_orphans(cursor: Any, names: Any) -> dict[str, int]:
	result: dict[str, int] = {}
	for role in (TableRole.PRICES, TableRole.DELTAS):
		table = quote_identifier(names.table(role))
		sessions = quote_identifier(names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT COUNT(*) FROM {table} r LEFT JOIN {sessions} s ON s.id = r.date_id WHERE s.id IS NULL", ())
		rows = cursor.fetchall()
		if len(rows) != 1 or len(rows[0]) != 1:
			raise ValueError("Invalid orphan count")
		result[role.value] = int(rows[0][0])
	return result


def _database_groups(cursor: Any, names: Any, start: date | None, end: date | None) -> Iterator[tuple[date, tuple[ArchiveBar, ...]]]:
	where: list[str] = []
	parameters: list[object] = []
	if start is not None:
		where.append("t.transaction_date >= %s")
		parameters.append(start)
	if end is not None:
		where.append("t.transaction_date <= %s")
		parameters.append(end)
	predicate = " WHERE " + " AND ".join(where) if where else ""
	query = f"SELECT t.transaction_date, s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions FROM {quote_identifier(names.table(TableRole.PRICES))} r JOIN {quote_identifier(names.table(TableRole.TICKERS))} s ON s.id = r.ticker_id JOIN {quote_identifier(names.table(TableRole.SESSIONS))} t ON t.id = r.date_id{predicate} ORDER BY t.transaction_date, s.label"
	cursor.execute(query, tuple(parameters))
	current_date: date | None = None
	current: list[ArchiveBar] = []
	while True:
		batch = cursor.fetchmany(1000)
		if not batch:
			break
		for row in batch:
			if len(row) != 9 or not isinstance(row[0], date):
				raise ValueError("Invalid database raw audit row")
			observed = {"T": row[1], "o": row[2], "h": row[3], "l": row[4], "c": row[5], "v": row[6], "vw": row[7], "n": row[8]}
			bar = decode_archive_bar(observed)
			if current_date is None:
				current_date = row[0]
			if row[0] != current_date:
				yield current_date, tuple(current)
				current_date, current = row[0], []
			current.append(bar)
	if current_date is not None:
		yield current_date, tuple(current)


def _field_differences(expected: ArchiveBar, actual: ArchiveBar) -> dict[str, dict[str, str | None]]:
	fields = ("open", "high", "low", "close", "volume", "vwap", "transactions")
	result: dict[str, dict[str, str | None]] = {}
	for name in fields:
		expected_value, actual_value = getattr(expected, name), getattr(actual, name)
		if expected_value != actual_value:
			result[name] = {"archive": None if expected_value is None else str(expected_value), "database": None if actual_value is None else str(actual_value)}
	return result


def _compare_session(session: str, path: Path, item: ArchiveFileInventory, database: tuple[ArchiveBar, ...] | None) -> dict[str, object]:
	result: dict[str, object] = {"session": session, "path": str(path), "file_sha256": item.file_sha256, "file_valid": item.valid, "archive_count": item.record_count, "database_count": None if database is None else len(database), "missing_keys": [], "extra_keys": [], "field_difference_counts": {name: 0 for name in ("open", "high", "low", "close", "volume", "vwap", "transactions")}, "difference_examples": [], "state": "invalid_file" if not item.valid else "database_unavailable" if database is None else "complete", "matches": False}
	if not item.valid or database is None:
		return result
	bars, digest = read_archive(path)
	if digest != item.file_sha256:
		result.update(state="changed_file", matches=False)
		return result
	expected_by_key, actual_by_key = {bar.ticker: bar for bar in bars}, {bar.ticker: bar for bar in database}
	missing = sorted(set(expected_by_key) - set(actual_by_key))
	extra = sorted(set(actual_by_key) - set(expected_by_key))
	result["missing_keys"], result["extra_keys"] = missing[:100], extra[:100]
	field_counts = result["field_difference_counts"]
	examples: list[dict[str, object]] = []
	for ticker in sorted(set(expected_by_key) & set(actual_by_key)):
		differences = _field_differences(expected_by_key[ticker], actual_by_key[ticker])
		for name in differences:
			field_counts[name] += 1
		if differences and len(examples) < 100:
			examples.append({"ticker": ticker, "fields": differences})
	result["difference_examples"] = examples
	result["omitted_missing_keys"] = max(0, len(missing) - len(result["missing_keys"]))
	result["omitted_extra_keys"] = max(0, len(extra) - len(result["extra_keys"]))
	result["omitted_difference_examples"] = max(0, sum(field_counts.values()) - len(examples))
	result["matches"] = not missing and not extra and not any(field_counts.values())
	return result


def audit_archive(root: Path, database_config: Path | None = None, *, start: date | None = None, end: date | None = None, checkpoint_path: Path | None = None, decision_path: Path | None = None, evidence_path: Path | None = None, archive_inventory: ArchiveInventory | None = None) -> dict[str, object]:
	if decision_path is not None and decision_path.exists():
		decision = json.loads(decision_path.read_text(encoding="utf-8"))
		if not isinstance(decision, dict) or decision.get("mode") != "audit" or decision.get("production_apply") is not False:
			raise ValueError("Only a provisional audit decision is accepted in Stage 5")
	else:
		decision = {"schema_version": 1, "mode": "audit", "production_apply": False, "provisional": True}
	if evidence_path is not None and not evidence_path.exists():
		raise ValueError("Archive evidence path does not exist")
	archive = archive_inventory or scan_archive(root, start, end)
	checkpoint: dict[str, object] = {}
	if checkpoint_path is not None and checkpoint_path.exists():
		value = json.loads(checkpoint_path.read_text(encoding="utf-8"))
		if isinstance(value, dict) and value.get("schema_version") == 1 and value.get("source") == _source_payload(ARCHIVE_SOURCE):
			checkpoint = value
	reports: list[dict[str, object]] = []
	database_available = database_config is not None
	database_revision = datetime.now(timezone.utc).isoformat() if database_available else None
	database_session_dates: set[date] = set()
	database_groups: Iterator[tuple[date, tuple[ArchiveBar, ...]]] = iter(())
	orphans: dict[str, int] | None = None
	connection = None
	cursor = None
	try:
		if database_config is not None:
			connection = streaming_connection(database_config)
			names = load_database_configuration(database_config).load_table_names()
			cursor = connection.cursor()
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			cursor.execute("START TRANSACTION READ ONLY")
			database_session_dates = _database_session_dates(cursor, names)
			orphans = _database_orphans(cursor, names)
			database_groups = _database_groups(cursor, names, start, end)
		current_group = next(database_groups, None)
		archive_dates = {date.fromisoformat(item.session) for item in archive.files}
		for item in archive.files:
			path = Path(item.path)
			session = date.fromisoformat(item.session)
			while current_group is not None and current_group[0] < session:
				reports.append({"session": current_group[0].isoformat(), "state": "database_only", "database_count": len(current_group[1]), "matches": False, "missing_keys": [], "extra_keys": [bar.ticker for bar in current_group[1]][:100]})
				current_group = next(database_groups, None)
			database_rows = current_group[1] if current_group is not None and current_group[0] == session else (() if current_group is None or current_group[0] > session else current_group[1])
			reports.append(_compare_session(item.session, path, item, database_rows if database_available else None))
			if current_group is not None and current_group[0] == session:
				current_group = next(database_groups, None)
		while current_group is not None:
			reports.append({"session": current_group[0].isoformat(), "state": "database_only", "database_count": len(current_group[1]), "matches": False, "missing_keys": [], "extra_keys": [bar.ticker for bar in current_group[1]][:100]})
			current_group = next(database_groups, None)
		for session in sorted(database_session_dates - archive_dates):
			if (start is None or session >= start) and (end is None or session <= end) and not any(item.get("session") == session.isoformat() for item in reports):
				reports.append({"session": session.isoformat(), "state": "database_only", "database_count": 0, "matches": False, "missing_keys": [], "extra_keys": []})
	finally:
		if cursor is not None:
			cursor.close()
		if connection is not None:
			connection.rollback()
			connection.close()
	valid_archive = len(archive.valid_files) == len(archive.files)
	complete = database_available and len(reports) >= len(archive.files) and all(item.get("state") != "changed_file" for item in reports)
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
		"complete": complete,
		"matches": complete and valid_archive and all(item.get("matches") is True for item in reports),
		"archive_file_count": len(archive.files),
		"valid_archive_file_count": len(archive.valid_files),
		"invalid_archive_files": [item.__dict__ for item in archive.invalid_files],
		"database_only_dates": sorted(item["session"] for item in reports if item.get("state") == "database_only"),
		"orphan_counts": orphans,
		"session_count": len(reports),
		"sessions": sorted(reports, key=lambda item: str(item.get("session"))),
		"checkpoint_reuse": 0,
		"scope_identity": content_id({"source": _source_payload(ARCHIVE_SOURCE), "files": [(item.session, item.file_sha256) for item in archive.files], "database_revision": database_revision}),
	}
	if checkpoint_path is not None:
		checkpoint_payload = {"schema_version": 1, "source": _source_payload(ARCHIVE_SOURCE), "scope_identity": result["scope_identity"], "sessions": {item["session"]: item for item in reports}}
		atomic_write(checkpoint_path, encode_json(checkpoint_payload))
	return result
