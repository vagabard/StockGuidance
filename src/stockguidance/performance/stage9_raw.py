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
from stockguidance.performance.candidates import _file_hash
from stockguidance.performance.mysql_candidates import MySQLPythonBaseline, MySQLTarget, load_manifest
from stockguidance.performance.sql_raw_candidate import SQLRawCandidate, SQLRawTarget


BACKENDS = ("python", "java", "sql")
SCENARIOS = ("matching_adoption", "missing_or_different_repair", "unchanged_rerun")
PREPARERS = {"python": (1, 3), "java": (1, 3), "sql": (1,)}
REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PreparedRaw:
	session: str
	bars: tuple[ExactArchiveBar, ...]
	file_sha256: str
	records_id: str


def _prepare_one(item: dict[str, Any]) -> PreparedRaw:
	session = str(item["session"])
	path = Path(str(item["path"]))
	file_sha256 = str(item["file_sha256"])
	records_id = str(item["records_id"])
	if _file_hash(path) != file_sha256:
		raise ValueError(f"Changed Stage 6 input: {session}")
	bars, observed_hash = read_exact_archive(path)
	if observed_hash != file_sha256 or exact_records_id(bars) != records_id:
		raise ValueError(f"Changed Stage 6 records identity: {session}")
	return PreparedRaw(session, bars, observed_hash, records_id)


def _prepared_sessions(manifest: dict[str, Any], sessions: tuple[str, ...], preparers: int) -> Iterable[PreparedRaw]:
	if preparers not in (1, 3):
		raise ValueError("Raw preparation requires one or three preparers")
	items = {str(item["session"]): item for item in manifest["files"]}
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
				item = items.get(session)
				if not isinstance(item, dict) or not item.get("valid") or not item.get("path"):
					raise ValueError(f"Raw source session is unavailable: {session}")
				pending.append(executor.submit(_prepare_one, item))
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
	changed_rows = 0
	query_count = 0
	session_reports = []
	for prepared in _prepared_sessions(manifest, sessions, preparers):
		preparation_finished = perf_counter()
		write_started = perf_counter()
		result = backend._raw_session_prepared(prepared.session, prepared.bars, prepared.records_id)
		writing_seconds += perf_counter() - write_started
		rows += len(prepared.bars)
		changed_rows += int(result["changed_rows"])
		query_count += int(result["query_count"])
		session_reports.append({"session": prepared.session, "rows": len(prepared.bars), "changed_rows": result["changed_rows"], "query_count": result["query_count"]})
	return {
		"schema_version": REPORT_SCHEMA_VERSION,
		"backend": "python",
		"preparers": preparers,
		"status": "complete",
		"rows": rows,
		"changed_rows": changed_rows,
		"workload_seconds": perf_counter() - started,
		"preparation_wall_seconds": preparation_finished - preparation_started,
		"write_wall_seconds": writing_seconds,
		"client_cpu_seconds": process_time(),
		"query_count": query_count,
		"query_count_scope": "DB-API execute/executemany calls; target validation is excluded",
		"transaction_unit": "one session",
		"sessions": session_reports,
	}


def _sql_worker(arguments: dict[str, str]) -> dict[str, Any]:
	manifest = load_manifest(Path(arguments["manifest"]))
	target = SQLRawTarget(Path(arguments["dbinfo_file"]), arguments["schema"], manifest)
	checkpoint_directory = _checkpoint_directory(arguments["checkpoint_directory"])
	sessions = tuple(value for value in arguments["sessions"].split(",") if value)
	candidate = SQLRawCandidate(manifest, target, checkpoint_directory / "sql.json")
	connection = target.connect()
	started = perf_counter()
	rows = 0
	changed_rows = 0
	loader_seconds = 0.0
	query_count = 0
	session_reports = []
	try:
		target.validate(connection)
		for session in sessions:
			before_queries = candidate.query_count
			result = candidate.write_raw(session, connection=connection)
			rows += int(result["rows"])
			changed_rows += int(result["changed_rows"])
			loader_seconds += float(result["loader_seconds"])
			query_count += candidate.query_count - before_queries
			session_reports.append({"session": session, "rows": result["rows"], "changed_rows": result["changed_rows"], "loader_seconds": result["loader_seconds"], "query_count": candidate.query_count - before_queries})
	finally:
		connection.close()
	return {
		"schema_version": REPORT_SCHEMA_VERSION,
		"backend": "sql",
		"preparers": 1,
		"status": "complete",
		"rows": rows,
		"changed_rows": changed_rows,
		"workload_seconds": perf_counter() - started,
		"preparation_wall_seconds": loader_seconds,
		"write_wall_seconds": max(0.0, perf_counter() - started - loader_seconds),
		"loader_seconds": loader_seconds,
		"query_count": query_count,
		"query_count_scope": "candidate staging/procedure calls; target validation is excluded",
		"transaction_unit": "one session; one writer connection",
		"sessions": session_reports,
	}


