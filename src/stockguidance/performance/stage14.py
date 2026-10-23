from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from contextlib import contextmanager
from functools import wraps
from hashlib import sha256
import json
import math
from pathlib import Path
import random
import struct
import subprocess
import sys
from time import perf_counter
from typing import Any, Callable, Iterable, Iterator, cast

from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.performance.stage12 import _active_writers, _connect, _manifest_names
from stockguidance.performance.stage13 import _database_revision


STAGE14_SCHEMA_VERSION = 1
FAMILIES = ("price", "price_delta")
WINDOWS = (20, 60)
MAX_CHUNK_SIZE = 20
MAX_WINDOW = 60
STATUS_PRIORITY = {1: 1, 2: 2, 3: 3, 4: 4}


class Stage14Error(ValueError):
	pass


@contextmanager
def _output_lock(output: Path) -> Iterator[None]:
	lock_path = output.resolve().parent / "stage14" / "run.lock"
	lock_path.parent.mkdir(parents=True, exist_ok=True)
	with lock_path.open("a+b") as stream:
		if sys.platform == "win32":
			import msvcrt
			try:
				stream.seek(0)
				msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
			except OSError as error:
				raise Stage14Error("Another Stage 14 coordinator holds the run lock") from error
			try:
				yield
			finally:
				stream.seek(0)
				msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
		else:
			import fcntl
			try:
				fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
			except OSError as error:
				raise Stage14Error("Another Stage 14 coordinator holds the run lock") from error
			try:
				yield
			finally:
				fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _exclusive_output(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
	@wraps(function)
	def locked(context: Stage14Context, dbinfo_file: Path, output: Path, **kwargs: Any) -> dict[str, Any]:
		with _output_lock(output):
			try:
				return function(context, dbinfo_file, output, **kwargs)
			except BaseException:
				close = getattr(kwargs.get("java_runner"), "close", None)
				if close is not None:
					close()
				raise
	return locked


@dataclass(frozen=True)
class Stage14Context:
	decision_path: Path
	decision: dict[str, Any]
	stage12_path: Path
	stage12: dict[str, Any]
	stage13_path: Path
	stage13: dict[str, Any]
	stage13_manifest_path: Path
	stage13_manifest: dict[str, Any]
	snapshot_path: Path
	snapshot: dict[str, Any]
	decision_sha256: str
	stage12_sha256: str
	stage13_sha256: str
	stage13_manifest_sha256: str
	snapshot_sha256: str


def _json(path: Path) -> dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
		raise Stage14Error(f"Cannot read JSON artifact: {path}") from error
	if not isinstance(value, dict):
		raise Stage14Error(f"JSON artifact is not an object: {path}")
	return value


def _sha256(path: Path) -> str:
	digest = sha256()
	try:
		with path.open("rb") as stream:
			for chunk in iter(lambda: stream.read(1024 * 1024), b""):
				digest.update(chunk)
	except OSError as error:
		raise Stage14Error(f"Cannot hash required artifact: {path}") from error
	return digest.hexdigest()


def _require_file(path: Path, label: str) -> Path:
	path = path.resolve()
	if not path.is_file():
		raise Stage14Error(f"Missing {label}: {path}")
	return path


def _aware(value: object, label: str) -> datetime:
	try:
		result = datetime.fromisoformat(str(value))
	except (TypeError, ValueError) as error:
		raise Stage14Error(f"Invalid {label}") from error
	if result.tzinfo is None or result.utcoffset() is None:
		raise Stage14Error(f"{label} must include a timezone")
	return result


def _valid_files(manifest: dict[str, Any]) -> tuple[dict[str, Any], ...]:
	files = manifest.get("files")
	if not isinstance(files, list) or not files:
		raise Stage14Error("Stage 13 manifest has no retained files")
	seen: set[str] = set()
	result: list[dict[str, Any]] = []
	for item in files:
		if not isinstance(item, dict) or not isinstance(item.get("session"), str):
			raise Stage14Error("Stage 13 manifest contains an invalid file entry")
		session = str(item["session"])
		if session in seen:
			raise Stage14Error(f"Duplicate retained session: {session}")
		seen.add(session)
		result.append(item)
	if tuple(item["session"] for item in result) != tuple(sorted(item["session"] for item in result)):
		raise Stage14Error("Stage 13 manifest files are not chronological")
	return tuple(result)


def build_zscore_manifest(base: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
	files = _valid_files(base)
	valid_sessions = tuple(str(item["session"]) for item in files if item.get("valid") is True)
	if not valid_sessions:
		raise Stage14Error("Stage 13 manifest has no valid archive sessions")
	base_targets = {str(item.get("session")): item for item in base.get("targets", ()) if isinstance(item, dict)}
	targets: list[dict[str, Any]] = []
	for index, item in enumerate(files):
		if item.get("valid") is not True:
			continue
		session = str(item["session"])
		base_target = base_targets.get(session)
		if base_target is None:
			raise Stage14Error(f"Stage 13 target is missing: {session}")
		dependencies = [str(value["session"]) for value in files[max(0, index - MAX_WINDOW):index + 1]]
		targets.append({
			"session": session,
			"roles": ["retained_history"],
			"derived_dependencies": list(base_target.get("derived_dependencies", ())),
			"zscore_dependencies": dependencies,
		})
	manifest = {key: value for key, value in base.items() if key != "manifest_id"}
	manifest["targets"] = targets
	manifest["fixed_targets"] = []
	manifest["target_blocks"] = {"zscore": list(valid_sessions)}
	manifest["zscore_population"] = {
		"stage": 14, "families": list(FAMILIES), "windows": list(WINDOWS),
		"dependency_sessions": MAX_WINDOW + 1, "requested_sessions": len(valid_sessions),
		"invalid_archive_sessions": len(files) - len(valid_sessions),
	}
	manifest["manifest_id"] = content_id(manifest)
	return manifest, {
		"requested_sessions": len(valid_sessions),
		"invalid_archive_sessions": len(files) - len(valid_sessions),
		"minimum_dependency_count": min(len(item["zscore_dependencies"]) for item in targets),
		"maximum_dependency_count": max(len(item["zscore_dependencies"]) for item in targets),
	}


def _validate_pipeline_dependencies(manifest: dict[str, Any]) -> None:
	files = tuple(str(item["session"]) for item in manifest["files"])
	positions = {session: index for index, session in enumerate(files)}
	for target in manifest["targets"]:
		session = str(target["session"])
		index = positions[session]
		if index >= MAX_WINDOW:
			needed = files[index - MAX_WINDOW:index + 1]
			actual = tuple(str(value) for value in target["zscore_dependencies"])
			if actual[-len(needed):] != needed:
				raise Stage14Error(f"Dependency correction required before pipeline publication: {session} needs 61 raw sessions to validate 60 price_delta inputs; the existing manifest/checkpoint needs review")


def validate_prerequisites(
	decision_path: Path,
	stage12_path: Path,
	stage13_path: Path,
	stage13_manifest_path: Path,
	snapshot_path: Path,
) -> Stage14Context:
	decision_path = _require_file(decision_path, "Stage 10 decision")
	stage12_path = _require_file(stage12_path, "Stage 12 report")
	stage13_path = _require_file(stage13_path, "Stage 13 population report")
	stage13_manifest_path = _require_file(stage13_manifest_path, "Stage 13 population manifest")
	snapshot_path = _require_file(snapshot_path, "Stage 5d snapshot")
	decision, stage12, stage13 = _json(decision_path), _json(stage12_path), _json(stage13_path)
	manifest, snapshot = _json(stage13_manifest_path), _json(snapshot_path)
	if decision.get("stage") != 10 or decision.get("status") != "complete" or decision.get("production_ready") is not False:
		raise Stage14Error("Stage 10 decision is not the frozen pre-production decision")
	if decision.get("production_backend_selected") is not True or decision.get("no_production_mutations") is not True:
		raise Stage14Error("Stage 10 production gate is missing")
	if decision.get("workloads", {}).get("zscores_persistence", {}).get("selected_backend") != "java":
		raise Stage14Error("Stage 10 did not select the Java z-score backend")
	if stage12.get("stage") != 12 or stage12.get("status") != "complete" or stage12.get("production_ready") is not True or stage12.get("production_access", {}).get("dml") is not True:
		raise Stage14Error("Stage 12 production gate is incomplete")
	if stage13.get("schema_version") != 1 or stage13.get("stage") != 13 or stage13.get("status") != "complete" or stage13.get("production_ready") is not True:
		raise Stage14Error("Stage 13 population gate is incomplete")
	if stage13.get("manifest_sha256") != _sha256(stage13_manifest_path):
		raise Stage14Error("Stage 13 manifest hash is not bound by its population report")
	if manifest.get("schema_version") != 1 or manifest.get("stage") != 6 or content_id({key: value for key, value in manifest.items() if key != "manifest_id"}) != manifest.get("manifest_id"):
		raise Stage14Error("Stage 13 population manifest is invalid")
	if stage13.get("coverage", {}).get("adopted_raw_sessions") != stage13.get("coverage", {}).get("valid_archive_sessions") or stage13.get("receipts", {}).get("raw_receipts") != stage13.get("coverage", {}).get("adopted_raw_sessions"):
		raise Stage14Error("Stage 13 raw coverage or receipts are incomplete")
	if snapshot.get("schema_version") != 1 or snapshot.get("operation") != "offline_archive_snapshot" or snapshot.get("provenance_kind") != "adopted_archive_snapshot":
		raise Stage14Error("Stage 5d snapshot availability is incomplete")
	if _aware(snapshot.get("cutoff"), "Stage 5d snapshot cutoff") > _aware(stage13.get("prerequisites", {}).get("cutoff"), "Stage 13 cutoff"):
		raise Stage14Error("Stage 5d snapshot is newer than the Stage 13 cutoff")
	_valid_files(manifest)
	return Stage14Context(
		decision_path, decision, stage12_path, stage12, stage13_path, stage13, stage13_manifest_path,
		manifest, snapshot_path, snapshot, _sha256(decision_path), _sha256(stage12_path), _sha256(stage13_path),
		_sha256(stage13_manifest_path), _sha256(snapshot_path),
	)


def _write_json(path: Path, value: dict[str, Any]) -> None:
	atomic_write(path.resolve(), encode_json(value))


def _java_classpath() -> str:
	java_root = Path(__file__).resolve().parents[3] / "java"
	return ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))


