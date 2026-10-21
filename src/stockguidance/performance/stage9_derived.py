from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from time import perf_counter, process_time
from typing import Any, Iterable
from uuid import uuid4

from stockguidance.database.table_names import quote_identifier
from stockguidance.legacy_migration.precision import ExactArchiveBar, exact_records_id, read_exact_archive
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow, derived_id
from stockguidance.performance.candidates import _exact_derived_rows, _file_hash
from stockguidance.performance.mysql_candidates import MySQLPythonBaseline, MySQLTarget, load_manifest
from stockguidance.performance.sql_derived_candidate import SQLDerivedCandidate, SQLDerivedTarget
from stockguidance.validation.derived import _equal


BACKENDS = ("python", "java", "sql")
BLOCKS = ("fixed_25", "chronological_20")
PREPARERS = {"python": (1, 3), "java": (1, 3), "sql": (1,)}
REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PreparedDerived:
	session: str
	prior_session: str
	following_session: str
	prior_bars: tuple[ExactArchiveBar, ...]
	target_bars: tuple[ExactArchiveBar, ...]
	following_bars: tuple[ExactArchiveBar, ...]
	prior_records_id: str
	target_records_id: str
	following_records_id: str
	rows: tuple[LegacyDerivedRow, ...]
	dependency_id: str
	content_digest: str


def _read_prepared_file(item: dict[str, Any]) -> tuple[tuple[ExactArchiveBar, ...], str]:
	path = Path(str(item["path"]))
	file_hash = str(item["file_sha256"])
	records_id = str(item["records_id"])
	if _file_hash(path) != file_hash:
		raise ValueError(f"Changed Stage 6 input: {item['session']}")
	bars, observed_hash = read_exact_archive(path)
	if observed_hash != file_hash or exact_records_id(bars) != records_id:
		raise ValueError(f"Changed Stage 6 records identity: {item['session']}")
	return bars, records_id


def _prepare_one(arguments: dict[str, Any]) -> PreparedDerived:
	prior_bars, prior_records_id = _read_prepared_file(arguments["prior"])
	target_bars, target_records_id = _read_prepared_file(arguments["target"])
	following_bars, following_records_id = _read_prepared_file(arguments["following"])
	rows = _exact_derived_rows(prior_bars, target_bars, following_bars)
	dependency_id = content_id({
		"prior": arguments["prior_session"],
		"following": arguments["following_session"],
		"raw": [prior_records_id, following_records_id],
	})
	return PreparedDerived(
		str(arguments["session"]),
		str(arguments["prior_session"]),
		str(arguments["following_session"]),
		prior_bars,
		target_bars,
		following_bars,
		prior_records_id,
		target_records_id,
		following_records_id,
		rows,
		dependency_id,
		derived_id(rows),
	)


def _prepare_arguments(manifest: dict[str, Any], session: str) -> dict[str, Any]:
	target = next((item for item in manifest["targets"] if item["session"] == session), None)
	if not isinstance(target, dict) or len(target.get("derived_dependencies", ())) != 2:
		raise ValueError(f"Derived dependencies are unavailable: {session}")
	items = {str(item["session"]): item for item in manifest["files"]}
	prior_session, following_session = (str(value) for value in target["derived_dependencies"])
	selected = {"prior": prior_session, "target": session, "following": following_session}
	arguments: dict[str, Any] = {"session": session, "prior_session": prior_session, "following_session": following_session}
	for name, value in selected.items():
		item = items.get(value)
		if not isinstance(item, dict) or not item.get("valid") or not item.get("path"):
			raise ValueError(f"Derived source session is unavailable: {value}")
		arguments[name] = item
	return arguments


def _prepared_sessions(manifest: dict[str, Any], sessions: tuple[str, ...], preparers: int) -> Iterable[PreparedDerived]:
	if preparers not in (1, 3):
		raise ValueError("Derived preparation requires one or three preparers")
	with ProcessPoolExecutor(max_workers=preparers) as executor:
		pending: list[Any] = []
		remaining = iter(sessions)
		exhausted = False
		while pending or not exhausted:
			while len(pending) < 3:
				try:
					session = next(remaining)
				except StopIteration:
					exhausted = True
					break
				pending.append(executor.submit(_prepare_one, _prepare_arguments(manifest, session)))
			if pending:
				yield pending.pop(0).result()


def _checkpoint_directory(value: str) -> Path:
	path = Path(value)
	path.mkdir(parents=True, exist_ok=True)
	return path