def _java_worker_command(arguments: dict[str, str]) -> list[str]:
	repository = Path(__file__).resolve().parents[3]
	java_root = repository / "java"
	classpath = ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))
	return [
		"java", "-Xmx512m", "-XX:ActiveProcessorCount=3", "-XX:+UseSerialGC", "-cp", classpath,
		"stockguidance.migration.JavaRawBenchmarkMain", "--manifest", arguments["manifest"],
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
		command = [sys.executable, "-m", "stockguidance.performance.stage9_raw", "--worker", backend]
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
	log_path = output_directory / "logs" / f"{arguments['backend']}-p{arguments['preparers']}-{arguments['scenario']}-{arguments['repetition']}.stderr.log"
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
		for table in ("stage6_completion", names["imports"], names["deltas"], names["prices"]):
			cursor.execute(f"DELETE FROM {quote_identifier(table)}")
		if isinstance(target, SQLRawTarget):
			cursor.execute("DELETE FROM `stage8a_raw_stage`")
		connection.commit()
	finally:
		connection.close()


def _seed_target(target: MySQLTarget, manifest: dict[str, Any], directory: Path) -> int:
	backend = MySQLPythonBaseline(manifest, target, directory / "seed.json")
	rows = 0
	for session in manifest["target_blocks"]["fixed_25"]:
		result = backend._raw_session(session, seed=True)
		rows += int(result["rows"])
	return rows


def _repair_target(target: MySQLTarget, manifest: dict[str, Any]) -> None:
	names = _target_tables(manifest)
	session = manifest["target_blocks"]["fixed_25"][0]
	connection = target.connect()
	try:
		target.validate(connection)
		cursor = connection.cursor()
		cursor.execute(f"SELECT id FROM {quote_identifier(names['sessions'])} WHERE transaction_date = %s", (session,))
		date_id = cursor.fetchone()[0]
		cursor.execute(f"SELECT ticker_id FROM {quote_identifier(names['prices'])} WHERE date_id = %s ORDER BY ticker_id LIMIT 2", (date_id,))
		ticker_ids = [row[0] for row in cursor.fetchall()]
		if len(ticker_ids) != 2:
			raise ValueError("Repair fixture lacks two raw rows")
		cursor.execute(f"DELETE FROM {quote_identifier(names['prices'])} WHERE date_id = %s AND ticker_id = %s", (date_id, ticker_ids[0]))
		cursor.execute(f"UPDATE {quote_identifier(names['prices'])} SET `close` = %s WHERE date_id = %s AND ticker_id = %s", ("10", date_id, ticker_ids[1]))
		connection.commit()
	finally:
		connection.close()


def _audit_target(target: MySQLTarget, manifest: dict[str, Any], sessions: tuple[str, ...]) -> dict[str, Any]:
	names = _target_tables(manifest)
	connection = target.connect()
	checked_rows = 0
	try:
		target.validate(connection)
		cursor = connection.cursor()
		for session in sessions:
			item = next(value for value in manifest["files"] if value["session"] == session)
			bars, file_hash = read_exact_archive(Path(item["path"]))
			if file_hash != item["file_sha256"] or exact_records_id(bars) != item["records_id"]:
				raise ValueError(f"Audit source identity changed: {session}")
			cursor.execute(f"SELECT t.label, p.`open`, p.`high`, p.`low`, p.`close`, p.volume, p.vwap, p.transactions FROM {quote_identifier(names['prices'])} p JOIN {quote_identifier(names['tickers'])} t ON t.id = p.ticker_id JOIN {quote_identifier(names['sessions'])} s ON s.id = p.date_id WHERE s.transaction_date = %s", (session,))
			actual = {row[0]: row[1:] for row in cursor.fetchall()}
			if len(actual) != len(bars):
				raise ValueError(f"Raw audit row count differs: {session}")
			for bar in bars:
				values = actual.get(bar.ticker)
				if values is None:
					raise ValueError(f"Raw audit ticker is missing: {session}/{bar.ticker}")
				expected = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)
				if any((None if stored is None else Decimal(str(stored))) != expected_value for stored, expected_value in zip(values, expected, strict=True)):
					raise ValueError(f"Raw audit value differs: {session}/{bar.ticker}")
			cursor.execute(f"SELECT raw_id FROM {quote_identifier(names['imports'])} WHERE session_date = %s", (session,))
			if cursor.fetchall() != [(item["records_id"],)]:
				raise ValueError(f"Raw receipt differs: {session}")
			cursor.execute("SELECT content_digest FROM `stage6_completion` WHERE workload = %s AND session_date = %s AND series_key = %s", ("raw", session, "raw"))
			if cursor.fetchall() != [(item["records_id"],)]:
				raise ValueError(f"Raw completion differs: {session}")
			checked_rows += len(bars)
		return {"status": "passed", "sessions": len(sessions), "rows": checked_rows, "exact_decimals": True, "exact_case_keys": True}
	finally:
		connection.close()