def _run_java(dbinfo_file: Path, manifest_path: Path, sessions: tuple[str, ...], checkpoint_directory: Path, timeout: float) -> dict[str, Any]:
	command = [
		"java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "-cp", _java_classpath(),
		"stockguidance.migration.JavaZScoreBenchmarkMain", "--manifest", str(manifest_path.resolve()),
		"--dbinfo-file", str(dbinfo_file.resolve()), "--production", "true", "--sessions", ",".join(sessions),
		"--checkpoint-directory", str(checkpoint_directory.resolve()),
	]
	import os
	environment = dict(os.environ)
	environment.update({"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1"})
	try:
		completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, env=environment)
	except (OSError, subprocess.TimeoutExpired) as error:
		raise Stage14Error("Selected Java z-score entry point could not run") from error
	try:
		result = json.loads(completed.stdout)
	except (TypeError, ValueError, json.JSONDecodeError) as error:
		raise Stage14Error("Selected Java z-score entry point returned invalid JSON") from error
	if completed.returncode != 0 or result.get("failed") or result.get("status") != "complete":
		raise Stage14Error("Selected Java z-score entry point failed")
	return cast(dict[str, Any], result)


def _database_guard(connection: Any, names: TableNames, expected_revision: str | None) -> str:
	if _active_writers(connection):
		raise Stage14Error("Competing production writers are active")
	revision = _database_revision(connection, names)
	if expected_revision is not None and revision != expected_revision:
		raise Stage14Error("Raw or derived inputs changed during Stage 14")
	return revision


