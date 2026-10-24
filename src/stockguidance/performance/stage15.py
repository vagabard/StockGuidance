from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.legacy_migration.precision import ExactArchiveBar
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.market_data.records import DailyBar
from stockguidance.performance.archive_read import ArchiveBar, archive_records_id, read_archive
from stockguidance.performance.stage12 import MIGRATION_COMPLETION, MIGRATION_RECEIPTS, _active_writers, _connect, _manifest_names
from stockguidance.performance.stage13 import _database_revision


STAGE15_SCHEMA_VERSION = 1
FAMILIES = ("price", "price_delta")
WINDOWS = (20, 60)
STATUS_CODES = tuple(range(6))
RAW_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "transactions")
Z_SCORE_TOLERANCE = {"rel_tol": 1e-5, "abs_tol": 1e-6}


class Stage15Error(ValueError):
	pass


@dataclass(frozen=True)
class Stage15Context:
	decision_path: Path
	decision: dict[str, Any]
	stage12_path: Path
	stage12: dict[str, Any]
	population_path: Path
	population: dict[str, Any]
	population_manifest_path: Path
	population_manifest: dict[str, Any]
	zscore_path: Path
	zscore: dict[str, Any]
	zscore_manifest_path: Path
	zscore_manifest: dict[str, Any]
	snapshot_path: Path
	snapshot: dict[str, Any]
	input_hashes: dict[str, str]


def _json(path: Path) -> dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
		raise Stage15Error(f"Cannot read Stage 15 artifact: {path}") from error
	if not isinstance(value, dict):
		raise Stage15Error(f"Stage 15 artifact is not an object: {path}")
	return value


def _sha256(path: Path) -> str:
	digest = sha256()
	try:
		with path.open("rb") as stream:
			for chunk in iter(lambda: stream.read(1024 * 1024), b""):
				digest.update(chunk)
	except OSError as error:
		raise Stage15Error(f"Cannot hash Stage 15 artifact: {path}") from error
	return digest.hexdigest()


def _require_file(path: Path, label: str) -> Path:
	resolved = path.resolve()
	if not resolved.is_file():
		raise Stage15Error(f"Missing {label}: {resolved}")
	return resolved


def _valid_files(manifest: dict[str, Any]) -> tuple[dict[str, Any], ...]:
	files = manifest.get("files")
	if not isinstance(files, list) or not files:
		raise Stage15Error("Stage 15 manifest has no retained files")
	result: list[dict[str, Any]] = []
	seen: set[str] = set()
	for item in files:
		if not isinstance(item, dict) or not isinstance(item.get("session"), str):
			raise Stage15Error("Stage 15 manifest contains an invalid file entry")
		session = str(item["session"])
		if session in seen:
			raise Stage15Error(f"Duplicate retained session: {session}")
		seen.add(session)
		result.append(item)
	if tuple(item["session"] for item in result) != tuple(sorted(item["session"] for item in result)):
		raise Stage15Error("Stage 15 manifest files are not chronological")
	return tuple(result)


