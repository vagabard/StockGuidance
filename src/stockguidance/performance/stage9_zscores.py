from __future__ import annotations

import json
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys
import threading
from time import perf_counter, process_time
from typing import Any
from uuid import uuid4

from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.identity import content_id
from stockguidance.performance.candidates import _file_hash, _manifest_table_names
from stockguidance.performance.mysql_candidates import MySQLPythonBaseline, load_manifest
from stockguidance.performance.zscore_digest import TextDigest
from stockguidance.performance.sql_zscore_candidate import (
	SUPPORTED_WINDOWS,
	SQLZScoreCandidate,
	SQLZScoreTarget,
	TRIAL_RECEIPT,
	_series_key,
)


BACKENDS = ("python", "java", "sql")
BLOCKS = ("fixed_25", "chronological_20")
FAMILIES = ("price", "price_delta")
PREPARERS = {"python": (1,), "java": (1,), "sql": (1,)}
REPORT_SCHEMA_VERSION = 1


def _checkpoint_path(directory: Path, session: str, family: str, window: int) -> Path:
	return directory / f"{session}-{family}-{window}.json"


def _target_sessions(manifest: dict[str, Any], block: str) -> tuple[str, ...]:
	values = tuple(str(value) for value in manifest["target_blocks"][block])
	for session in values:
		item = next((target for target in manifest["targets"] if target["session"] == session), None)
		if not isinstance(item, dict) or block not in item.get("roles", ()):
			raise ValueError(f"Manifest z-score target block is incomplete: {block}/{session}")
		dependencies = tuple(item.get("zscore_dependencies", ()))
		if not dependencies or dependencies[-1] != session:
			raise ValueError(f"Manifest z-score dependencies are not target-terminated: {session}")
	return values


def _all_dependencies(manifest: dict[str, Any], sessions: tuple[str, ...]) -> tuple[str, ...]:
	items = {str(item["session"]): item for item in manifest["targets"]}
	result: set[str] = set()
	for session in sessions:
		result.update(str(value) for value in items[session]["zscore_dependencies"])
	return tuple(sorted(result))


def _verify_archive_files(manifest: dict[str, Any], sessions: tuple[str, ...]) -> None:
	files = {str(item["session"]): item for item in manifest["files"]}
	for session in _all_dependencies(manifest, sessions):
		item = files.get(session)
		if item and item.get("valid") and _file_hash(Path(item["path"])) != item["file_sha256"]:
			raise ValueError(f"Stage 9c archive changed: {session}")


def _target_for(dbinfo_file: Path, schema: str, manifest: dict[str, Any]) -> SQLZScoreTarget:
	return SQLZScoreTarget(dbinfo_file, schema, manifest)


def _clear_target(target: SQLZScoreTarget, manifest: dict[str, Any]) -> None:
	names = _manifest_table_names(manifest)
	connection = target.connect()
	try:
		target.validate(connection)
		cursor = connection.cursor()
		try:
			tables = [
				names.zscore_sessions,
			"stage6_completion",
			]
			resolved_tables = _manifest_table_names(manifest).resolve_zscore_tables((20, 60))
			for resolved in resolved_tables.values():
				tables.extend((resolved.price, resolved.price_delta))
			for table in dict.fromkeys(tables):
				if table == "stage6_completion":
					cursor.execute(f"DELETE FROM {quote_identifier(table)} WHERE workload = %s", ("zscores",))
				else:
					cursor.execute(f"DELETE FROM {quote_identifier(table)}")
			connection.commit()
		finally:
			cursor.close()
	finally:
		connection.close()