def _session_ids(connection: Any, names: TableNames, sessions: Iterable[str]) -> dict[str, int]:
	values = tuple(sessions)
	placeholders = ",".join("%s" for _ in values)
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id, transaction_date FROM {quote_identifier(names.sessions)} WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date, id", values)
		result: dict[str, int] = {}
		for row in cursor.fetchall():
			key = str(row[1])
			if key in result:
				raise Stage14Error(f"Duplicate date mapping: {key}")
			result[key] = int(row[0])
		if set(result) != set(values):
			raise Stage14Error("Z-score session mapping is incomplete")
		return result
	finally:
		cursor.close()


def _status_counts(connection: Any, table: str, date_id: int) -> dict[int, int]:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT status, COUNT(*) FROM {quote_identifier(table)} WHERE date_id=%s GROUP BY status ORDER BY status", (date_id,))
		return {int(row[0]): int(row[1]) for row in cursor.fetchall()}
	finally:
		cursor.close()


def _series_record(connection: Any, names: TableNames, item: dict[str, Any], date_id: int, ticker_count: int) -> dict[str, Any]:
	family, window = str(item.get("family")), int(cast(Any, item.get("window")))
	if family not in FAMILIES or window not in WINDOWS:
		raise Stage14Error("Java returned an unsupported z-score series")
	tables = names.resolve_zscore_tables((window,))[window]
	table = tables.price if family == "price" else tables.price_delta
	status_counts = _status_counts(connection, table, date_id)
	row_count = sum(status_counts.values())
	if row_count != ticker_count or int(item.get("row_count", -1)) != ticker_count:
		raise Stage14Error(f"Z-score ticker coverage differs: {item.get('session')}/{family}/{window}")
	if any(status not in range(6) for status in status_counts):
		raise Stage14Error("Z-score status code is outside the contract")
	cursor = connection.cursor()
	try:
		cursor.execute(
			f"SELECT series_key, row_count, ready_count, dependency_digest, availability_digest, actual_input_available, content_digest FROM {quote_identifier(names.zscore_sessions)} WHERE date_id=%s AND series_key IN (SELECT series_key FROM {quote_identifier(names.zscore_series)} WHERE family=%s AND `window`=%s)",
			(date_id, family, window),
		)
		metadata = cursor.fetchall()
	finally:
		cursor.close()
	if len(metadata) != 1:
		raise Stage14Error(f"Z-score completion metadata is incomplete: {item.get('session')}/{family}/{window}")
	meta = metadata[0]
	if int(meta[1]) != row_count or int(meta[2]) != status_counts.get(0, 0) or str(meta[6]) != str(item.get("content_digest")):
		raise Stage14Error(f"Z-score completion metadata differs: {item.get('session')}/{family}/{window}")
	return {
		"session": str(item["session"]), "family": family, "window": window,
		"row_count": row_count, "ready_count": status_counts.get(0, 0),
		"status_counts": {str(code): status_counts.get(code, 0) for code in range(6)},
		"content_digest": str(meta[6]), "dependency_digest": str(meta[3]),
		"availability_digest": str(meta[4]), "actual_input_available": bool(meta[5]),
		"series_key": str(meta[0]),
	}