def validate_prerequisites(
	decision_path: Path,
	stage12_path: Path,
	population_path: Path,
	population_manifest_path: Path,
	zscore_path: Path,
	zscore_manifest_path: Path,
	snapshot_path: Path,
) -> Stage15Context:
	paths = {
		"decision": _require_file(decision_path, "Stage 10 decision"),
		"stage12": _require_file(stage12_path, "Stage 12 report"),
		"population": _require_file(population_path, "Stage 13 population report"),
		"population_manifest": _require_file(population_manifest_path, "Stage 13 population manifest"),
		"zscore": _require_file(zscore_path, "Stage 14 z-score report"),
		"zscore_manifest": _require_file(zscore_manifest_path, "Stage 14 z-score manifest"),
		"snapshot": _require_file(snapshot_path, "Stage 5d snapshot"),
	}
	decision = _json(paths["decision"])
	stage12 = _json(paths["stage12"])
	population = _json(paths["population"])
	population_manifest = _json(paths["population_manifest"])
	zscore = _json(paths["zscore"])
	zscore_manifest = _json(paths["zscore_manifest"])
	snapshot = _json(paths["snapshot"])
	if decision.get("stage") != 10 or decision.get("status") != "complete" or decision.get("production_ready") is not False:
		raise Stage15Error("Stage 10 decision is not the frozen pre-production decision")
	if decision.get("production_backend_selected") is not True or decision.get("no_production_mutations") is not True:
		raise Stage15Error("Stage 10 production gate is missing")
	if stage12.get("stage") != 12 or stage12.get("status") != "complete" or stage12.get("production_ready") is not True:
		raise Stage15Error("Stage 12 gate is incomplete")
	if population.get("stage") != 13 or population.get("status") != "complete" or population.get("production_ready") is not True:
		raise Stage15Error("Stage 13 population gate is incomplete")
	if zscore.get("stage") != 14 or zscore.get("status") != "complete" or zscore.get("production_ready") is not True:
		raise Stage15Error("Stage 14 z-score gate is incomplete")
	if population.get("manifest_sha256") != _sha256(paths["population_manifest"]):
		raise Stage15Error("Stage 13 manifest hash is not bound by its population report")
	if zscore.get("manifest_sha256") != _sha256(paths["zscore_manifest"]):
		raise Stage15Error("Stage 14 manifest hash is not bound by its z-score report")
	if population_manifest.get("schema_version") != 1 or population_manifest.get("stage") != 6:
		raise Stage15Error("Stage 13 population manifest is invalid")
	if content_id({key: value for key, value in population_manifest.items() if key != "manifest_id"}) != population_manifest.get("manifest_id"):
		raise Stage15Error("Stage 13 population manifest identity changed")
	if zscore_manifest.get("zscore_population", {}).get("stage") != 14:
		raise Stage15Error("Stage 14 z-score manifest is invalid")
	if zscore.get("manifest_id") != zscore_manifest.get("manifest_id"):
		raise Stage15Error("Stage 14 report and manifest identities differ")
	if snapshot.get("schema_version") != 1 or snapshot.get("operation") != "offline_archive_snapshot" or snapshot.get("provenance_kind") != "adopted_archive_snapshot":
		raise Stage15Error("Stage 5d snapshot availability is incomplete")
	if stage12.get("restore", {}).get("status") not in {"restored", "restored_verified"}:
		raise Stage15Error("Stage 12 restore evidence is incomplete")
	if not all(bool(value) for value in stage12.get("pilot", {}).get("idempotency", {}).values()):
		raise Stage15Error("Stage 12 idempotency evidence is incomplete")
	_valid_files(population_manifest)
	_valid_files(zscore_manifest)
	input_hashes = {name: _sha256(path) for name, path in paths.items()}
	return Stage15Context(
		paths["decision"], decision, paths["stage12"], stage12, paths["population"], population,
		paths["population_manifest"], population_manifest, paths["zscore"], zscore,
		paths["zscore_manifest"], zscore_manifest, paths["snapshot"], snapshot, input_hashes,
	)


def _write_json(path: Path, value: dict[str, Any]) -> None:
	atomic_write(path.resolve(), encode_json(value))


def _hash_source_files(files: tuple[dict[str, Any], ...]) -> dict[str, Any]:
	items: list[dict[str, Any]] = []
	for item in files:
		path = Path(str(item.get("path"))).resolve()
		if not path.is_file():
			raise Stage15Error(f"Missing retained archive file: {path}")
		actual = _sha256(path)
		if actual != item.get("file_sha256"):
			raise Stage15Error(f"Changed retained archive file: {item.get('session')}")
		items.append({"session": str(item["session"]), "path": str(path), "file_sha256": actual, "valid": bool(item.get("valid")), "record_count": int(item.get("record_count", 0)), "records_id": item.get("records_id"), "finding": item.get("finding")})
	return {"checked_files": len(items), "valid_files": sum(item["valid"] for item in items), "excluded_files": [item for item in items if not item["valid"]], "files": items}


def _daily_as_archive(bar: DailyBar | ExactArchiveBar | ArchiveBar) -> ArchiveBar:
	return ArchiveBar(bar.ticker, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)


def _bar_difference(expected: ArchiveBar | ExactArchiveBar, actual: DailyBar | ExactArchiveBar) -> dict[str, dict[str, str | None]]:
	differences: dict[str, dict[str, str | None]] = {}
	for name in RAW_FIELDS:
		left, right = getattr(expected, name), getattr(actual, name)
		if left != right:
			differences[name] = {"archive": None if left is None else str(left), "database": None if right is None else str(right)}
	return differences


def _raw_result(session: str, archive: tuple[ArchiveBar | ExactArchiveBar, ...], actual: Mapping[str, DailyBar | ExactArchiveBar]) -> dict[str, Any]:
	expected_by_ticker = {bar.ticker: bar for bar in archive}
	missing = sorted(set(expected_by_ticker) - set(actual))
	extra = sorted(set(actual) - set(expected_by_ticker))
	difference_counts = {field: 0 for field in RAW_FIELDS}
	examples: list[dict[str, Any]] = []
	differing_rows = 0
	for ticker in sorted(set(expected_by_ticker) & set(actual)):
		differences = _bar_difference(expected_by_ticker[ticker], actual[ticker])
		if differences:
			differing_rows += 1
			for field in differences:
				difference_counts[field] += 1
			if len(examples) < 20:
				examples.append({"ticker": ticker, "fields": differences})
	return {
		"session": session, "archive_count": len(archive), "database_count": len(actual),
		"archive_id": archive_records_id(tuple(_daily_as_archive(bar) for bar in archive)), "database_id": archive_records_id(tuple(_daily_as_archive(bar) for bar in sorted(actual.values(), key=lambda value: value.ticker))),
		"missing_count": len(missing), "extra_count": len(extra), "missing_examples": missing[:20], "extra_examples": extra[:20],
		"differing_row_count": differing_rows, "field_difference_counts": difference_counts, "difference_examples": examples,
		"matches": not missing and not extra and differing_rows == 0,
	}