def _schema_name(backend: str, preparers: int) -> str:
	return f"stockguidance_disposable_s9a_{backend[0]}{preparers}_{uuid4().hex[:8]}"


def _median(values: list[float]) -> float | None:
	if not values:
		return None
	ordered = sorted(values)
	return ordered[len(ordered) // 2]


def _recovery_evidence(repository: Path) -> dict[str, Any]:
	evidence = {}
	for backend, filename in (("python", "python-raw.json"), ("java", "java-writer-mysql.json"), ("sql", "sql-raw-mysql.json")):
		path = repository / "output/legacy-migration/benchmark/candidates" / filename
		if not path.exists():
			evidence[backend] = {"status": "missing", "path": str(path)}
			continue
		value = json.loads(path.read_text(encoding="utf-8"))
		recovery = value.get("recovery_checks", [])
		evidence[backend] = {"status": "passed" if value.get("status") == "complete" and (recovery or backend == "python") else "incomplete", "path": str(path), "sha256": _file_hash(path), "checks": recovery}
	return evidence


def run_stage9_raw(manifest_path: Path, dbinfo_file: Path, output: Path, *, timeout_seconds: float = 3600.0) -> dict[str, Any]:
	manifest = load_manifest(manifest_path)
	repository = Path(__file__).resolve().parents[3]
	output = output.resolve()
	output.mkdir(parents=True, exist_ok=True)
	raw_directory = output / "raw"
	raw_directory.mkdir(parents=True, exist_ok=True)
	report: dict[str, Any] = {
		"schema_version": REPORT_SCHEMA_VERSION,
		"benchmark": "stockguidance-legacy-migration-stage9a-raw-mysql-v1",
		"stage": "9a",
		"status": "incomplete",
		"manifest": {"path": str(manifest_path.resolve()), "id": manifest["manifest_id"], "sha256": _file_hash(manifest_path)},
		"workload": "raw_adoption_repair",
		"scenarios": SCENARIOS,
		"configurations": {},
		"protocol": {"first_plus_warm_repetitions": 4, "candidate_orders": manifest["repetitions"]["candidate_orders"], "writers": 1, "queued_sessions_max": 3, "native_threads": 1, "java_heap": "-Xmx512m", "python_java_preparers": [1, 3], "sql_preparers": "inapplicable: one database writer connection", "cache_state": "unknown", "timeout_seconds": timeout_seconds, "timing_scope": "candidate process startup through durable commit and checkpoint; target setup/reset and correctness audit excluded"},
		"server_metrics": {"status": "captured per disposable target", "competing_trials": "none; candidates run sequentially", "client_monitoring": "psutil process-tree sampling when available", "server_resource_metrics": "unavailable unless exposed by configured account"},
		"production_access": {"ddl": False, "dml": False, "read_only_extraction": False},
		"recovery_evidence": _recovery_evidence(repository),
		"source_code_hashes": {},
	}
	for path in (Path(__file__), Path(__file__).with_name("candidates.py"), Path(__file__).with_name("mysql_candidates.py"), Path(__file__).with_name("sql_raw_candidate.py")):
		report["source_code_hashes"][str(path.relative_to(repository))] = _file_hash(path)

	def save() -> None:
		(output / "stage9a-raw.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

	targets: dict[str, MySQLTarget] = {}
	for backend in BACKENDS:
		for preparers in PREPARERS[backend]:
			configuration_key = f"{backend}-p{preparers}"
			configuration: dict[str, Any] = {"backend": backend, "preparers": preparers, "status": "incomplete", "schema": None, "target_setup": None, "scenarios": {}}
			report["configurations"][configuration_key] = configuration
			schema = _schema_name(backend, preparers)
			target = (SQLRawTarget if backend == "sql" else MySQLTarget)(dbinfo_file.resolve(), schema, manifest)
			setup_started = perf_counter()
			configuration["target_setup"] = target.prepare()
			configuration["schema"] = schema
			configuration["disk"] = {"free_bytes": shutil.disk_usage(repository).free, "capacity_bytes": shutil.disk_usage(repository).total}
			configuration["setup_seconds"] = perf_counter() - setup_started
			targets[configuration_key] = target
			for scenario in SCENARIOS:
				scenario_sessions = tuple(manifest["target_blocks"]["fixed_25"]) if scenario == "matching_adoption" else (manifest["target_blocks"]["fixed_25"][0],)
				configuration["scenarios"][scenario] = {"status": "incomplete", "sessions": list(scenario_sessions), "repetitions": [], "correctness": []}

	executed_orders = []
	for repetition, order in enumerate(manifest["repetitions"]["candidate_orders"]):
		executed_order = []
		for backend in order:
			for preparers in PREPARERS[backend]:
				configuration_key = f"{backend}-p{preparers}"
				configuration = report["configurations"][configuration_key]
				target = targets[configuration_key]
				for scenario in SCENARIOS:
					scenario_report = configuration["scenarios"][scenario]
					scenario_sessions = tuple(scenario_report["sessions"])
					reset_started = perf_counter()
					_clear_target(target, manifest)
					if scenario != "matching_adoption":
						_seed_target(target, manifest, raw_directory / "setup")
					if scenario == "missing_or_different_repair":
						_repair_target(target, manifest)
					reset_seconds = perf_counter() - reset_started
					arguments = {"backend": backend, "manifest": str(manifest_path.resolve()), "dbinfo_file": str(dbinfo_file.resolve()), "schema": configuration["schema"], "preparers": str(preparers), "sessions": ",".join(scenario_sessions), "checkpoint_directory": str(raw_directory / "checkpoints" / f"{configuration_key}-{scenario}-{repetition}"), "scenario": scenario, "repetition": str(repetition)}
					trial = _run_child(arguments, timeout_seconds, raw_directory)
					trial["repetition"] = repetition
					trial["candidate_order"] = list(order)
					trial["reset_seconds"] = reset_seconds
					if trial.get("status") == "complete":
						try:
							trial["correctness"] = _audit_target(target, manifest, scenario_sessions)
						except Exception as error:
							trial["status"] = "failed_correctness"
							trial["failure_type"] = type(error).__name__
							trial["correctness_error"] = str(error)
					if trial.get("correctness", {}).get("status") == "passed":
						scenario_report["correctness"].append(trial["correctness"])
					else:
						scenario_report["correctness"].append({"status": "not_available"})
					scenario_report["repetitions"].append(trial)
					configuration["last_trial"] = {"scenario": scenario, "repetition": repetition, "status": trial["status"]}
					executed_order.append(configuration_key)
					report_path = raw_directory / f"{configuration_key}.json"
					report_path.write_text(json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8")
					save()
		executed_orders.append(executed_order)
	report["protocol"]["executed_orders"] = executed_orders
	for configuration_key, configuration in report["configurations"].items():
		for scenario_report in configuration["scenarios"].values():
			successful = [item for item in scenario_report["repetitions"] if item.get("status") == "complete" and item.get("correctness", {}).get("status") == "passed"]
			scenario_report["status"] = "measured" if len(successful) == 4 else "incomplete"
			scenario_report["warm_median_seconds"] = _median([float(item["end_to_end_seconds"]) for item in successful[1:]]) if len(successful) == 4 else None
			scenario_report["first_seconds"] = float(successful[0]["end_to_end_seconds"]) if successful else None
			scenario_report["rows_per_trial"] = int(successful[0]["worker"]["rows"]) if successful else None
		configuration["status"] = "measured" if all(value["status"] == "measured" for value in configuration["scenarios"].values()) else "incomplete"
		configuration["warm_median_seconds"] = sum(float(value["warm_median_seconds"]) for value in configuration["scenarios"].values() if value["warm_median_seconds"] is not None) if configuration["status"] == "measured" else None
		configuration["throughput_rows_per_second"] = sum(int(value["rows_per_trial"]) for value in configuration["scenarios"].values() if value["rows_per_trial"] is not None) / configuration["warm_median_seconds"] if configuration["warm_median_seconds"] else None
		report_path = raw_directory / f"{configuration_key}.json"
		report_path.write_text(json.dumps(configuration, indent=2, sort_keys=True) + "\n", encoding="utf-8")
		save()
	valid_configurations = [value for value in report["configurations"].values() if value.get("status") == "measured"]
	report["comparison_table"] = [{"workload": "raw_adoption_repair", "backend": backend, "status": "measured" if any(value.get("backend") == backend and value.get("status") == "measured" for value in valid_configurations) else "incomplete", "configurations": [key for key, value in report["configurations"].items() if value.get("backend") == backend]} for backend in BACKENDS]
	report["status"] = "complete" if len(valid_configurations) == len(report["configurations"]) else "incomplete"
	save()
	return report


def aggregate_stage9_study(raw_report: dict[str, Any], output: Path) -> dict[str, Any]:
	rows = []
	for backend in BACKENDS:
		configurations = {key: value for key, value in raw_report["configurations"].items() if value["backend"] == backend}
		valid = [(key, value) for key, value in configurations.items() if value.get("status") == "measured" and value.get("warm_median_seconds") is not None]
		best = min(valid, key=lambda item: float(item[1]["warm_median_seconds"])) if valid else None
		rows.append({"workload": "raw_adoption_repair", "backend": backend, "status": "measured" if best else "incomplete", "best_configuration": None if best is None else best[0], "warm_median_seconds": None if best is None else best[1]["warm_median_seconds"], "throughput_rows_per_second": None if best is None else best[1]["throughput_rows_per_second"]})
	for workload in ("derived_computation_persistence", "zscores_persistence"):
		for backend in BACKENDS:
			rows.append({"workload": workload, "backend": backend, "status": "pending_stage9b_or_9c", "best_configuration": None, "warm_median_seconds": None, "throughput_rows_per_second": None})
	study = {"schema_version": 1, "benchmark": "stockguidance-legacy-migration-stage9-candidate-study-v1", "stage": 9, "status": "incomplete", "substage": {"9a": raw_report["status"], "9b": "pending", "9c": "pending"}, "manifest": {"id": raw_report["manifest"]["id"], "raw_report": str((output / "stage9a-raw.json").resolve())}, "comparison_table": rows, "raw": raw_report, "selection": {"production_backend_selected": False, "selection_stage": 10, "rule": "Java and SQL qualify only when a valid startup-inclusive warm median is at least 10% faster than Python"}, "production_access": {"ddl": False, "dml": False, "read_only_extraction": False}}
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
		worker_arguments["worker"] = worker_name
		result = _python_worker(worker_arguments) if worker_name == "python" else _sql_worker(worker_arguments)
		print(json.dumps(result, sort_keys=True))
		return 0
	if arguments and arguments[0] == "--run":
		worker_arguments = _worker_arguments(arguments[1:])
		output = Path(worker_arguments["output"])
		raw_report = run_stage9_raw(Path(worker_arguments["manifest"]), Path(worker_arguments["dbinfo_file"]), output, timeout_seconds=float(worker_arguments.get("timeout_seconds", 3600)))
		study = aggregate_stage9_study(raw_report, output)
		print(json.dumps({"successful": raw_report["status"] == "complete", "raw_report": str((output / "raw" / "stage9a-raw.json").resolve()), "study": str((output / "study.json").resolve()), "status": study["status"]}))
		return 0 if raw_report["status"] == "complete" else 1
	raise SystemExit("Stage 9a raw module requires --run or --worker")


if __name__ == "__main__":
	raise SystemExit(main())