def _coverage(connection: Any, names: TableNames, sessions: tuple[str, ...], ticker_count: int) -> dict[str, Any]:
	date_ids = _session_ids(connection, names, sessions)
	series: list[dict[str, Any]] = []
	for family in FAMILIES:
		for window in WINDOWS:
			tables = names.resolve_zscore_tables((window,))[window]
			table = tables.price if family == "price" else tables.price_delta
			placeholders = ",".join("%s" for _ in date_ids)
			cursor = connection.cursor()
			try:
				cursor.execute(
					f"SELECT COUNT(*), COUNT(DISTINCT date_id), COALESCE(SUM(status=0),0) FROM {quote_identifier(table)} WHERE date_id IN ({placeholders})",
					tuple(date_ids.values()),
				)
				row_count, date_count, ready_count = cursor.fetchone()
				cursor.execute(
					f"SELECT COUNT(*) FROM {quote_identifier(names.zscore_sessions)} WHERE date_id IN ({placeholders}) AND series_key IN (SELECT series_key FROM {quote_identifier(names.zscore_series)} WHERE family=%s AND `window`=%s)",
					tuple(date_ids.values()) + (family, window),
				)
				metadata_count = int(cursor.fetchone()[0])
			finally:
				cursor.close()
			expected_rows = len(sessions) * ticker_count
			if int(row_count) != expected_rows or int(date_count) != len(sessions) or metadata_count != len(sessions):
				raise Stage14Error(f"Z-score materialization coverage differs: {family}/{window}")
			series.append({"family": family, "window": window, "sessions": int(date_count), "rows": int(row_count), "ready_rows": int(ready_count), "metadata_rows": metadata_count})
	return {"sessions": len(sessions), "tickers": ticker_count, "series": series}


def _float32(value: float) -> float:
	return cast(float, struct.unpack("!f", struct.pack("!f", value))[0])


def _higher(first: int | None, second: int | None) -> int | None:
	if first is None:
		return second
	if second is None:
		return first
	return first if STATUS_PRIORITY.get(first, 0) >= STATUS_PRIORITY.get(second, 0) else second


def _reference_status_score(
	family: str,
	window: int,
	target_index: int,
	dates: tuple[str, ...],
	ticker_id: int,
	raw: dict[tuple[str, int], object],
	deltas: dict[tuple[str, int], object],
	verified: dict[str, bool],
) -> tuple[int, float | None]:
	first = max(0, target_index - window + 1)
	issue: int | None = None
	values: list[float] = []
	for index in range(first, target_index + 1):
		date_value = dates[index]
		current = raw.get((date_value, ticker_id))
		current_issue: int | None
		if not verified.get(date_value, False):
			current_issue = 3
		elif current is None:
			current_issue = 2
		else:
			current_number = math.nan
			try:
				current_number = float(cast(Any, current))
			except (TypeError, ValueError, OverflowError):
				current_number = math.nan
			current_issue = 0 if math.isfinite(current_number) and current_number > 0.0 else 4
		if family == "price":
			issue = _higher(issue, current_issue)
			if current_issue == 0:
				values.append(current_number)
			continue
		if index == 0:
			current_issue = _higher(current_issue, 1)
		else:
			prior_date = dates[index - 1]
			prior = raw.get((prior_date, ticker_id))
			prior_issue: int | None
			if not verified.get(prior_date, False):
				prior_issue = 3
			elif prior is None:
				prior_issue = 2
			else:
				prior_number = math.nan
				try:
					prior_number = float(cast(Any, prior))
				except (TypeError, ValueError, OverflowError):
					prior_number = math.nan
				prior_issue = 0 if math.isfinite(prior_number) and prior_number > 0.0 else 4
			current_issue = _higher(current_issue, prior_issue)
			stored = deltas.get((date_value, ticker_id))
			if current_issue == 0:
				stored_number = math.nan
				try:
					stored_number = float(cast(Any, stored))
				except (TypeError, ValueError, OverflowError):
					stored_number = math.nan
				if stored is None:
					current_issue = 2
				elif not math.isfinite(stored_number) or stored_number <= 0.0 or not math.isclose(stored_number, current_number / prior_number, rel_tol=1e-10, abs_tol=1e-12):
					current_issue = 4
				else:
					values.append(stored_number - 1.0)
			issue = _higher(issue, current_issue)
	if target_index + 1 < window:
		issue = _higher(issue, 1)
	if issue is not None and issue != 0:
		return issue, None
	if len(values) != window:
		return 2, None
	if all(value == values[0] for value in values):
		return 5, None
	mean = math.fsum(values) / window
	variance = math.fsum((value - mean) * (value - mean) for value in values) / window
	if not math.isfinite(variance) or variance <= 0.0:
		return 5, None
	return 0, _float32((values[-1] - mean) / math.sqrt(variance))