def _placeholders(values: Iterable[object]) -> str:
	items = tuple(values)
	if not items:
		raise Stage15Error("Stage 15 query scope is empty")
	return ",".join("%s" for _ in items)


def _date_ids(connection: Any, names: TableNames, sessions: tuple[str, ...]) -> dict[str, int]:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id, transaction_date FROM {quote_identifier(names.sessions)} WHERE transaction_date IN ({_placeholders(sessions)}) ORDER BY transaction_date, id", sessions)
		result: dict[str, int] = {}
		for identifier, session in cursor.fetchall():
			key = str(session)
			if key in result:
				raise Stage15Error(f"Duplicate date mapping: {key}")
			result[key] = int(identifier)
		return result
	finally:
		cursor.close()


def _imports(connection: Any, names: TableNames, sessions: tuple[str, ...], records: dict[str, str | None]) -> dict[str, bool]:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT session_date, raw_id, available_at FROM {quote_identifier(names.imports)} WHERE session_date IN ({_placeholders(sessions)}) ORDER BY session_date", sessions)
		result: dict[str, bool] = {}
		for session, raw_id, available_at in cursor.fetchall():
			key = str(session)
			result[key] = bool(records.get(key) and raw_id == records[key] and available_at is not None and str(available_at).strip())
		return result
	finally:
		cursor.close()


def _table_counts(connection: Any, names: TableNames) -> dict[str, Any]:
	materialized: list[str] = []
	for window in WINDOWS:
		resolved = names.resolve_zscore_tables((window,))[window]
		materialized.extend((resolved.price, resolved.price_delta))
	selected = list(dict.fromkeys([*names.tables(include_verification=True).values(), MIGRATION_RECEIPTS, MIGRATION_COMPLETION, *materialized]))
	cursor = connection.cursor()
	try:
		placeholders = _placeholders(selected)
		cursor.execute(f"SELECT TABLE_NAME, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME", tuple(selected))
		inventory = {str(row[0]): {"engine": row[1], "estimated_rows": None if row[2] is None else int(row[2]), "data_bytes": None if row[3] is None else int(row[3]), "index_bytes": None if row[4] is None else int(row[4])} for row in cursor.fetchall()}
		counts: dict[str, int] = {}
		for table in selected:
			if table not in inventory:
				continue
			cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}")
			counts[table] = int(cursor.fetchone()[0])
		return {"expected_tables": selected, "existing_tables": sorted(inventory), "missing_tables": sorted(set(selected) - set(inventory)), "counts": counts, "inventory": inventory}
	finally:
		cursor.close()


def _schema_snapshot(connection: Any, names: TableNames) -> dict[str, Any]:
	tables = [names.prices, names.tickers, names.sessions, names.deltas, names.imports, names.zscore_series, names.zscore_sessions]
	for window in WINDOWS:
		resolved = names.resolve_zscore_tables((window,))[window]
		tables.extend((resolved.price, resolved.price_delta))
	cursor = connection.cursor()
	try:
		placeholders = _placeholders(tables)
		cursor.execute(f"SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME", tuple(tables))
		engines = {str(row[0]): str(row[1]) for row in cursor.fetchall()}
		cursor.execute(f"SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ({placeholders}) ORDER BY TABLE_NAME, ORDINAL_POSITION", tuple(tables))
		columns = [list(row) for row in cursor.fetchall()]
		return {"engines": engines, "columns": columns, "corrected_raw_types": {str(row[1]): str(row[2]) for row in columns if row[0] == names.prices}, "ticker_collation": next((row[4] for row in columns if row[0] == names.tickers and row[1] == "label"), None), "sessions_engine": engines.get(names.sessions)}
	finally:
		cursor.close()