def _python_worker(arguments: dict[str, str]) -> dict[str, Any]:
	manifest = load_manifest(Path(arguments["manifest"]))
	target = MySQLTarget(Path(arguments["dbinfo_file"]), arguments["schema"], manifest)
	checkpoint_directory = _checkpoint_directory(arguments["checkpoint_directory"])
	sessions = tuple(value for value in arguments["sessions"].split(",") if value)
	preparers = int(arguments["preparers"])
	backend = MySQLPythonBaseline(manifest, target, checkpoint_directory / "python.json")
	started = perf_counter()
	preparation_started = perf_counter()
	preparation_finished = preparation_started
	writing_seconds = 0.0
	rows = 0
	query_count = 0
	session_reports = []
	for prepared in _prepared_sessions(manifest, sessions, preparers):
		preparation_finished = perf_counter()
		write_started = perf_counter()
		result = backend._derived_session_prepared(
			prepared.session,
			(prepared.prior_session, prepared.following_session),
			prepared.prior_bars,
			prepared.target_bars,
			prepared.following_bars,
			checkpoint=checkpoint_directory / f"{prepared.session}.json",
		)
		writing_seconds += perf_counter() - write_started
		rows += len(prepared.rows)
		query_count += int(result["query_count"])
		session_reports.append({"session": prepared.session, "rows": len(prepared.rows), "status": result["status"], "content_id": prepared.content_digest, "query_count": result["query_count"]})
	return {
		"schema_version": REPORT_SCHEMA_VERSION,
		"backend": "python",
		"preparers": preparers,
		"status": "complete",
		"rows": rows,
		"workload_seconds": perf_counter() - started,
		"preparation_wall_seconds": preparation_finished - preparation_started,
		"write_wall_seconds": writing_seconds,
		"client_cpu_seconds": process_time(),
		"query_count": query_count,
		"query_count_scope": "DB-API execute/executemany calls; target validation is excluded",
		"transaction_unit": "one derived session",
		"sessions": session_reports,
	}


def _sql_worker(arguments: dict[str, str]) -> dict[str, Any]:
	manifest = load_manifest(Path(arguments["manifest"]))
	target = SQLDerivedTarget(Path(arguments["dbinfo_file"]), arguments["schema"], manifest)
	checkpoint_directory = _checkpoint_directory(arguments["checkpoint_directory"])
	sessions = tuple(value for value in arguments["sessions"].split(",") if value)
	candidate = SQLDerivedCandidate(manifest, target, checkpoint_directory / "sql.json")
	connection = target.connect()
	started = perf_counter()
	preparation_started = perf_counter()
	preparation_finished = preparation_started
	writing_seconds = 0.0
	rows = 0
	query_count = 0
	session_reports = []
	try:
		target.validate(connection)
		for session in sessions:
			prepared_started = perf_counter()
			prepared = _prepare_one(_prepare_arguments(manifest, session))
			preparation_finished = perf_counter()
			write_started = perf_counter()
			before_queries = candidate.query_count
			result = candidate.write_derived_prepared(
				prepared.session,
				prepared.prior_session,
				prepared.following_session,
				prepared.target_records_id,
				prepared.prior_records_id,
				prepared.following_records_id,
				prepared.dependency_id,
				prepared.content_digest,
				len(prepared.rows),
				connection=connection,
				checkpoint=checkpoint_directory / f"{prepared.session}.json",
			)
			writing_seconds += perf_counter() - write_started
			rows += len(prepared.rows)
			query_count += candidate.query_count - before_queries
			session_reports.append({"session": prepared.session, "rows": len(prepared.rows), "status": result["status"], "content_id": prepared.content_digest, "query_count": candidate.query_count - before_queries, "preparation_seconds": perf_counter() - prepared_started})
	finally:
		connection.close()
	return {
		"schema_version": REPORT_SCHEMA_VERSION,
		"backend": "sql",
		"preparers": 1,
		"status": "complete",
		"rows": rows,
		"workload_seconds": perf_counter() - started,
		"preparation_wall_seconds": preparation_finished - preparation_started,
		"write_wall_seconds": writing_seconds,
		"database_resident_seconds": writing_seconds,
		"query_count": query_count,
		"query_count_scope": "Stage 8b routine/staging statements; target validation is excluded",
		"transaction_unit": "one derived session; one writer connection",
		"sessions": session_reports,
	}