def _audit_series(connection: Any, names: TableNames, manifest: dict[str, Any], session: str, family: str, window: int) -> dict[str, Any]:
	target = next((item for item in manifest["targets"] if item.get("session") == session), None)
	if target is None:
		raise Stage14Error(f"Missing z-score target: {session}")
	dates = tuple(str(value) for value in target["zscore_dependencies"])
	date_ids = _session_ids(connection, names, dates)
	id_to_date = {identifier: date_value for date_value, identifier in date_ids.items()}
	file_by_date = {str(item["session"]): item for item in manifest["files"]}
	placeholders = ",".join("%s" for _ in dates)
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id FROM {quote_identifier(names.tickers)} ORDER BY id")
		ticker_ids = tuple(int(row[0]) for row in cursor.fetchall())
		cursor.execute(f"SELECT date_id, ticker_id, close FROM {quote_identifier(names.prices)} WHERE date_id IN ({placeholders})", tuple(date_ids.values()))
		raw = {(id_to_date[int(row[0])], int(row[1])): row[2] for row in cursor.fetchall()}
		if family == "price_delta":
			cursor.execute(f"SELECT date_id, ticker_id, close_ratio_to_prior FROM {quote_identifier(names.deltas)} WHERE date_id IN ({placeholders})", tuple(date_ids.values()))
			deltas = {(id_to_date[int(row[0])], int(row[1])): row[2] for row in cursor.fetchall()}
		else:
			deltas = {}
		cursor.execute(f"SELECT session_date, raw_id, available_at FROM {quote_identifier(names.imports)} WHERE session_date IN ({placeholders})", dates)
		imports = {str(row[0]): (str(row[1]), row[2]) for row in cursor.fetchall()}
	finally:
		cursor.close()
	verified = {}
	for date_value in dates:
		item = file_by_date.get(date_value)
		receipt = imports.get(date_value)
		verified[date_value] = bool(item and item.get("valid") is True and receipt and receipt[0] == item.get("records_id") and receipt[1] is not None and str(receipt[1]).strip())
	expected = {ticker_id: _reference_status_score(family, window, len(dates) - 1, dates, ticker_id, raw, deltas, verified) for ticker_id in ticker_ids}
	table = names.resolve_zscore_tables((window,))[window].price if family == "price" else names.resolve_zscore_tables((window,))[window].price_delta
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT ticker_id, zscore, status FROM {quote_identifier(table)} WHERE date_id=%s ORDER BY ticker_id", (date_ids[session],))
		stored = {int(row[0]): (None if row[1] is None else float(row[1]), int(row[2])) for row in cursor.fetchall()}
		cursor.execute(f"SELECT row_count, ready_count, status_counts, dependency_digest, availability_digest FROM {quote_identifier(names.zscore_sessions)} WHERE date_id=%s AND series_key IN (SELECT series_key FROM {quote_identifier(names.zscore_series)} WHERE family=%s AND `window`=%s)", (date_ids[session], family, window))
		metadata = cursor.fetchall()
	finally:
		cursor.close()
	if set(stored) != set(expected) or len(metadata) != 1:
		raise Stage14Error(f"Independent z-score audit row scope differs: {session}/{family}/{window}")
	for ticker_id, (status, score) in expected.items():
		actual_score, actual_status = stored[ticker_id]
		if actual_status != status:
			raise Stage14Error(f"Independent z-score audit status differs: {session}/{family}/{window}/{ticker_id}")
		if status == 0 and (actual_score is None or not math.isclose(actual_score, score or 0.0, rel_tol=1e-5, abs_tol=1e-6)):
			raise Stage14Error(f"Independent z-score audit value differs: {session}/{family}/{window}/{ticker_id}")
		if status != 0 and actual_score is not None:
			raise Stage14Error(f"Unavailable z-score has a value: {session}/{family}/{window}/{ticker_id}")
	counts = {code: sum(status == code for status, _ in expected.values()) for code in range(6)}
	meta = metadata[0]
	if int(meta[0]) != len(expected) or int(meta[1]) != counts[0]:
		raise Stage14Error(f"Independent z-score audit metadata counts differ: {session}/{family}/{window}")
	return {"session": session, "family": family, "window": window, "rows": len(expected), "ready_rows": counts[0], "status_counts": {str(code): counts[code] for code in range(6)}, "dependency_digest": str(meta[3]), "availability_digest": str(meta[4]), "status": "passed"}