def _integrity(connection: Any, names: TableNames, files: tuple[dict[str, Any], ...], date_ids: dict[str, int], counts: dict[str, Any], audited_scope: dict[str, Any] | None = None) -> dict[str, Any]:
	file_sessions = {str(item["session"]) for item in files}
	valid_sessions = {str(item["session"]) for item in files if item.get("valid") is True}
	audited_scope = audited_scope or {}
	population = counts.get("counts", {})
	raw_audit = audited_scope.get("raw_derived", {})
	raw_sessions = raw_audit.get("raw_sessions", [])
	derived_sessions = [item for item in raw_audit.get("derived_sessions", []) if item.get("state") == "complete"]
	raw_proven = bool(raw_sessions) and len(raw_sessions) == len(valid_sessions) and raw_audit.get("raw_engine") == "exact-archive-v2" and {item["session"] for item in raw_sessions} == valid_sessions and all(item.get("matches") is True for item in raw_sessions) and sum(int(item["database_count"]) for item in raw_sessions) == population.get(names.prices)
	derived_proven = raw_proven and {item["session"] for item in derived_sessions} <= valid_sessions and all(item.get("matches") is True for item in derived_sessions) and sum(int(item["stored_count"]) for item in derived_sessions) == population.get(names.deltas)
	proven_counts = {}
	if raw_proven:
		proven_counts.update({"raw_without_ticker": 0, "raw_without_date": 0})
	if derived_proven:
		proven_counts.update({"derived_without_ticker": 0, "derived_without_date": 0})
	if raw_proven and derived_proven:
		proven_counts.update({"raw_without_derived_key": int(population[names.prices]) - int(population[names.deltas]), "derived_without_raw_key": 0})
	previous_database = audited_scope.get("database", {})
	reused_checks: list[str] = []
	cached_counts: dict[str, int] = {}
	if isinstance(audited_scope.get("revision_before"), str) and previous_database.get("read_only") is True and previous_database.get("revision_before") == previous_database.get("revision_after") == audited_scope.get("revision_before") and previous_database.get("table_populations", {}).get("counts") == population:
		for check, value in previous_database.get("integrity", {}).get("orphan_counts", {}).items():
			cached_counts[check] = int(value)
	zscore_proofs = {}
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id, label FROM {quote_identifier(names.tickers)} ORDER BY id")
		tickers = [(int(row[0]), str(row[1])) for row in cursor.fetchall()]
		cursor.execute(f"SELECT id, transaction_date FROM {quote_identifier(names.sessions)} ORDER BY id")
		mapped_dates = [(int(row[0]), str(row[1])) for row in cursor.fetchall()]
		orphan_queries = {
			"raw_without_ticker": f"SELECT COUNT(*) FROM {quote_identifier(names.prices)} r LEFT JOIN {quote_identifier(names.tickers)} s ON s.id=r.ticker_id WHERE s.id IS NULL",
			"derived_without_ticker": f"SELECT COUNT(*) FROM {quote_identifier(names.deltas)} r LEFT JOIN {quote_identifier(names.tickers)} s ON s.id=r.ticker_id WHERE s.id IS NULL",
			"raw_without_date": f"SELECT COUNT(*) FROM {quote_identifier(names.prices)} r LEFT JOIN {quote_identifier(names.sessions)} s ON s.id=r.date_id WHERE s.id IS NULL",
			"derived_without_date": f"SELECT COUNT(*) FROM {quote_identifier(names.deltas)} r LEFT JOIN {quote_identifier(names.sessions)} s ON s.id=r.date_id WHERE s.id IS NULL",
			"raw_without_derived_key": f"SELECT COUNT(*) FROM {quote_identifier(names.prices)} r LEFT JOIN {quote_identifier(names.deltas)} d ON d.ticker_id=r.ticker_id AND d.date_id=r.date_id WHERE d.ticker_id IS NULL",
			"derived_without_raw_key": f"SELECT COUNT(*) FROM {quote_identifier(names.deltas)} d LEFT JOIN {quote_identifier(names.prices)} r ON r.ticker_id=d.ticker_id AND r.date_id=d.date_id WHERE r.ticker_id IS NULL",
		}
		orphans: dict[str, int] = {}
		for name, query in orphan_queries.items():
			if name in proven_counts:
				orphans[name] = proven_counts[name]
			elif name in cached_counts:
				orphans[name] = cached_counts[name]
				reused_checks.append(name)
			else:
				cursor.execute(query)
				orphans[name] = int(cursor.fetchone()[0])
		for window in WINDOWS:
			resolved = names.resolve_zscore_tables((window,))[window]
			for family, table in (("price", resolved.price), ("price_delta", resolved.price_delta)):
				zscore = audited_scope.get("zscore", {})
				proven = zscore.get("status") == "complete" and zscore.get("mismatch_count") == 0 and isinstance(zscore.get("expected_rows"), int) and zscore["expected_rows"] > 0 and zscore.get("actual_rows", {}).get(f"{family}/{window}") == population.get(table) and population.get(table) == zscore.get("expected_rows")
				zscore_proofs[table] = proven
				if proven:
					ticker_orphans, date_orphans = 0, 0
				else:
					cursor.execute(f"SELECT COALESCE(SUM(t.id IS NULL),0), COALESCE(SUM(s.id IS NULL),0) FROM {quote_identifier(table)} z LEFT JOIN {quote_identifier(names.tickers)} t ON t.id=z.ticker_id LEFT JOIN {quote_identifier(names.sessions)} s ON s.id=z.date_id")
					ticker_orphans, date_orphans = cursor.fetchone()
				orphans[f"{table}_without_ticker"] = int(ticker_orphans)
				orphans[f"{table}_without_date"] = int(date_orphans)
		all_mapped_dates_retained = {session for _, session in mapped_dates} <= file_sessions
		database_only_raw = 0
		if not raw_proven and not all_mapped_dates_retained:
			cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(names.prices)} r JOIN {quote_identifier(names.sessions)} s ON s.id=r.date_id WHERE s.transaction_date NOT IN ({_placeholders(file_sessions)})", tuple(file_sessions))
			database_only_raw = int(cursor.fetchone()[0])
		database_only_derived = 0
		if not derived_proven and not all_mapped_dates_retained:
			cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(names.deltas)} r JOIN {quote_identifier(names.sessions)} s ON s.id=r.date_id WHERE s.transaction_date NOT IN ({_placeholders(file_sessions)})", tuple(file_sessions))
			database_only_derived = int(cursor.fetchone()[0])
		mapping = {
			"ticker_count": len(tickers), "date_count": len(mapped_dates),
			"identity": {"tickers": content_id(tickers), "dates": content_id(mapped_dates)},
			"missing_retained_dates": sorted(file_sessions - {session for _, session in mapped_dates}),
			"database_only_dates": sorted({session for _, session in mapped_dates} - file_sessions),
			"valid_dates_without_mapping": sorted(valid_sessions - set(date_ids)),
		}
		return {"mapping": mapping, "orphan_counts": orphans, "database_only_rows": {"raw": database_only_raw, "derived": database_only_derived}, "table_counts": counts, "scope_proofs": {"method": "exact global counts equal independently validated complete keyspace; otherwise SQL scan", "raw": raw_proven, "derived": derived_proven, "zscores": zscore_proofs, "all_mapped_dates_retained": all_mapped_dates_retained, "reused_sql_checks": reused_checks}}
	finally:
		cursor.close()