def _seed_inputs(target: SQLZScoreTarget, manifest: dict[str, Any], sessions: tuple[str, ...], output: Path) -> dict[str, Any]:
	dependencies = _all_dependencies(manifest, sessions)
	files = {str(item["session"]): item for item in manifest["files"]}
	names = _manifest_table_names(manifest)
	connection = target.connect()
	try:
		target.validate(connection)
		cursor = connection.cursor()
		try:
			placeholders = ",".join("%s" for _ in dependencies)
			cursor.execute(f"SELECT transaction_date FROM {quote_identifier(names.sessions)} WHERE transaction_date IN ({placeholders})", dependencies)
			existing = {str(value[0]) for value in cursor.fetchall()}
			missing = [session for session in dependencies if session not in existing]
			if missing:
				cursor.executemany(f"INSERT INTO {quote_identifier(names.sessions)} (transaction_date) VALUES (%s)", [(session,) for session in missing])
				connection.commit()
		finally:
			cursor.close()
	finally:
		connection.close()
	baseline = MySQLPythonBaseline(manifest, target, output / "python-seed.json")
	started = perf_counter()
	for session in dependencies:
		item = files.get(session)
		if not isinstance(item, dict) or not item.get("valid"):
			continue
		baseline._raw_session(session, seed=True)

	items = {str(item["session"]): item for item in manifest["targets"]}
	pairs = set()
	for session in sessions:
		dependency_dates = tuple(str(value) for value in items[session]["zscore_dependencies"])
		pairs.update(zip(dependency_dates, dependency_dates[1:]))
	connection = target.connect()
	try:
		target.validate(connection)
		cursor = connection.cursor()
		try:
			date_ids: dict[str, int] = {}
			all_dates = sorted({value for pair in pairs for value in pair})
			placeholders = ",".join("%s" for _ in all_dates)
			cursor.execute(
				f"SELECT id, transaction_date FROM {quote_identifier(_manifest_table_names(manifest).sessions)} "
				f"WHERE transaction_date IN ({placeholders})",
				all_dates,
			)
			date_ids = {str(session): int(identifier) for identifier, session in cursor.fetchall()}
			if set(date_ids) != set(all_dates):
				raise ValueError("Z-score seed date mappings are incomplete")
			prices = quote_identifier(names.prices)
			deltas = quote_identifier(names.deltas)
			cursor.execute("CREATE TEMPORARY TABLE stage9c_seed_pairs (prior_id BIGINT NOT NULL, current_id BIGINT NOT NULL, PRIMARY KEY (prior_id, current_id))")
			cursor.executemany(
				"INSERT INTO stage9c_seed_pairs (prior_id, current_id) VALUES (%s, %s)",
				[(date_ids[prior], date_ids[current]) for prior, current in sorted(pairs)],
			)
			cursor.execute(
				f"""INSERT INTO {deltas} (ticker_id, date_id, close_ratio_to_prior)
SELECT current_row.ticker_id, current_row.date_id, 1e0 * current_row.`close` / prior_row.`close`
FROM {prices} current_row
JOIN {prices} prior_row ON prior_row.ticker_id = current_row.ticker_id
JOIN stage9c_seed_pairs ON stage9c_seed_pairs.current_id = current_row.date_id
  AND stage9c_seed_pairs.prior_id = prior_row.date_id
WHERE current_row.`close` IS NOT NULL AND prior_row.`close` IS NOT NULL AND prior_row.`close` <> 0
				ON DUPLICATE KEY UPDATE close_ratio_to_prior = VALUES(close_ratio_to_prior)""",
			)
			connection.commit()
		finally:
			cursor.close()
	finally:
		connection.close()
	fingerprint = _input_fingerprint(target, manifest)
	connection = target.connect()
	try:
		cursor = connection.cursor()
		cursor.execute("INSERT INTO stage6_disposable_metadata (`key`, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value = VALUES(value)", ("stage9c_seed", json.dumps({"manifest_id": manifest["manifest_id"], "fingerprint": fingerprint}, sort_keys=True)))
		cursor.close()
	finally:
		connection.close()
	return {"dependency_sessions": len(dependencies), "delta_pairs": len(pairs), "seconds": perf_counter() - started, "fingerprint": fingerprint}


def _input_fingerprint(target: SQLZScoreTarget, manifest: dict[str, Any]) -> str:
	names = _manifest_table_names(manifest)
	digest = sha256()
	connection = target.connect()
	try:
		target.validate(connection)
		connection.start_transaction(isolation_level="REPEATABLE READ", readonly=True)
		cursor = connection.cursor()
		try:
			for table in (names.tickers, names.sessions, names.prices, names.deltas, names.imports):
				digest.update(table.encode("utf-8"))
				cursor.execute(f"SELECT * FROM {quote_identifier(table)} ORDER BY 1, 2")
				while rows := cursor.fetchmany(1000):
					for row in rows:
						digest.update(bytes.fromhex(content_id([None if value is None else str(value) for value in row])))
		finally:
			cursor.close()
		connection.rollback()
	finally:
		connection.close()
	return digest.hexdigest()


def _has_seeded_inputs(target: SQLZScoreTarget, manifest: dict[str, Any]) -> bool:
	connection = target.connect()
	try:
		target.validate(connection)
		cursor = connection.cursor()
		try:
			cursor.execute("SELECT value FROM stage6_disposable_metadata WHERE `key` = %s", ("stage9c_seed",))
			row = cursor.fetchone()
			if row is None:
				raise ValueError("Disposable schema lacks a verified Stage 9c seed; use a fresh schema")
			identity = json.loads(row[0])
		finally:
			cursor.close()
	finally:
		connection.close()
	if identity.get("manifest_id") != manifest["manifest_id"] or identity.get("fingerprint") != _input_fingerprint(target, manifest):
		raise ValueError("Disposable Stage 9c seeded inputs changed")
	return True