def _audit_selection(sessions: tuple[str, ...], chunk_size: int, seed: int, limit: int) -> tuple[str, ...]:
	if limit < 1:
		raise Stage14Error("Stage 14 audit limit must be positive")
	selected: list[str] = []
	for start in range(0, len(sessions), chunk_size):
		selected.extend((sessions[start], sessions[min(start + chunk_size - 1, len(sessions) - 1)]))
	randomizer = random.Random(seed)
	selected.extend(randomizer.sample(list(sessions), min(limit, len(sessions))))
	return tuple(dict.fromkeys(selected))


def _mark_dependency_repair(state: dict[str, Any]) -> None:
	migration = state.get("dependency_migration")
	if not migration:
		return
	history = Path(migration["history"])
	old = _json(history / "zscores.json")
	previous = {str(session) for chunk in old["chunks"] for session in chunk["sessions"]}
	completed = {str(session) for chunk in state["chunks"] for session in chunk["sessions"]}
	if previous <= completed:
		migration["status"] = "repaired"
		migration.setdefault("repaired_at", datetime.now().astimezone().isoformat())


@_exclusive_output
def run_stage14(
	context: Stage14Context,
	dbinfo_file: Path,
	output: Path,
	*,
	chunk_size: int = MAX_CHUNK_SIZE,
	java_timeout: float = 7200.0,
	resume: bool = True,
	audit_limit: int = 3,
	audit_seed: int = 20261022,
	max_chunks: int | None = None,
	java_runner: Callable[..., dict[str, Any]] | None = None,
	pipeline_configuration: dict[str, Any] | None = None,
	on_chunk_complete: Callable[[dict[str, Any]], None] | None = None,
	batch_manager: Any = None,
	allow_dependency_migration: bool = False,
	prepare_only: bool = False,
) -> dict[str, Any]:
	if type(chunk_size) is not int or chunk_size < 1 or chunk_size > MAX_CHUNK_SIZE:
		raise Stage14Error("Stage 14 chunk size must be between one and twenty sessions")
	if java_timeout <= 0:
		raise Stage14Error("Stage 14 Java timeout must be positive")
	if max_chunks is not None and (type(max_chunks) is not int or max_chunks < 1):
		raise Stage14Error("Stage 14 maximum chunk count must be positive")
	manifest, scope = build_zscore_manifest(context.stage13_manifest)
	if pipeline_configuration is not None:
		_validate_pipeline_dependencies(manifest)
	output = output.resolve()
	stage_directory = output.parent / "stage14"
	stage_directory.mkdir(parents=True, exist_ok=True)
	manifest_path = stage_directory / "zscore-manifest.json"
	names = _manifest_names(manifest)
	production = load_database_configuration(dbinfo_file)
	connection = _connect(dbinfo_file, production.database)
	try:
		revision = _database_revision(connection, names)
		if _active_writers(connection):
			raise Stage14Error("Competing production writers are active")
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT id, label FROM {quote_identifier(names.tickers)} ORDER BY id")
			current_tickers = [(int(row[0]), str(row[1])) for row in cursor.fetchall()]
		finally:
			cursor.close()
	finally:
		connection.rollback()
		connection.close()
	base_tickers = {int(item["id"]): str(item["label"]) for item in manifest.get("mappings", {}).get("ticker_mappings", ()) if isinstance(item, dict) and "id" in item and "label" in item}
	current_ticker_map = dict(current_tickers)
	if any(current_ticker_map.get(identifier) != label for identifier, label in base_tickers.items()):
		raise Stage14Error("Current ticker mappings differ from Stage 13 identities")
	manifest["mappings"] = dict(manifest.get("mappings", {}))
	manifest["mappings"]["ticker_mappings"] = [{"id": identifier, "label": label} for identifier, label in current_tickers]
	manifest["manifest_id"] = content_id({key: value for key, value in manifest.items() if key != "manifest_id"})
	state = _json(output) if resume and output.exists() else None
	if state is not None and state.get("manifest_id") != manifest["manifest_id"]:
		if not allow_dependency_migration:
			raise Stage14Error("Dependency manifest changed; use the migration-aware Stage 14 runner")
		from stockguidance.performance.stage14_batches import migrate_dependencies
		state = migrate_dependencies(state, _json(manifest_path), manifest, output, revision)
	_write_json(manifest_path, manifest)
	report_directory = stage_directory / f"java-reports-{manifest['manifest_id'][:12]}" if pipeline_configuration is not None else stage_directory / "java-reports"
	report_directory.mkdir(parents=True, exist_ok=True)
	checkpoint_directory = stage_directory / f"java-checkpoints-{manifest['manifest_id'][:12]}"
	checkpoint_directory.mkdir(parents=True, exist_ok=True)
	ticker_count = len(current_tickers)
	sessions = tuple(str(item["session"]) for item in manifest["targets"])
	if not sessions or ticker_count < 1:
		raise Stage14Error("Stage 14 has no requested sessions or retained ticker IDs")
	if resume and output.exists():
		state = _json(output)
		if state.get("schema_version") != STAGE14_SCHEMA_VERSION or state.get("stage") != 14 or state.get("manifest_id") != manifest["manifest_id"]:
			raise Stage14Error("Existing Stage 14 report belongs to a different input set")
		if state.get("database_revision_before") != revision:
			raise Stage14Error("Raw or derived inputs changed since the Stage 14 report")
		if state.get("status") == "complete":
			connection = _connect(dbinfo_file, production.database)
			try:
				_coverage(connection, names, sessions, ticker_count)
			finally:
				connection.rollback()
				connection.close()
			return state
	if state is None:
		state = {
			"schema_version": STAGE14_SCHEMA_VERSION, "stage": 14, "status": "running", "production_ready": False,
			"manifest_id": manifest["manifest_id"], "manifest_sha256": _sha256(manifest_path),
			"prerequisites": {"decision_sha256": context.decision_sha256, "stage12_sha256": context.stage12_sha256, "stage13_sha256": context.stage13_sha256, "stage13_manifest_sha256": context.stage13_manifest_sha256, "snapshot_sha256": context.snapshot_sha256, "snapshot_cutoff": context.snapshot["cutoff"]},
			"configuration": {"backend": "java", "families": list(FAMILIES), "windows": list(WINDOWS), "dependency_sessions": MAX_WINDOW + 1, "chunk_size": chunk_size, "preparers": 1, "writers": 1, "java_heap": "-Xmx512m", "native_threads": 1},
			"scope": {"requested_sessions": len(sessions), "invalid_archive_sessions": scope["invalid_archive_sessions"], "ticker_count": ticker_count, "minimum_dependency_count": scope["minimum_dependency_count"], "maximum_dependency_count": scope["maximum_dependency_count"]},
			"database_revision_before": revision, "database_revision_after": revision, "chunks": [], "series": [],
		}
	else:
		state["configuration"]["chunk_size"] = chunk_size
	if pipeline_configuration is not None:
		state["configuration"].update(pipeline_configuration)
	if batch_manager is not None:
		batch_manager.initialize(manifest, state, checkpoint_directory, chunk_size)
		batch_manager.recover(state)
	if prepare_only:
		_write_json(output, state)
		return state
	completed_sessions = {str(session) for chunk in state.get("chunks", ()) for session in chunk.get("sessions", ())}
	_mark_dependency_repair(state)
	if max_chunks is not None and len(state.get("chunks", ())) >= max_chunks:
		state.update({"status": "partial", "production_ready": False, "coverage": {"requested_sessions": len(sessions), "completed_sessions": len(completed_sessions), "completed_series": len(state.get("series", ())), "expected_series": len(sessions) * len(FAMILIES) * len(WINDOWS)}, "handoff": {"next": "resume Stage 14", "selected_backend": "java", "bounded_operation": True, "remaining_sessions": len(sessions) - len(completed_sessions), "remaining_series": len(sessions) * len(FAMILIES) * len(WINDOWS) - len(state.get("series", ())), "resume": "Rerun without max_chunks to continue from the durable chunk report"}})
		_write_json(output, state)
		return state
	started = perf_counter()
	for chunk_index, start in enumerate(range(0, len(sessions), chunk_size)):
		chunk = sessions[start:start + chunk_size]
		if set(chunk) <= completed_sessions:
			continue
		connection = _connect(dbinfo_file, production.database)
		try:
			if _active_writers(connection) or _database_revision(connection, names) != state.get("database_revision_after"):
				raise Stage14Error("Raw or derived inputs changed before a z-score chunk")
		finally:
			connection.rollback()
			connection.close()
		chunk_started = perf_counter()
		if batch_manager is not None:
			batch_manager.begin(chunk_index, chunk)
			runner = cast(Any, java_runner)
			if hasattr(runner, "before_publication"):
				runner.before_publication = batch_manager.publishing
			else:
				batch_manager.publishing()
		java_result = (java_runner or _run_java)(dbinfo_file, manifest_path, chunk, checkpoint_directory, java_timeout)
		if pipeline_configuration is not None:
			state["configuration"].update(pipeline_configuration)
		if len(java_result.get("series", ())) != len(chunk) * len(FAMILIES) * len(WINDOWS):
			raise Stage14Error("Java z-score chunk did not complete every series")
		expected_series = {(session, family, window) for session in chunk for family in FAMILIES for window in WINDOWS}
		actual_series = {(str(item.get("session")), str(item.get("family")), int(item.get("window", -1))) for item in java_result["series"]}
		if actual_series != expected_series:
			raise Stage14Error("Java z-score chunk returned duplicate, missing, or unexpected series")
		connection = _connect(dbinfo_file, production.database)
		try:
			date_ids = _session_ids(connection, names, chunk)
			chunk_series = [_series_record(connection, names, item, date_ids[str(item["session"])], ticker_count) for item in java_result["series"]]
			chunk_revision = _database_revision(connection, names)
		finally:
			connection.rollback()
			connection.close()
		if chunk_revision != state.get("database_revision_after"):
			raise Stage14Error("Raw or derived inputs changed during z-score publication; chunk was not accepted")
		state["chunks"].append({"index": chunk_index, "sessions": list(chunk), "series_count": len(chunk_series), "rows": sum(item["row_count"] for item in chunk_series), "seconds": perf_counter() - chunk_started, "database_revision_after": chunk_revision, "java_report": str((report_directory / f"chunk-{chunk_index:04d}.json").resolve())})
		if java_result.get("pipeline"):
			state["chunks"][-1]["pipeline"] = {key: java_result.get(key) for key in ("pipeline", "preparers", "writers", "cached_sessions", "source_batches")}
			state["chunks"][-1]["pipeline"]["preserved_output_rows"] = sum(int(item.get("preserved_output_rows", 0)) for item in java_result["series"])
		state["series"].extend(chunk_series)
		state["database_revision_after"] = chunk_revision
		completed_sessions.update(chunk)
		_mark_dependency_repair(state)
		state.update({"status": "partial", "production_ready": False, "coverage": {"requested_sessions": len(sessions), "completed_sessions": len(completed_sessions), "completed_series": len(state["series"]), "expected_series": len(sessions) * len(FAMILIES) * len(WINDOWS)}, "handoff": {"next": "resume Stage 14", "selected_backend": "java", "bounded_operation": True, "remaining_sessions": len(sessions) - len(completed_sessions), "remaining_series": len(sessions) * len(FAMILIES) * len(WINDOWS) - len(state["series"]), "resume": "Run tools/resume_stage14.py with the configured interpreter; completed chunks are retained"}})
		_write_json(report_directory / f"chunk-{chunk_index:04d}.json", java_result)
		_write_json(output, state)
		if batch_manager is not None:
			batch_manager.complete(chunk_index, state)
		if on_chunk_complete is not None:
			on_chunk_complete(state)
		if max_chunks is not None and len(state["chunks"]) >= max_chunks:
			return state
	close = getattr(java_runner, "close", None)
	if close is not None:
		close()
	connection = _connect(dbinfo_file, production.database)
	try:
		final_revision = _database_revision(connection, names)
		if final_revision != state.get("database_revision_after"):
			raise Stage14Error("Raw or derived inputs changed after z-score population")
		coverage = _coverage(connection, names, sessions, ticker_count)
		selected_audits = _audit_selection(sessions, chunk_size, audit_seed, audit_limit)
		audits = [_audit_series(connection, names, manifest, session, family, window) for session in selected_audits for family in FAMILIES for window in WINDOWS]
	finally:
		connection.rollback()
		connection.close()
	if len(completed_sessions) != len(sessions) or len(state["series"]) != len(sessions) * len(FAMILIES) * len(WINDOWS):
		raise Stage14Error("Stage 14 did not complete the retained z-score scope")
	state.update({"status": "complete", "production_ready": True, "database_revision_after": final_revision, "coverage": coverage, "independent_audit": {"seed": audit_seed, "sessions": list(selected_audits), "series": audits, "status": "passed"}, "elapsed_seconds": perf_counter() - started, "handoff": {"next": "Stage 15", "selected_backend": "java", "families": list(FAMILIES), "windows": list(WINDOWS), "dependency_sessions": MAX_WINDOW, "local_snapshot_provenance": "adopted archive snapshot at the recorded cutoff; no original historical availability claim", "resume": "Rerun with the same report and manifest; raw/derived revision changes are rejected and completed reports are coverage-checked without new writes"}})
	_write_json(output, state)
	return state