def _float32(value: float) -> float:
	import struct
	try:
		return float(struct.unpack("!f", struct.pack("!f", value))[0])
	except (OverflowError, struct.error) as error:
		raise Stage15Error("Semantic z-score is not representable as float32") from error


def _number(value: object | None) -> float | None:
	if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
		return None
	try:
		converted = float(value)
	except (TypeError, ValueError, OverflowError):
		return None
	return converted if math.isfinite(converted) else None


def _close_issue(value: object | None, verified: bool) -> int:
	if not verified:
		return 3
	number = _number(value)
	return 0 if number is not None and number > 0.0 else 2 if value is None else 4


def _higher(first: int | None, second: int | None) -> int | None:
	if first is None:
		return second
	if second is None:
		return first
	return first if first >= second else second


def _semantic_score(
	family: str,
	window: int,
	dates: tuple[str, ...],
	values: dict[tuple[int, int], tuple[object | None, object | None]],
	verified: dict[int, bool],
	date_ids_by_session: dict[str, int],
	ticker_id: int,
) -> tuple[int, float | None]:
	target_index = len(dates) - 1
	first = max(0, target_index - window + 1)
	issue: int | None = None
	numeric_values: list[float] = []
	for index in range(first, target_index + 1):
		date_id = date_ids_by_session.get(dates[index])
		current, ratio = (None, None) if date_id is None else values.get((date_id, ticker_id), (None, None))
		current_number = _number(current)
		current_issue = _close_issue(current, bool(date_id is not None and verified.get(date_id, False)))
		if family == "price":
			issue = _higher(issue, current_issue)
			if current_issue == 0:
				assert current_number is not None
				numeric_values.append(current_number)
			continue
		if index == 0:
			current_issue = max(current_issue, 1)
		else:
			prior_id = date_ids_by_session.get(dates[index - 1])
			prior_close, _ = (None, None) if prior_id is None else values.get((prior_id, ticker_id), (None, None))
			prior_number = _number(prior_close)
			prior_issue = _close_issue(prior_close, bool(prior_id is not None and verified.get(prior_id, False)))
			current_issue = max(current_issue, prior_issue)
			ratio_number = _number(ratio)
			if current_issue == 0:
				assert current_number is not None and prior_number is not None
				if ratio is None:
					current_issue = 2
				elif ratio_number is None or ratio_number <= 0.0 or not math.isclose(ratio_number, current_number / prior_number, rel_tol=1e-10, abs_tol=1e-12):
					current_issue = 4
				else:
					numeric_values.append(ratio_number - 1.0)
		issue = _higher(issue, current_issue)
	if target_index + 1 < window:
		issue = _higher(issue, 1)
	if issue is not None and issue != 0:
		return issue, None
	if len(numeric_values) != window:
		return 2, None
	if all(value == numeric_values[0] for value in numeric_values):
		return 5, None
	mean = math.fsum(numeric_values) / window
	variance = math.fsum((value - mean) * (value - mean) for value in numeric_values) / window
	if not math.isfinite(variance) or variance <= 0.0:
		return 5, None
	return 0, _float32((numeric_values[-1] - mean) / math.sqrt(variance))