class PythonZScoreCandidate:
	def __init__(self, manifest: dict[str, Any], target: SQLZScoreTarget, output: Path, target_session: str) -> None:
		self.target = target
		self.delegate = SQLZScoreCandidate(manifest, target, output, target_session=target_session)

	def write_zscores(self, family: str, window: int, *, checkpoint: Path | None = None, interruption: str | None = None, verify_source_identities: bool = True, verified_source_sessions: set[str] | None = None) -> dict[str, Any]:
		if family not in FAMILIES or window not in SUPPORTED_WINDOWS:
			raise ValueError("Unsupported Python z-score series")
		started = perf_counter()
		if verify_source_identities:
			self.delegate._verify_source_identities(verified_source_sessions)
		phases = {"archive_validation": perf_counter() - started, "input_loading": 0.0, "hashing": 0.0}
		phase_started = perf_counter()
		connection = self.target.connect()
		cursor = connection.cursor(buffered=True)
		committed = False
		try:
			self.target.validate(connection)
			connection.autocommit = False
			date_ids = self.delegate._date_ids(cursor)
			ticker_ids = self.delegate._ticker_ids(cursor)
			phases["input_loading"] += perf_counter() - phase_started
			expected = {}
			digests = (TextDigest(), TextDigest())
			actual_input_available = True
			for offset in range(0, len(ticker_ids), 1000):
				batch = ticker_ids[offset:offset + 1000]
				phase_started = perf_counter()
				snapshot = self.delegate._load_input_snapshot(cursor, family, date_ids, batch)
				phases["input_loading"] += perf_counter() - phase_started
				phase_started = perf_counter()
				expected.update(self.delegate._oracle(cursor, family, window, snapshot=snapshot, date_ids=date_ids, ticker_ids=batch, vectorized=True, phases=phases))
				phase_started = perf_counter()
				dependency_digest, availability_digest, available = self.delegate._input_digests(cursor, family, date_ids, batch, snapshot=snapshot, digests=digests)
				actual_input_available = actual_input_available and available
				phases["hashing"] += perf_counter() - phase_started
			phase_started = perf_counter()
			content_digest = content_id([[self.delegate.target_session, ticker_id, score, status] for ticker_id, (score, status) in sorted(expected.items())])
			self.delegate._validate_checkpoint(checkpoint, family, window, content_digest)
			phases["hashing"] += perf_counter() - phase_started
			phase_started = perf_counter()
			series_key = self.delegate._ensure_series(cursor, family, window)
			resolved = self.delegate.names.resolve_zscore_tables((window,))[window]
			table = resolved.price if family == "price" else resolved.price_delta
			physical = quote_identifier(table)
			cursor.execute(f"DELETE FROM {physical} WHERE date_id = %s", (date_ids[self.delegate.target_session],))
			rows = [(date_ids[self.delegate.target_session], ticker_id, score, status) for ticker_id, (score, status) in sorted(expected.items())]
			cursor.executemany(f"INSERT INTO {physical} (date_id, ticker_id, zscore, status) VALUES (%s, %s, %s, %s)", rows)
			status_counts = ",".join(f"{status}:{sum(value[1] == status for value in expected.values())}" for status in range(6))
			metadata = quote_identifier(self.delegate.names.zscore_sessions)
			cursor.execute(f"DELETE FROM {metadata} WHERE series_key = %s AND date_id = %s", (series_key, date_ids[self.delegate.target_session]))
			cursor.execute(
				f"""INSERT INTO {metadata}
(series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
				(series_key, date_ids[self.delegate.target_session], dependency_digest, availability_digest, int(actual_input_available), TRIAL_RECEIPT, len(expected), sum(status == 0 for _, status in expected.values()), status_counts, content_digest),
			)
			completion = quote_identifier("stage6_completion")
			cursor.execute(
				f"""INSERT INTO {completion} (workload, session_date, series_key, content_digest, committed_at)
VALUES (%s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE content_digest = VALUES(content_digest), committed_at = VALUES(committed_at)""",
				("zscores", self.delegate.target_session, series_key, content_digest, TRIAL_RECEIPT),
			)
			if interruption == "before_commit":
				raise RuntimeError("Injected interruption before commit")
			connection.commit()
			committed = True
			phases["persistence"] = perf_counter() - phase_started
			if interruption == "after_commit":
				raise RuntimeError("Injected interruption after commit")
			phase_started = perf_counter()
			self.delegate._write_checkpoint(checkpoint, family, window, content_digest)
			phases["checkpoint"] = perf_counter() - phase_started
			return {
				"family": family,
				"window": window,
				"session": self.delegate.target_session,
				"row_count": len(expected),
				"ready_count": sum(status == 0 for _, status in expected.values()),
				"content_digest": content_digest,
				"dependency_digest": dependency_digest,
				"availability_digest": availability_digest,
				"actual_input_available": actual_input_available,
				"seconds": perf_counter() - started,
				"committed": True,
				"phase_seconds": phases,
			}
		except Exception:
			if not committed:
				connection.rollback()
			raise
		finally:
			cursor.close()
			connection.close()


def _series_jobs(sessions: tuple[str, ...]) -> tuple[tuple[str, str, int], ...]:
	return tuple((session, family, window) for session in sessions for family in FAMILIES for window in SUPPORTED_WINDOWS)


def _worker_arguments(argv: list[str]) -> dict[str, str]:
	result = {}
	index = 0
	while index < len(argv):
		key = argv[index]
		if not key.startswith("--") or index + 1 >= len(argv):
			raise ValueError("Worker arguments must be named pairs")
		result[key[2:].replace("-", "_")] = argv[index + 1]
		index += 2
	return result


def _write_progress(directory: Path, backend: str, series: list[dict[str, Any]]) -> None:
	temporary = directory / "progress.tmp"
	temporary.write_text(json.dumps(series, sort_keys=True) + "\n", encoding="utf-8")
	temporary.replace(directory / "progress.json")


def _python_worker(arguments: dict[str, str]) -> dict[str, Any]:
	manifest = load_manifest(Path(arguments["manifest"]))
	target = _target_for(Path(arguments["dbinfo_file"]), arguments["schema"], manifest)
	checkpoint_directory = Path(arguments["checkpoint_directory"])
	checkpoint_directory.mkdir(parents=True, exist_ok=True)
	sessions = tuple(value for value in arguments["sessions"].split(",") if value)
	started = perf_counter()
	series = []
	verified_source_sessions: set[str] = set()
	candidate = None
	for session, family, window in _series_jobs(sessions):
		if candidate is None or candidate.delegate.target_session != session:
			candidate = PythonZScoreCandidate(manifest, target, checkpoint_directory / "python-reference.json", session)
		result = candidate.write_zscores(
			family,
			window,
			checkpoint=_checkpoint_path(checkpoint_directory, session, family, window),
			verify_source_identities=True,
			verified_source_sessions=verified_source_sessions,
		)
		series.append(result)
		_write_progress(checkpoint_directory, "python", series)
	return {
		"schema_version": REPORT_SCHEMA_VERSION,
		"backend": "python",
		"preparers": 1,
		"status": "complete",
		"series": series,
		"rows": sum(int(item["row_count"]) for item in series),
		"workload_seconds": perf_counter() - started,
		"client_cpu_seconds": process_time(),
		"verified_archive_count": sum(bool(candidate.delegate.files[session].get("valid")) for session in verified_source_sessions),
		"query_count": "unavailable: direct MySQL candidate statements are not instrumented",
		"transaction_unit": "one z-score series/session",
	}


def _sql_worker(arguments: dict[str, str]) -> dict[str, Any]:
	manifest = load_manifest(Path(arguments["manifest"]))
	target = _target_for(Path(arguments["dbinfo_file"]), arguments["schema"], manifest)
	checkpoint_directory = Path(arguments["checkpoint_directory"])
	checkpoint_directory.mkdir(parents=True, exist_ok=True)
	sessions = tuple(value for value in arguments["sessions"].split(",") if value)
	started = perf_counter()
	series = []
	verified_source_sessions: set[str] = set()
	candidate = None
	for session, family, window in _series_jobs(sessions):
		if candidate is None or candidate.target_session != session:
			candidate = SQLZScoreCandidate(manifest, target, checkpoint_directory / "sql-reference.json", target_session=session)
		result = candidate.write_zscores(
			family,
			window,
			checkpoint=_checkpoint_path(checkpoint_directory, session, family, window),
			validate_reference=False,
			verify_source_identities=True,
			verified_source_sessions=verified_source_sessions,
		)
		series.append(result)
		_write_progress(checkpoint_directory, "sql", series)
	return {
		"schema_version": REPORT_SCHEMA_VERSION,
		"backend": "sql",
		"preparers": 1,
		"status": "complete",
		"series": series,
		"rows": sum(int(item["row_count"]) for item in series),
		"workload_seconds": perf_counter() - started,
		"series_seconds": sum(float(item["seconds"]) for item in series),
		"sql_call_and_readback_seconds": sum(float(item["phase_seconds"]["calculation"]) for item in series),
		"verified_archive_count": sum(bool(candidate.files[session].get("valid")) for session in verified_source_sessions),
		"query_count": "unavailable: MySQL server statement counter is not exposed",
		"transaction_unit": "one z-score series/session; one writer connection per series",
	}


def _java_worker_command(arguments: dict[str, str]) -> list[str]:
	repository = Path(__file__).resolve().parents[3]
	java_root = repository / "java"
	classpath = ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))
	return [
		"java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "-XX:ActiveProcessorCount=3", "-XX:+UseSerialGC", "-cp", classpath,
		"stockguidance.migration.JavaZScoreBenchmarkMain", "--manifest", arguments["manifest"],
		"--dbinfo-file", arguments["dbinfo_file"], "--disposable-schema", arguments["schema"],
		"--sessions", arguments["sessions"], "--checkpoint-directory", arguments["checkpoint_directory"],
	]


def _native_thread_environment() -> dict[str, str]:
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
	for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS"):
		environment[name] = "1"
	return environment


class _ProcessTreeMonitor:
	def __init__(self, pid: int) -> None:
		self.pid = pid
		self.peak_rss: int | None = None
		self.peak_threads: int | None = None
		self._stop = threading.Event()
		self._thread = threading.Thread(target=self._run, daemon=True)
		self._cpu_start: dict[int, float] = {}
		self._cpu_last: dict[int, float] = {}

	def start(self) -> None:
		self._thread.start()

	def stop(self) -> dict[str, Any]:
		self._stop.set()
		self._thread.join(timeout=2)
		return {
			"peak_tree_rss_bytes": self.peak_rss,
			"peak_tree_os_threads": self.peak_threads,
			"client_cpu_seconds": sum(self._cpu_last[pid] - self._cpu_start[pid] for pid in self._cpu_last),
			"sampling": "psutil process tree at 250ms intervals",
		}

	def _run(self) -> None:
		try:
			import psutil
		except ImportError:
			return
		while not self._stop.is_set():
			try:
				root = psutil.Process(self.pid)
				processes = [root, *root.children(recursive=True)]
			except psutil.Error:
				processes = []
			rss = 0
			threads = 0
			for process in processes:
				try:
					cpu = process.cpu_times()
					value = float(cpu.user + cpu.system)
					self._cpu_start.setdefault(process.pid, value)
					self._cpu_last[process.pid] = value
					rss += int(process.memory_info().rss)
					threads += int(process.num_threads())
				except psutil.Error:
					continue
			if rss:
				self.peak_rss = max(self.peak_rss or 0, rss)
			if threads:
				self.peak_threads = max(self.peak_threads or 0, threads)
			self._stop.wait(0.25)


def _run_child(arguments: dict[str, str], timeout_seconds: float, output_directory: Path) -> dict[str, Any]:
	backend = arguments["backend"]
	if backend == "java":
		command = _java_worker_command(arguments)
	else:
		command = [sys.executable, "-m", "stockguidance.performance.stage9_zscores", "--worker", backend]
		for name in ("manifest", "dbinfo_file", "schema", "sessions", "checkpoint_directory"):
			command.extend((f"--{name.replace('_', '-')}", arguments[name]))
	started = perf_counter()
	process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_native_thread_environment())
	monitor = _ProcessTreeMonitor(process.pid)
	monitor.start()
	timed_out = False
	try:
		stdout, stderr = process.communicate(timeout=timeout_seconds)
	except subprocess.TimeoutExpired:
		timed_out = True
		process.kill()
		stdout, stderr = process.communicate()
	resources = monitor.stop()
	wall_seconds = perf_counter() - started
	log_path = output_directory / "logs" / f"{backend}-{arguments['block']}-{arguments['repetition']}.stderr.log"
	log_path.parent.mkdir(parents=True, exist_ok=True)
	log_path.write_text(stderr[-12000:], encoding="utf-8")
	result: dict[str, Any] = {
		"returncode": process.returncode,
		"timed_out": timed_out,
		"end_to_end_seconds": wall_seconds,
		"startup_and_handoff_seconds": None,
		"stderr_log": str(log_path),
		**resources,
	}
	if not timed_out and process.returncode == 0:
		try:
			payload = json.loads(stdout)
		except json.JSONDecodeError:
			result.update(status="failed", failure_type="invalid_worker_report")
		else:
			result["status"] = payload.get("status", "failed")
			result["worker"] = payload
			result["startup_and_handoff_seconds"] = max(0.0, wall_seconds - float(payload.get("workload_seconds", 0.0)))
	else:
		result.update(status="timed_out" if timed_out else "failed", failure_type="timeout" if timed_out else "worker_exit")
		if not timed_out:
			try:
				payload = json.loads(stdout)
				result["worker_failure_type"] = payload.get("failure_type")
			except (ValueError, AttributeError):
				pass
	progress = Path(arguments["checkpoint_directory"]) / "progress.json"
	if progress.exists():
		result["committed_series"] = json.loads(progress.read_text(encoding="utf-8"))
	return result


def _audit_series(target: SQLZScoreTarget, manifest: dict[str, Any], session: str, family: str, window: int, *, cache_directory: Path | None = None, input_fingerprint: str | None = None) -> dict[str, Any]:
	candidate = SQLZScoreCandidate(manifest, target, Path("stage9c-audit.json"), target_session=session)
	connection = target.connect()
	try:
		target.validate(connection)
		cursor = connection.cursor(buffered=True)
		try:
			date_ids = candidate._date_ids(cursor)
			connection.autocommit = False
			identifiers = candidate._ticker_ids(cursor)
			cache_key = content_id([manifest["manifest_id"], input_fingerprint, session, family, window, _file_hashes(Path(__file__).resolve().parents[3])])
			cache_path = None if cache_directory is None or input_fingerprint is None else cache_directory / f"{cache_key}.json"
			if cache_path is not None and cache_path.exists():
				cached = json.loads(cache_path.read_text(encoding="utf-8"))
				if cached["key"] != cache_key or content_id(cached["rows"]) != cached["digest"]:
					raise ValueError("Independent scalar audit cache changed")
				expected = {int(identifier): (score, status) for identifier, score, status in cached["rows"]}
			else:
				expected = {}
				for offset in range(0, len(identifiers), 1000):
					expected.update(candidate._oracle(cursor, family, window, date_ids=date_ids, ticker_ids=identifiers[offset:offset + 1000]))
				if cache_path is not None:
					cache_directory.mkdir(parents=True, exist_ok=True)
					cached_rows = [[identifier, score, status] for identifier, (score, status) in sorted(expected.items())]
					cache_path.write_text(json.dumps({"key": cache_key, "rows": cached_rows, "digest": content_id(cached_rows)}, sort_keys=True) + "\n", encoding="utf-8")
			resolved = candidate.names.resolve_zscore_tables((window,))[window]
			table = resolved.price if family == "price" else resolved.price_delta
			cursor.execute(f"SELECT ticker_id, 0e0 + zscore, status FROM {quote_identifier(table)} WHERE date_id = %s ORDER BY ticker_id", (date_ids[session],))
			actual = {int(identifier): (None if score is None else float(score), int(status)) for identifier, score, status in cursor.fetchall()}
			if actual.keys() != expected.keys():
				raise ValueError(f"Z-score ticker scope differs: {session}/{family}/{window}")
			for ticker_id, (actual_score, actual_status) in actual.items():
				expected_score, expected_status = expected[ticker_id]
				if actual_status != expected_status:
					raise ValueError(f"Z-score status differs: {session}/{family}/{window}/{ticker_id}")
				if expected_score is None:
					if actual_score is not None:
						raise ValueError(f"Unavailable z-score has a value: {session}/{family}/{window}/{ticker_id}")
				elif actual_score is None or abs(actual_score - expected_score) > max(1e-6, 1e-5 * abs(expected_score)):
					raise ValueError(f"Z-score value differs: {session}/{family}/{window}/{ticker_id}")
			series_key = _series_key(manifest, family, window)
			cursor.execute(f"SELECT row_count, ready_count, content_digest FROM {quote_identifier(candidate.names.zscore_sessions)} WHERE series_key = %s AND date_id = %s", (series_key, date_ids[session]))
			metadata = cursor.fetchall()
			content_digest = content_id([[session, ticker_id, score, status] for ticker_id, (score, status) in sorted(actual.items())])
			if metadata != [(len(expected), sum(status == 0 for _, status in expected.values()), content_digest)]:
				raise ValueError(f"Z-score metadata differs: {session}/{family}/{window}")
			return {"status": "passed", "session": session, "family": family, "window": window, "row_count": len(actual), "ready_count": sum(status == 0 for _, status in actual.values()), "status_counts": {str(status): sum(value[1] == status for value in actual.values()) for status in range(6)}}
		finally:
			cursor.close()
	finally:
		connection.close()


def _file_hashes(repository: Path) -> dict[str, str]:
	paths = [Path(__file__), Path(__file__).with_name("sql_zscore_candidate.py"), Path(__file__).with_name("mysql_candidates.py"), repository / "java/src/main/java/stockguidance/migration/JavaZScoreCandidate.java", repository / "java/src/main/java/stockguidance/migration/JavaZScoreBenchmarkMain.java"]
	paths.extend(repository / relative for relative in (
		"src/stockguidance/zscores/vectorized.py", "src/stockguidance/zscores/calculation.py",
		"src/stockguidance/performance/zscore_digest.py", "src/stockguidance/performance/sql_raw_candidate.py",
		"src/stockguidance/market_data/identity.py", "java/pom.xml",
		"java/src/main/java/stockguidance/migration/ZScoreVectorKernel.java",
		"java/src/main/java/stockguidance/migration/JavaWriterManifest.java",
		"src/stockguidance/zscores/inputs.py", "src/stockguidance/zscores/results.py",
		"src/stockguidance/zscores/specification.py", "src/stockguidance/calendar/exchange.py",
		"src/stockguidance/legacy_migration/precision.py", "src/stockguidance/performance/candidates.py",
		"src/stockguidance/performance/stage9_zscore_diagnostic.py",
		"java/src/main/java/stockguidance/migration/JavaWriterCandidate.java",
		"java/src/main/java/stockguidance/migration/ArchiveCandidateReader.java",
	))
	paths.extend(sorted((repository / "java/target/classes/stockguidance/migration").glob("*Score*.class")))
	return {path.relative_to(repository).as_posix(): _file_hash(path) for path in paths}


def _median(values: list[float]) -> float | None:
	if not values:
		return None
	ordered = sorted(values)
	return ordered[len(ordered) // 2]


def run_stage9_zscores(manifest_path: Path, dbinfo_file: Path, output: Path, *, timeout_seconds: float = 3600.0, disposable_schema: str | None = None) -> dict[str, Any]:
	connections = []
	try:
		return _run_stage9_zscores(manifest_path, dbinfo_file, output, timeout_seconds=timeout_seconds, disposable_schema=disposable_schema, keepalive_connections=connections)
	finally:
		for connection in connections:
			connection.close()


def _run_stage9_zscores(manifest_path: Path, dbinfo_file: Path, output: Path, *, timeout_seconds: float, disposable_schema: str | None, keepalive_connections: list) -> dict[str, Any]:
	manifest = load_manifest(manifest_path)
	repository = Path(__file__).resolve().parents[3]
	output = output.resolve()
	if output.exists() and any(output.iterdir()):
		raise ValueError("Stage 9c requires a fresh output directory; preserve earlier evidence")
	zscore_directory = output / "zscores"
	zscore_directory.mkdir(parents=True, exist_ok=True)
	report: dict[str, Any] = {
		"schema_version": REPORT_SCHEMA_VERSION,
		"benchmark": "stockguidance-legacy-migration-stage9c-zscores-mysql-v1",
		"stage": "9c",
		"status": "incomplete",
		"manifest": {"path": str(manifest_path.resolve()), "id": manifest["manifest_id"], "sha256": _file_hash(manifest_path)},
		"workload": "zscores_persistence",
		"blocks": list(BLOCKS),
		"series": {"families": list(FAMILIES), "windows": list(SUPPORTED_WINDOWS), "transaction_unit": "one z-score series/session", "ticker_scope": "all retained manifest ticker IDs", "status_policy": "dense rows with explicit NULL/status outcomes"},
		"configurations": {},
		"protocol": {"first_plus_warm_repetitions": 4, "candidate_orders": manifest["repetitions"]["candidate_orders"], "writers": 1, "queued_sessions_max": 1, "native_threads": 1, "java_heap": "-Xmx512m", "preparers": "inapplicable: inputs are database-resident and each series/session is one atomic writer transaction", "cache_state": "unknown", "timeout_seconds": timeout_seconds, "timing_scope": "candidate process startup through durable series/session commit and checkpoint; target setup/reset, input seeding and correctness audit excluded"},
		"server_metrics": {"competing_trials": "none; candidates run sequentially", "client_monitoring": "psutil process-tree sampling when available", "server_resource_metrics": "unavailable unless exposed by configured account"},
		"production_access": {"ddl": False, "dml": False, "read_only_extraction": False},
		"source_code_hashes": _file_hashes(repository),
	}

	def save() -> None:
		(output / "stage9c-zscores.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	targets: dict[str, SQLZScoreTarget] = {}
	shared_schema = disposable_schema or f"stockguidance_disposable_s9c_shared_{uuid4().hex[:8]}"
	shared_target = _target_for(dbinfo_file.resolve(), shared_schema, manifest)
	shared_target_setup = shared_target.prepare()
	keepalive_connection = shared_target.connect()
	keepalive_connections.append(keepalive_connection)
	for backend in BACKENDS:
		configuration_key = f"{backend}-p1"
		target = shared_target
		setup_started = perf_counter()
		configuration: dict[str, Any] = {"backend": backend, "preparers": 1, "status": "incomplete", "schema": shared_schema, "blocks": {}, "target_setup": shared_target_setup, "setup_seconds": perf_counter() - setup_started}
		report["configurations"][configuration_key] = configuration
		targets[configuration_key] = target
		for block in BLOCKS:
			sessions = _target_sessions(manifest, block)
			configuration["blocks"][block] = {"status": "incomplete", "sessions": list(sessions), "repetitions": [], "correctness": []}
		save()
	seed_sessions = tuple(sorted(set(session for block in BLOCKS for session in _target_sessions(manifest, block))))
	seed_info = {"reused": True, "seconds": 0.0} if disposable_schema and _has_seeded_inputs(shared_target, manifest) else _seed_inputs(shared_target, manifest, seed_sessions, zscore_directory / "setup")
	input_fingerprint = _input_fingerprint(shared_target, manifest)
	report["input_fingerprint"] = input_fingerprint
	report["protocol"]["archive_validation"] = "each required valid archive verified once per timed worker; cache reset per repetition"
	save()

	for repetition, order in enumerate(manifest["repetitions"]["candidate_orders"]):
		for backend in order:
			configuration_key = f"{backend}-p1"
			configuration = report["configurations"][configuration_key]
			target = targets[configuration_key]
			for block in BLOCKS:
				block_report = configuration["blocks"][block]
				sessions = tuple(block_report["sessions"])
				reset_started = perf_counter()
				_clear_target(target, manifest)
				reset_seconds = perf_counter() - reset_started
				if _input_fingerprint(target, manifest) != input_fingerprint:
					raise ValueError("Stage 9c inputs changed before trial")
				if _file_hashes(repository) != report["source_code_hashes"]:
					raise ValueError("Stage 9c candidate code changed before trial")
				_verify_archive_files(manifest, sessions)
				arguments = {"backend": backend, "manifest": str(manifest_path.resolve()), "dbinfo_file": str(dbinfo_file.resolve()), "schema": configuration["schema"], "sessions": ",".join(sessions), "checkpoint_directory": str(zscore_directory / "checkpoints" / f"{configuration_key}-{block}-{repetition}"), "block": block, "repetition": str(repetition)}
				trial = _run_child(arguments, timeout_seconds, zscore_directory)
				trial.update(repetition=repetition, candidate_order=list(order), block=block, reset_seconds=reset_seconds, seed=seed_info)
				if trial.get("status") == "complete":
					try:
						if _input_fingerprint(target, manifest) != input_fingerprint:
							raise ValueError("Stage 9c inputs changed during trial")
						if _file_hashes(repository) != report["source_code_hashes"]:
							raise ValueError("Stage 9c candidate code changed during trial")
						_verify_archive_files(manifest, sessions)
						trial["correctness"] = {"status": "passed", "series": [_audit_series(target, manifest, session, family, window, cache_directory=zscore_directory / "scalar-audit-cache", input_fingerprint=input_fingerprint) for session, family, window in _series_jobs(sessions)]}
					except Exception as error:
						trial.update(status="failed_correctness", failure_type=type(error).__name__, correctness_error=str(error))
				if trial.get("correctness", {}).get("status") == "passed":
					block_report["correctness"].append(trial["correctness"])
				else:
					block_report["correctness"].append({"status": "not_available"})
				block_report["repetitions"].append(trial)
				configuration["last_trial"] = {"block": block, "repetition": repetition, "status": trial["status"]}
				(zscore_directory / f"{configuration_key}.json").write_text(json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8")
				save()

	for configuration in report["configurations"].values():
		for block_report in configuration["blocks"].values():
			successful = [item for item in block_report["repetitions"] if item.get("status") == "complete" and item.get("correctness", {}).get("status") == "passed"]
			block_report["status"] = "measured" if len(successful) == 4 else "incomplete"
			block_report["warm_median_seconds"] = _median([float(item["end_to_end_seconds"]) for item in successful[1:]]) if len(successful) == 4 else None
			block_report["first_seconds"] = float(successful[0]["end_to_end_seconds"]) if successful else None
			block_report["rows_per_trial"] = int(successful[0]["worker"]["rows"]) if successful else None
		configuration["status"] = "measured" if all(value["status"] == "measured" for value in configuration["blocks"].values()) else "incomplete"
		configuration["warm_median_seconds"] = sum(float(value["warm_median_seconds"]) for value in configuration["blocks"].values() if value["warm_median_seconds"] is not None) if configuration["status"] == "measured" else None
		configuration["throughput_rows_per_second"] = sum(int(value["rows_per_trial"]) for value in configuration["blocks"].values() if value["rows_per_trial"] is not None) / configuration["warm_median_seconds"] if configuration["warm_median_seconds"] else None
		(zscore_directory / f"{configuration['backend']}-p1.json").write_text(json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8")
		save()
	report["comparison_table"] = [{"workload": "zscores_persistence", "backend": backend, "status": "measured" if report["configurations"][f"{backend}-p1"]["status"] == "measured" else "incomplete", "best_configuration": f"{backend}-p1" if report["configurations"][f"{backend}-p1"]["status"] == "measured" else None, "warm_median_seconds": report["configurations"][f"{backend}-p1"].get("warm_median_seconds"), "throughput_rows_per_second": report["configurations"][f"{backend}-p1"].get("throughput_rows_per_second")} for backend in BACKENDS]
	report["status"] = "complete" if all(value["status"] == "measured" for value in report["configurations"].values()) else "incomplete"
	save()
	return report


def _best(report: dict[str, Any], backend: str) -> dict[str, Any]:
	values = [(key, value) for key, value in report["configurations"].items() if value.get("backend") == backend and value.get("status") == "measured" and value.get("warm_median_seconds") is not None]
	if not values:
		return {"status": "incomplete", "best_configuration": None, "warm_median_seconds": None, "throughput_rows_per_second": None}
	key, value = min(values, key=lambda item: float(item[1]["warm_median_seconds"]))
	return {"status": "measured", "best_configuration": key, "warm_median_seconds": value["warm_median_seconds"], "throughput_rows_per_second": value.get("throughput_rows_per_second")}


def aggregate_stage9_study(raw_report: dict[str, Any], derived_report: dict[str, Any], zscore_report: dict[str, Any], output: Path) -> dict[str, Any]:
	rows = []
	for workload, report in (("raw_adoption_repair", raw_report), ("derived_computation_persistence", derived_report), ("zscores_persistence", zscore_report)):
		for backend in BACKENDS:
			best = _best(report, backend)
			rows.append({"workload": workload, "backend": backend, **best})
	study = {
		"schema_version": 1,
		"benchmark": "stockguidance-legacy-migration-stage9-candidate-study-v1",
		"stage": 9,
		"status": "complete" if raw_report.get("status") == "complete" and derived_report.get("status") == "complete" and zscore_report.get("status") == "complete" else "incomplete",
		"substage": {"9a": raw_report.get("status"), "9b": derived_report.get("status"), "9c": zscore_report.get("status")},
		"manifest": {"id": zscore_report["manifest"]["id"], "raw_report": raw_report.get("manifest", {}).get("path"), "derived_report": derived_report.get("manifest", {}).get("path"), "zscore_report": zscore_report.get("manifest", {}).get("path")},
		"comparison_table": rows,
		"raw": raw_report,
		"derived": derived_report,
		"zscores": zscore_report,
		"selection": {"production_backend_selected": False, "selection_stage": 10, "rule": "Java and SQL qualify only when a valid startup-inclusive warm median is at least 10% faster than Python"},
		"production_access": {"ddl": False, "dml": False, "read_only_extraction": False},
	}
	output.mkdir(parents=True, exist_ok=True)
	(output / "study.json").write_text(json.dumps(study, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return study


def main(argv: list[str] | None = None) -> int:
	arguments = list(sys.argv[1:] if argv is None else argv)
	if arguments and arguments[0] == "--worker":
		worker_name = arguments[1]
		worker_arguments = _worker_arguments(arguments[2:])
		result = _python_worker(worker_arguments) if worker_name == "python" else _sql_worker(worker_arguments)
		print(json.dumps(result, sort_keys=True))
		return 0
	raise SystemExit("Stage 9c z-score module requires --worker")


if __name__ == "__main__":
	raise SystemExit(main())