def _java_worker_command(arguments: dict[str, str]) -> list[str]:
	repository = Path(__file__).resolve().parents[3]
	java_root = repository / "java"
	classpath = ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))
	return [
		"java", "-Xmx512m", "-XX:ActiveProcessorCount=3", "-XX:+UseSerialGC", "-cp", classpath,
		"stockguidance.migration.JavaDerivedBenchmarkMain", "--manifest", arguments["manifest"],
		"--dbinfo-file", arguments["dbinfo_file"], "--disposable-schema", arguments["schema"],
		"--preparers", arguments["preparers"], "--sessions", arguments["sessions"],
		"--checkpoint-directory", arguments["checkpoint_directory"],
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
		command = [sys.executable, "-m", "stockguidance.performance.stage9_derived", "--worker", backend]
		for name in ("manifest", "dbinfo_file", "schema", "preparers", "sessions", "checkpoint_directory"):
			command.extend((f"--{name.replace('_', '-')}", arguments[name]))
	environment = _native_thread_environment()
	started = perf_counter()
	process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
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
	log_path = output_directory / "logs" / f"{arguments['backend']}-p{arguments['preparers']}-{arguments['block']}-{arguments['repetition']}.stderr.log"
	log_path.parent.mkdir(parents=True, exist_ok=True)
	log_path.write_text(stderr[-12000:], encoding="utf-8")
	result: dict[str, Any] = {"returncode": process.returncode, "timed_out": timed_out, "end_to_end_seconds": wall_seconds, "startup_and_handoff_seconds": None, "stderr_log": str(log_path), **resources}
	if not timed_out and process.returncode == 0:
		try:
			payload = json.loads(stdout)
		except json.JSONDecodeError:
			result["status"] = "failed"
			result["failure_type"] = "invalid_worker_report"
		else:
			result["status"] = payload.get("status", "failed")
			result["worker"] = payload
			result["startup_and_handoff_seconds"] = max(0.0, wall_seconds - float(payload.get("workload_seconds", 0.0)))
	else:
		result["status"] = "timed_out" if timed_out else "failed"
		result["failure_type"] = "timeout" if timed_out else "worker_exit"
	return result


def _target_tables(manifest: dict[str, Any]) -> dict[str, str]:
	return {name: str(value) for name, value in manifest["authoritative_registry"]["tables"].items()}


def _clear_target(target: MySQLTarget, manifest: dict[str, Any]) -> None:
	names = _target_tables(manifest)
	connection = target.connect()
	try:
		target.validate(connection)
		cursor = connection.cursor()
		for table in ("stage8b_derived_stage", "stage8a_raw_stage", "stage6_completion", names["imports"], names["deltas"], names["prices"]):
			try:
				cursor.execute(f"DELETE FROM {quote_identifier(table)}")
			except Exception:
				if table not in ("stage8b_derived_stage", "stage8a_raw_stage"):
					raise
		connection.commit()
	finally:
		connection.close()


def _seed_target(target: MySQLTarget, manifest: dict[str, Any], sessions: set[str], directory: Path) -> int:
	backend = MySQLPythonBaseline(manifest, target, directory / "seed.json")
	rows = 0
	for session in sorted(sessions):
		result = backend._raw_session(session, seed=True)
		rows += int(result["rows"])
	return rows


def _derived_sessions(manifest: dict[str, Any], block: str) -> tuple[str, ...]:
	values = tuple(str(value) for value in manifest["target_blocks"][block])
	if any(not any(item["session"] == session and block in item.get("roles", ()) for item in manifest["targets"]) for session in values):
		raise ValueError(f"Manifest target block is incomplete: {block}")
	return values


def _seed_sessions(manifest: dict[str, Any], sessions: tuple[str, ...]) -> set[str]:
	result = set(sessions)
	for target in manifest["targets"]:
		if target["session"] in result:
			result.update(str(value) for value in target["derived_dependencies"])
	return result


def _audit_target(target: MySQLTarget, manifest: dict[str, Any], sessions: tuple[str, ...]) -> dict[str, Any]:
	names = _target_tables(manifest)
	connection = target.connect()
	checked_rows = 0
	try:
		target.validate(connection)
		cursor = connection.cursor(buffered=True)
		for session in sessions:
			prepared = _prepare_one(_prepare_arguments(manifest, session))
			cursor.execute(f"SELECT t.label, {', '.join('d.' + quote_identifier(column) for column in DERIVED_COLUMNS)} FROM {quote_identifier(names['deltas'])} d JOIN {quote_identifier(names['tickers'])} t ON t.id = d.ticker_id JOIN {quote_identifier(names['sessions'])} s ON s.id = d.date_id WHERE s.transaction_date = %s ORDER BY t.label", (session,))
			actual = {str(row[0]): tuple(row[1:]) for row in cursor.fetchall()}
			if set(actual) != {row.ticker for row in prepared.rows}:
				raise ValueError(f"Derived ticker population differs: {session}")
			for expected in prepared.rows:
				for observed, required in zip(actual[expected.ticker], expected.values, strict=True):
					if not _equal(observed, required):
						raise ValueError(f"Derived value differs: {session}/{expected.ticker}")
			cursor.execute(f"SELECT derived_dependency_id, derived_content_id FROM {quote_identifier(names['imports'])} WHERE session_date = %s", (session,))
			if cursor.fetchall() != [(prepared.dependency_id, prepared.content_digest)]:
				raise ValueError(f"Derived receipt differs: {session}")
			cursor.execute("SELECT content_digest FROM `stage6_completion` WHERE workload = %s AND session_date = %s AND series_key = %s", ("derived", session, "legacy-open-to-close-v1"))
			if cursor.fetchall() != [(prepared.content_digest,)]:
				raise ValueError(f"Derived completion differs: {session}")
			checked_rows += len(prepared.rows)
		return {"status": "passed", "sessions": len(sessions), "rows": checked_rows, "derived_tolerance": "existing persistence tolerance", "exact_case_keys": True}
	finally:
		connection.close()


def _schema_name(backend: str, preparers: int) -> str:
	return f"stockguidance_disposable_s9b_{backend[0]}{preparers}_{uuid4().hex[:8]}"


def _median(values: list[float]) -> float | None:
	if not values:
		return None
	ordered = sorted(values)
	return ordered[len(ordered) // 2]


def _recovery_evidence(repository: Path) -> dict[str, Any]:
	evidence: dict[str, Any] = {}
	for backend, filename in (("python", "python-derived.json"), ("java", "java-writer-mysql.json"), ("sql", "sql-derived-mysql.json")):
		path = repository / "output/legacy-migration/benchmark/candidates" / filename
		if not path.exists():
			evidence[backend] = {"status": "missing", "path": str(path)}
			continue
		value = json.loads(path.read_text(encoding="utf-8"))
		checks = value.get("recovery_checks") or ([] if backend == "python" else None)
		status = "passed_reference" if backend == "python" and value.get("status") == "complete" else "passed" if value.get("status") == "complete" and checks else "incomplete"
		evidence[backend] = {"status": status, "path": str(path), "sha256": _file_hash(path), "checks": checks, "storage_engine": value.get("storage_engine") or "offline_reference"}
	return evidence


def _code_hashes(repository: Path) -> dict[str, str]:
	paths = [Path(__file__), Path(__file__).with_name("candidates.py"), Path(__file__).with_name("mysql_candidates.py"), Path(__file__).with_name("sql_derived_candidate.py")]
	paths.extend((repository / "java/src/main/java/stockguidance/migration").glob("JavaDerivedBenchmarkMain.java"))
	paths.extend((repository / "java/src/main/java/stockguidance/migration").glob("JavaWriterCandidate.java"))
	return {str(path.relative_to(repository)): _file_hash(path) for path in paths}


def run_stage9_derived(manifest_path: Path, dbinfo_file: Path, output: Path, *, timeout_seconds: float = 3600.0) -> dict[str, Any]:
	manifest = load_manifest(manifest_path)
	repository = Path(__file__).resolve().parents[3]
	output = output.resolve()
	output.mkdir(parents=True, exist_ok=True)
	derived_directory = output / "derived"
	derived_directory.mkdir(parents=True, exist_ok=True)
	report: dict[str, Any] = {
		"schema_version": REPORT_SCHEMA_VERSION,
		"benchmark": "stockguidance-legacy-migration-stage9b-derived-mysql-v1",
		"stage": "9b",
		"status": "incomplete",
		"manifest": {"path": str(manifest_path.resolve()), "id": manifest["manifest_id"], "sha256": _file_hash(manifest_path)},
		"workload": "derived_computation_persistence",
		"blocks": list(BLOCKS),
		"configurations": {},
		"protocol": {"first_plus_warm_repetitions": 4, "candidate_orders": manifest["repetitions"]["candidate_orders"], "writers": 1, "queued_sessions_max": 3, "native_threads": 1, "java_heap": "-Xmx512m", "python_java_preparers": [1, 3], "sql_preparers": "inapplicable: one database writer connection", "cache_state": "unknown", "timeout_seconds": timeout_seconds, "timing_scope": "candidate process startup through durable derived commit and checkpoint; target setup/reset, raw seeding and correctness audit excluded"},
		"server_metrics": {"status": "captured per disposable target", "competing_trials": "none; candidates run sequentially", "client_monitoring": "psutil process-tree sampling when available", "server_resource_metrics": "unavailable unless exposed by configured account", "sql_database_resident_phase": "reported separately from source preparation"},
		"production_access": {"ddl": False, "dml": False, "read_only_extraction": False},
		"recovery_evidence": _recovery_evidence(repository),
		"source_code_hashes": _code_hashes(repository),
	}

	def save() -> None:
		(output / "stage9b-derived.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	targets: dict[str, MySQLTarget] = {}
	for backend in BACKENDS:
		for preparers in PREPARERS[backend]:
			configuration_key = f"{backend}-p{preparers}"
			configuration: dict[str, Any] = {"backend": backend, "preparers": preparers, "status": "incomplete", "schema": None, "target_setup": None, "blocks": {}}
			report["configurations"][configuration_key] = configuration
			schema = _schema_name(backend, preparers)
			target = SQLDerivedTarget(dbinfo_file.resolve(), schema, manifest) if backend == "sql" else MySQLTarget(dbinfo_file.resolve(), schema, manifest)
			setup_started = perf_counter()
			configuration["target_setup"] = target.prepare()
			configuration["schema"] = schema
			usage = shutil.disk_usage(repository)
			configuration["disk"] = {"free_bytes": usage.free, "capacity_bytes": usage.total}
			configuration["setup_seconds"] = perf_counter() - setup_started
			targets[configuration_key] = target
			for block in BLOCKS:
				sessions = _derived_sessions(manifest, block)
				configuration["blocks"][block] = {"status": "incomplete", "sessions": list(sessions), "repetitions": [], "correctness": []}

	executed_orders = []
	for repetition, order in enumerate(manifest["repetitions"]["candidate_orders"]):
		executed_order = []
		for backend in order:
			for preparers in PREPARERS[backend]:
				configuration_key = f"{backend}-p{preparers}"
				configuration = report["configurations"][configuration_key]
				target = targets[configuration_key]
				for block in BLOCKS:
					block_report = configuration["blocks"][block]
					sessions = tuple(block_report["sessions"])
					reset_started = perf_counter()
					_clear_target(target, manifest)
					_seed_target(target, manifest, _seed_sessions(manifest, sessions), derived_directory / "setup")
					reset_seconds = perf_counter() - reset_started
					arguments = {"backend": backend, "manifest": str(manifest_path.resolve()), "dbinfo_file": str(dbinfo_file.resolve()), "schema": configuration["schema"], "preparers": str(preparers), "sessions": ",".join(sessions), "checkpoint_directory": str(derived_directory / "checkpoints" / f"{configuration_key}-{block}-{repetition}"), "block": block, "repetition": str(repetition)}
					trial = _run_child(arguments, timeout_seconds, derived_directory)
					trial["repetition"] = repetition
					trial["candidate_order"] = list(order)
					trial["block"] = block
					trial["reset_seconds"] = reset_seconds
					if trial.get("status") == "complete":
						try:
							trial["correctness"] = _audit_target(target, manifest, sessions)
						except Exception as error:
							trial["status"] = "failed_correctness"
							trial["failure_type"] = type(error).__name__
							trial["correctness_error"] = str(error)
					if trial.get("correctness", {}).get("status") == "passed":
						block_report["correctness"].append(trial["correctness"])
					else:
						block_report["correctness"].append({"status": "not_available"})
					block_report["repetitions"].append(trial)
					configuration["last_trial"] = {"block": block, "repetition": repetition, "status": trial["status"]}
					executed_order.append(configuration_key)
					(configuration_path := derived_directory / f"{configuration_key}.json").write_text(json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8")
					save()
		executed_orders.append(executed_order)
	report["protocol"]["executed_orders"] = executed_orders
	for configuration_key, configuration in report["configurations"].items():
		for block_report in configuration["blocks"].values():
			successful = [item for item in block_report["repetitions"] if item.get("status") == "complete" and item.get("correctness", {}).get("status") == "passed"]
			block_report["status"] = "measured" if len(successful) == 4 else "incomplete"
			block_report["warm_median_seconds"] = _median([float(item["end_to_end_seconds"]) for item in successful[1:]]) if len(successful) == 4 else None
			block_report["first_seconds"] = float(successful[0]["end_to_end_seconds"]) if successful else None
			block_report["rows_per_trial"] = int(successful[0]["worker"]["rows"]) if successful else None
		configuration["status"] = "measured" if all(value["status"] == "measured" for value in configuration["blocks"].values()) else "incomplete"
		configuration["warm_median_seconds"] = sum(float(value["warm_median_seconds"]) for value in configuration["blocks"].values() if value["warm_median_seconds"] is not None) if configuration["status"] == "measured" else None
		configuration["throughput_rows_per_second"] = sum(int(value["rows_per_trial"]) for value in configuration["blocks"].values() if value["rows_per_trial"] is not None) / configuration["warm_median_seconds"] if configuration["warm_median_seconds"] else None
		(derived_directory / f"{configuration_key}.json").write_text(json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8")
		save()
	valid_configurations = [value for value in report["configurations"].values() if value.get("status") == "measured"]
	report["comparison_table"] = [{"workload": "derived_computation_persistence", "backend": backend, "status": "measured" if any(value.get("backend") == backend and value.get("status") == "measured" for value in valid_configurations) else "incomplete", "configurations": [key for key, value in report["configurations"].items() if value.get("backend") == backend]} for backend in BACKENDS]
	report["status"] = "complete" if len(valid_configurations) == len(report["configurations"]) else "incomplete"
	save()
	return report


def aggregate_stage9_study(raw_report: dict[str, Any], derived_report: dict[str, Any], output: Path) -> dict[str, Any]:
	rows: list[dict[str, Any]] = []
	for backend in BACKENDS:
		configurations = {key: value for key, value in raw_report["configurations"].items() if value["backend"] == backend}
		valid = [(key, value) for key, value in configurations.items() if value.get("status") == "measured" and value.get("warm_median_seconds") is not None]
		best = min(valid, key=lambda item: float(item[1]["warm_median_seconds"])) if valid else None
		rows.append({"workload": "raw_adoption_repair", "backend": backend, "status": "measured" if best else "incomplete", "best_configuration": None if best is None else best[0], "warm_median_seconds": None if best is None else best[1]["warm_median_seconds"], "throughput_rows_per_second": None if best is None else best[1]["throughput_rows_per_second"]})
	for backend in BACKENDS:
		configurations = {key: value for key, value in derived_report["configurations"].items() if value["backend"] == backend}
		valid = [(key, value) for key, value in configurations.items() if value.get("status") == "measured" and value.get("warm_median_seconds") is not None]
		best = min(valid, key=lambda item: float(item[1]["warm_median_seconds"])) if valid else None
		rows.append({"workload": "derived_computation_persistence", "backend": backend, "status": "measured" if best else "incomplete", "best_configuration": None if best is None else best[0], "warm_median_seconds": None if best is None else best[1]["warm_median_seconds"], "throughput_rows_per_second": None if best is None else best[1]["throughput_rows_per_second"]})
	for backend in BACKENDS:
		rows.append({"workload": "zscores_persistence", "backend": backend, "status": "pending_stage9c", "best_configuration": None, "warm_median_seconds": None, "throughput_rows_per_second": None})
	study = {"schema_version": 1, "benchmark": "stockguidance-legacy-migration-stage9-candidate-study-v1", "stage": 9, "status": "incomplete", "substage": {"9a": raw_report["status"], "9b": derived_report["status"], "9c": "pending"}, "manifest": {"id": derived_report["manifest"]["id"], "raw_report": str((output / "stage9a-raw.json").resolve()), "derived_report": str((output / "stage9b-derived.json").resolve())}, "comparison_table": rows, "raw": raw_report, "derived": derived_report, "selection": {"production_backend_selected": False, "selection_stage": 10, "rule": "Java and SQL qualify only when a valid startup-inclusive warm median is at least 10% faster than Python"}, "production_access": {"ddl": False, "dml": False, "read_only_extraction": False}}
	output.mkdir(parents=True, exist_ok=True)
	(output / "study.json").write_text(json.dumps(study, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return study


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


def main(argv: list[str] | None = None) -> int:
	arguments = list(sys.argv[1:] if argv is None else argv)
	if arguments and arguments[0] == "--worker":
		worker_name = arguments[1]
		worker_arguments = _worker_arguments(arguments[2:])
		result = _python_worker(worker_arguments) if worker_name == "python" else _sql_worker(worker_arguments)
		print(json.dumps(result, sort_keys=True))
		return 0
	raise SystemExit("Stage 9b derived module requires --worker")


if __name__ == "__main__":
	raise SystemExit(main())