def _zscore_metadata(connection: Any, names: TableNames, manifest: dict[str, Any], target_by_id: dict[int, dict[str, Any]], expected_counts: dict[tuple[int, str, int], dict[int, int]], ticker_count: int, published_metadata: list[dict[str, Any]] | None = None) -> dict[str, Any]:
	operational = manifest.get("kind") == "daily-maintenance"
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT series_key, schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy FROM {quote_identifier(names.zscore_series)} ORDER BY series_key")
		series_rows = [list(row) for row in cursor.fetchall()]
		cursor.execute(f"SELECT series_key, date_id, row_count, ready_count, status_counts, content_digest, dependency_digest, availability_digest, actual_input_available FROM {quote_identifier(names.zscore_sessions)} ORDER BY series_key, date_id")
		metadata_rows = [list(row) for row in cursor.fetchall()]
	finally:
		cursor.close()
	source = manifest.get("source")
	archive_source = source.get("archive_source") if isinstance(source, dict) else None
	if not isinstance(archive_source, dict):
		raise Stage15Error("Stage 14 manifest is missing its archive source scope")
	source_scope = content_id(archive_source)
	series_expected: dict[str, tuple[str, int, tuple[Any, ...]]] = {}
	for family, window in ((family, window) for family in FAMILIES for window in WINDOWS):
		definition = {"schema_version": 1, "source_scope": source_scope, "calendar_id": "XNYS", "family": family, "window": window, "calculation_version": "rolling-population-zscore-v1", "delta_definition": "close_ratio_to_prior_minus_one" if family == "price_delta" else "not_applicable", "precision": "float32", "missing_value_policy": "null_status_v1"}
		expected_fields = tuple(definition[field] for field in ("schema_version", "source_scope", "calendar_id", "family", "window", "calculation_version", "delta_definition", "precision", "missing_value_policy"))
		series_expected[content_id(definition)] = (family, window, expected_fields)
	if operational:
		series_rows = [row for row in series_rows if str(row[0]) in series_expected]
		metadata_rows = [row for row in metadata_rows if str(row[0]) in series_expected and int(row[1]) in target_by_id]
	series_findings: list[dict[str, Any]] = []
	for row in series_rows:
		key = str(row[0])
		expected_series = series_expected.get(key)
		if expected_series is None:
			series_findings.append({"series_key": key, "finding": "series_definition_mismatch"})
		elif tuple(row[1:]) != expected_series[2]:
			series_findings.append({"series_key": key, "finding": "series_definition_mismatch"})
	metadata_findings: list[dict[str, Any]] = []
	published = {(item["session"], item["family"], int(item["window"])): item for item in published_metadata or ()}
	for row in metadata_rows:
		series_key, date_id = str(row[0]), int(row[1])
		series = series_expected.get(series_key)
		if series is None or date_id not in target_by_id:
			metadata_findings.append({"series_key": series_key, "date_id": date_id, "finding": "out_of_scope_metadata"})
			continue
		family, window, _ = series
		try:
			payload = str(row[4])
			stored_counts = {int(code): int(count) for code, count in json.loads(payload).items()} if payload.startswith("{") else {int(part.split(":", 1)[0]): int(part.split(":", 1)[1]) for part in payload.split(",") if ":" in part}
		except (TypeError, ValueError):
			stored_counts = {}
		expected = expected_counts[(date_id, family, window)]
		expected_total = sum(expected.values())
		if int(row[2]) != ticker_count or int(row[3]) != expected[0] or stored_counts != expected or not isinstance(row[5], str) or len(str(row[5])) != 64 or not isinstance(row[6], str) or len(str(row[6])) != 64 or not isinstance(row[7], str) or len(str(row[7])) != 64 or expected_total != ticker_count:
			metadata_findings.append({"series_key": series_key, "date_id": date_id, "finding": "metadata_count_or_digest_shape_mismatch"})
		if not operational and bool(row[8]):
			metadata_findings.append({"series_key": series_key, "date_id": date_id, "finding": "archive_snapshot_must_not_claim_historical_availability"})
		if published_metadata is not None:
			reference = published.get((target_by_id[date_id]["session"], family, window))
			if reference is None or any(str(row[index]) != reference.get(field) for index, field in ((5, "content_digest"), (6, "dependency_digest"), (7, "availability_digest"))):
				metadata_findings.append({"series_key": series_key, "date_id": date_id, "finding": "published_digest_binding_mismatch"})
	if len(series_rows) != len(series_expected) or len(metadata_rows) != len(target_by_id) * len(series_expected):
		metadata_findings.append({"finding": "missing_or_extra_series_metadata"})
	return {"validation_version": 2 if published_metadata is not None else 1, "digest_reference": "frozen Stage 14 publication report; values and status counts independently recomputed" if published_metadata is not None else "shape_only", "series_rows": len(series_rows), "metadata_rows": len(metadata_rows), "expected_series": len(series_expected), "expected_metadata": len(target_by_id) * len(series_expected), "series_findings": series_findings, "metadata_findings": metadata_findings}




def _recovery_evidence(stage12: dict[str, Any]) -> dict[str, Any]:
	return {"status": "passed", "restore_status": stage12.get("restore", {}).get("status"), "pilot_idempotency": stage12.get("pilot", {}).get("idempotency", {}), "source": "Stage 11b recovery and Stage 12 restore/pilot evidence; Stage 15 performs no production recovery writes"}


def run_stage15(
	context: Stage15Context,
	dbinfo_file: Path,
	data_root: Path,
	output: Path,
	*,
	zscore_batch_size: int = 256,
	resume: bool = True,
	workers: int = 5,
	fetch_size: int = 1000,
	sessions_per_batch: int = 128,
	cache_slots: int = 2,
	max_batches: int | None = None,
	benchmark_only: bool = False,
	progress_seconds: float = 10.0,
) -> dict[str, Any]:
	from stockguidance.performance.vectorized_zscore_validation import PipelineOptions, benchmark, run_pipeline

	options = PipelineOptions(workers, zscore_batch_size, fetch_size, sessions_per_batch, cache_slots, max_batches, progress_seconds)
	if not data_root.resolve().is_dir():
		raise Stage15Error(f"Missing retained data root: {data_root.resolve()}")
	if type(zscore_batch_size) is not int or not 1 <= zscore_batch_size <= 1000:
		raise Stage15Error("Stage 15 z-score batch size must be between one and one thousand tickers")
	files = _valid_files(context.zscore_manifest)
	source = _hash_source_files(files)
	output = output.resolve()
	state: dict[str, Any] | None = _json(output) if resume and output.exists() else None
	if state is not None and (state.get("schema_version") != STAGE15_SCHEMA_VERSION or state.get("stage") != 15 or state.get("input_hashes") != context.input_hashes):
		raise Stage15Error("Existing Stage 15 report belongs to different prerequisites")
	from stockguidance.database.configuration import load_database_configuration
	production = load_database_configuration(dbinfo_file)
	names = _manifest_names(context.zscore_manifest)
	connection = _connect(dbinfo_file, production.database)
	started = perf_counter()
	try:
		if _active_writers(connection):
			raise Stage15Error("Competing production writers are active")
		revision_before = _database_revision(connection, names)
		if state is not None and state.get("revision_before") != revision_before:
			raise Stage15Error("Database revision differs from the Stage 15 checkpoint")
		connection.rollback()
		cursor = connection.cursor()
		try:
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			cursor.execute("START TRANSACTION READ ONLY")
		finally:
			cursor.close()
		all_sessions = tuple(str(item["session"]) for item in files)
		date_ids = _date_ids(connection, names, all_sessions)
		records = {str(item["session"]): item.get("records_id") for item in files}
		verified = _imports(connection, names, all_sessions, records)
		verified_by_id = {date_ids[session]: verified.get(session, False) for session in date_ids}
		if benchmark_only:
			return benchmark(connection, names, context.zscore_manifest, date_ids, verified_by_id, output.with_name("stage15-benchmark.json"), options)
		if state is None or state.get("raw_derived", {}).get("status") != "complete" or state.get("raw_derived", {}).get("raw_engine") != "exact-archive-v2":
			from stockguidance.performance.stage15_derived import run_derived

			raw_results = run_derived(connection, names, context.population_manifest, date_ids, output.parent / "stage15-raw-checkpoints", {"inputs": context.input_hashes, "database_revision": revision_before}, workers, fetch_size, raw=True, max_sessions=max_batches, progress_seconds=progress_seconds)
			raw_complete = len(raw_results) == sum(item.get("valid") is True for item in files)
			prior_zscore = state.get("zscore", {"status": "pending"}) if state is not None else {"status": "pending"}
			state = {"schema_version": STAGE15_SCHEMA_VERSION, "stage": 15, "status": "running" if raw_complete else "partial", "production_ready": False, "input_hashes": context.input_hashes, "source": source, "revision_before": revision_before, "raw_derived": {"status": "complete" if raw_complete else "partial", "raw_engine": "exact-archive-v2", "raw_sessions": raw_results, "derived_sessions": []}, "zscore": prior_zscore}
			_write_json(output, state)
			if not raw_complete:
				return state
		if state.get("zscore", {}).get("status") != "complete" or state.get("zscore", {}).get("metadata", {}).get("validation_version") != 2:
			state["zscore"] = {"status": "running", "batch_size": zscore_batch_size}
			_write_json(output, state)
			state["zscore"] = run_pipeline(connection, names, context.zscore_manifest, date_ids, verified_by_id, output.parent / "stage15-checkpoints", {"inputs": context.input_hashes, "database_revision": revision_before}, options, published_metadata=context.zscore["series"], metadata_validator=_zscore_metadata)
			_write_json(output, state)
			if state["zscore"]["status"] != "complete":
				state["status"] = "partial"
				_write_json(output, state)
				return state
		if len(state["raw_derived"]["derived_sessions"]) != len(context.population_manifest["targets"]):
			from stockguidance.performance.stage15_derived import run_derived

			state["raw_derived"]["derived_sessions"] = run_derived(connection, names, context.population_manifest, date_ids, output.parent / "stage15-derived-checkpoints", {"inputs": context.input_hashes, "database_revision": revision_before}, workers, fetch_size, max_sessions=max_batches, progress_seconds=progress_seconds)
			_write_json(output, state)
			if len(state["raw_derived"]["derived_sessions"]) != len(context.population_manifest["targets"]):
				state["status"] = "partial"
				_write_json(output, state)
				return state
		print("Stage 15 final integrity: counting support and materialization tables", flush=True)
		counts = _table_counts(connection, names)
		print("Stage 15 final integrity: checking mappings, orphan rows, and schema", flush=True)
		integrity = _integrity(connection, names, files, date_ids, counts, state)
		schema = _schema_snapshot(connection, names)
		connection.rollback()
		if _active_writers(connection):
			raise Stage15Error("A competing production writer appeared during the Stage 15 audit")
		final_revision = _database_revision(connection, names)
		if final_revision != revision_before:
			raise Stage15Error("Database revision changed during the Stage 15 audit")
	finally:
		try:
			connection.rollback()
		finally:
			connection.close()
	raw_matches = all(bool(item.get("matches")) for item in state["raw_derived"]["raw_sessions"])
	derived_checked = [item for item in state["raw_derived"]["derived_sessions"] if item.get("state") == "complete"]
	derived_complete = len(derived_checked) == sum(len(item.get("derived_dependencies", ())) == 2 for item in context.population_manifest["targets"]) and all(item.get("matches") is True for item in derived_checked)
	zscore = state["zscore"]
	zscore_matches = int(zscore.get("mismatch_count", 1)) == 0 and not zscore.get("metadata", {}).get("series_findings") and not zscore.get("metadata", {}).get("metadata_findings")
	known_gaps = list(source["excluded_files"])
	known_gaps.extend(item for item in state["raw_derived"]["derived_sessions"] if item.get("state") != "complete")
	known_gaps.extend({"category": "database_only_date", "session": session} for session in integrity["mapping"]["database_only_dates"])
	known_gaps.extend({"category": "missing_retained_date_mapping", "session": session} for session in integrity["mapping"]["missing_retained_dates"])
	known_gaps.extend({"category": "orphan", "check": name, "count": count} for name, count in integrity["orphan_counts"].items() if count)
	known_gaps.extend({"category": "database_only_rows", "check": name, "count": count} for name, count in integrity["database_only_rows"].items() if count)
	unresolved = {"known_gaps": known_gaps, "raw_mismatches": [item for item in state["raw_derived"]["raw_sessions"] if not item.get("matches")], "derived_mismatches": [item for item in derived_checked if item.get("matches") is not True], "zscore_mismatches": zscore.get("mismatches", []), "integrity": {"mapping": integrity["mapping"], "orphan_counts": integrity["orphan_counts"], "database_only_rows": integrity["database_only_rows"], "missing_tables": integrity["table_counts"]["missing_tables"]}}
	state.update({
		"status": "complete", "production_ready": raw_matches and derived_complete and zscore_matches and not integrity["table_counts"]["missing_tables"] and not any(integrity["orphan_counts"].values()),
		"acceptance": {"exact_adopted_raw_agreement": raw_matches, "derived_reference_valid": derived_complete, "zscore_reference_valid": zscore_matches, "support_table_counts_complete": not integrity["table_counts"]["missing_tables"], "recovery_tested": True, "unresolved_gaps_reported": True, "fully_verified_continuous_coverage": not known_gaps, "audit_passed": raw_matches and derived_complete and zscore_matches and not unresolved["raw_mismatches"] and not unresolved["derived_mismatches"] and not unresolved["zscore_mismatches"]},
		"coverage": {"retained_files": len(files), "valid_archive_sessions": sum(item.get("valid") is True for item in files), "raw_sessions_checked": len(state["raw_derived"]["raw_sessions"]), "derived_sessions_checked": len(state["raw_derived"]["derived_sessions"]), "zscore_target_sessions": zscore.get("target_sessions"), "zscore_tickers": zscore.get("ticker_count")},
		"database": {"revision_before": revision_before, "revision_after": final_revision, "read_only": True, "schema": schema, "table_populations": integrity["table_counts"], "integrity": integrity},
		"source_hash_audit": source, "unresolved": unresolved, "recovery": _recovery_evidence(context.stage12), "elapsed_seconds": perf_counter() - started,
		"handoff": {"next": "operating maintenance", "resume": "Rerun the Stage 15 command with identical inputs and batch geometry; completed checksummed z-score batches and derived sessions are reused after database revision checks. Omit --stage15-max-batches to finish the full scope.", "selected_backends": context.decision.get("rollout", {}).get("production_entry_points", {}), "evidence_semantics": "operator-attested retained archive files and adoption receipts prove the local snapshot at the recorded cutoff; they do not prove original historical availability", "live_refresh_separate": True, "training_simulation_separate": True},
	})
	_write_json(output, state)
	return state
