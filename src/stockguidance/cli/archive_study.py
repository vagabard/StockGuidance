import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter, sleep
from typing import Any

from stockguidance.cli.compare_database_downloads import _classpath
from stockguidance.cli.paths import resolve_cli_path
from stockguidance.performance.archive_read import inventory, run_request


def render_summary(report: dict[str, Any]) -> str:
	lines = ["# Retained archive read benchmark", "", f"Completed selected read workloads: {report.get('complete', False)}; canonical result parity: {report.get('parity')}", "", "| Mode | Backend | Workers | First task (s) | Warm median (s) | Warm rows/s | Process total (s) | Peak tree RSS (MiB) |", "|---|---|---:|---:|---:|---:|---:|---:|"]
	for run in report["runs"]:
		memory = run.get("peak_tree_rss_bytes")
		memory_text = "unavailable" if memory is None else f"{memory / 1024**2:.1f}"
		lines.append(f"| {run['mode']} | {run['backend']} | {run['workers']} | {run['first_seconds']:.3f} | {run['warm_median_seconds']:.3f} | {run.get('warm_rows_per_second', 0):,.0f} | {run['process_seconds']:.3f} | {memory_text} |")
	lines.extend(["", "Java uses application threads; Python uses worker processes. Process total includes startup and all four task repetitions, not one task's cold latency. The first task is not guaranteed OS-cache cold. Memory is sampled process-tree RSS, not heap allocation.", "", "Derived computation, write performance, original historical availability and full-archive database agreement are outside this read study.", ""])
	for mode in sorted({run["mode"] for run in report["runs"]}):
		group = [run for run in report["runs"] if run["mode"] == mode]
		for backend in ("java", "python"):
			by_workers = {run["workers"]: run for run in group if run["backend"] == backend}
			if 1 in by_workers and 3 in by_workers:
				lines.append(f"- {mode}, {backend}: three-worker speedup {by_workers[1]['warm_median_seconds'] / by_workers[3]['warm_median_seconds']:.2f}x.")
	return "\n".join(lines) + "\n"


def result_parity(runs: list[dict[str, Any]]) -> bool:
	if not runs:
		return False
	known: dict[tuple[str, str], Any] = {}
	for mode in {run["mode"] for run in runs}:
		if len({measurement["digest"] for run in runs if run["mode"] == mode for measurement in run["result"]["measurements"]}) != 1:
			return False
	for run in runs:
		for measurement in run["result"]["measurements"]:
			for session in measurement["sessions"]:
				for name in ("file_sha256", "archive_id", "archive_count", "database_id", "database_count"):
					if name not in session:
						continue
					key = session["session"], name
					if key in known and known[key] != session[name]:
						return False
					known[key] = session[name]
	return True


def _hash_file(path: Path) -> str:
	digest = sha256()
	with path.open("rb") as stream:
		while chunk := stream.read(1024 * 1024):
			digest.update(chunk)
	return digest.hexdigest()


def _load_stage2a_selection(path: Path) -> tuple[int, list[Path]]:
	try:
		report = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError):
		raise ValueError("Cannot read the Stage 2a report") from None
	if report.get("schema_version") != 1 or report.get("seed") != 20261019 or not report.get("complete") or not report.get("parity"):
		raise ValueError("Stage 2a report is incomplete or lacks canonical parity")
	selected = report.get("selected_paths")
	if not isinstance(selected, list) or not selected or any(not isinstance(item, str) for item in selected):
		raise ValueError("Stage 2a report has no explicit selected paths")
	return int(report["seed"]), [Path(item).resolve() for item in selected]


def _derived_neighbors(paths: tuple[Path, ...], selected: list[Path]) -> dict[str, list[str]]:
	by_path = {path.resolve(): index for index, path in enumerate(paths)}
	neighbors: dict[str, list[str]] = {}
	for path in selected:
		resolved = path.resolve()
		index = by_path.get(resolved)
		if index is None or index == 0 or index == len(paths) - 1:
			raise ValueError("Selected Stage 2a session lacks retained archive neighbors")
		triple = (paths[index - 1], paths[index], paths[index + 1])
		neighbors[str(resolved)] = [str(item.resolve()) for item in triple]
	return neighbors


def render_derived_summary(report: dict[str, Any]) -> str:
	lines = [
		"# Stage 2b derived-computation study",
		"",
		f"Completed: {report.get('complete', False)}; canonical parity: {report.get('parity')}",
		"",
		"| Backend | Workers | First task (s) | Warm median (s) | Warm rows/s | Process total (s) | Peak tree RSS (MiB) | Warm queries |",
		"|---|---:|---:|---:|---:|---:|---:|---:|",
	]
	for run in report.get("runs", []):
		memory = run.get("peak_tree_rss_bytes")
		memory_text = "unavailable" if memory is None else f"{memory / 1024**2:.1f}"
		lines.append(
			f"| {run['backend']} | {run['workers']} | {run['first_seconds']:.3f} | "
			f"{run['warm_median_seconds']:.3f} | {run['warm_rows_per_second']:,.0f} | "
			f"{run['process_seconds']:.3f} | {memory_text} | {run['warm_query_count']} |"
		)
	lines.extend(
		[
			"",
			"The timed target is the database-compatible integer-volume contract. Archive files identify exact selected and neighboring sessions and are not converted into the current DailyBar contract.",
			"Each worker uses a separate read-only repeatable-read transaction; this is not a shared snapshot. Process totals include startup and all four repetitions; the first task is not guaranteed OS-cache cold.",
			"",
			f"Complete central sessions: {report.get('complete_session_count', 0)}; unavailable central sessions: {report.get('unavailable_session_count', 0)}.",
		]
	)
	return "\n".join(lines) + "\n"


def derived_study(arguments: argparse.Namespace) -> dict[str, Any]:
	stage2a_report = Path(arguments.study_report).resolve()
	seed, selected = _load_stage2a_selection(stage2a_report)
	root = resolve_cli_path(arguments.data_root, "data")
	paths = inventory(root)
	selected_set = {path.resolve() for path in selected}
	if len(selected_set) != len(selected) or not selected_set.issubset({path.resolve() for path in paths}):
		raise ValueError("Stage 2a selected paths are not the retained inventory")
	selected = [path.resolve() for path in selected]
	neighbors = _derived_neighbors(paths, selected)
	context_paths = sorted({item for triple in neighbors.values() for item in triple})
	input_hashes = {path: _hash_file(Path(path)) for path in context_paths}
	output = Path(arguments.output).resolve()
	output.mkdir(parents=True, exist_ok=True)
	environment = dict(os.environ)
	for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
		environment[name] = "1"
	runs: list[dict[str, Any]] = []
	report: dict[str, Any] = {
		"schema_version": 1,
		"study": "stage2b-derived-v1",
		"created_at": datetime.now(timezone.utc).isoformat(),
		"seed": seed,
		"stage2a_report": str(stage2a_report),
		"stage2a_report_sha256": _hash_file(stage2a_report),
		"selected_paths": [str(path) for path in selected],
		"neighbors": neighbors,
		"context_paths": context_paths,
		"context_file_sha256": input_hashes,
		"central_session_count": len(selected),
		"complete_session_count": None,
		"unavailable_session_count": None,
		"complete": False,
		"parity": None,
		"snapshot_mode": "per_worker_repeatable_read",
		"shared_snapshot": False,
		"database_idle_required": True,
		"runs": runs,
		"limitations": [
			"The database must remain idle during the study; worker transactions are not a shared snapshot.",
			"Archive fractional volumes are outside the current DailyBar/BIGINT persistence contract and are not timed as derived inputs.",
			"First-run cache state is unknown; process totals include startup, setup, and all four repetitions.",
			"No production writes, receipts, schema changes, or writer benchmark are included.",
		],
	}
	report_path = output / "study.json"
	report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
	for order_index, workers in enumerate((1, 3)):
		request = {
			"paths": [str(path) for path in selected],
			"neighbors": neighbors,
			"mode": "derived",
			"workers": workers,
			"repeats": 4,
			"dbinfo_file": str(resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")),
		}
		request_file = output / f"request-derived-{workers}.json"
		request_file.write_text(json.dumps(request), encoding="utf-8")
		backends = ("java", "python") if order_index == 0 else ("python", "java")
		for backend in backends:
			print(f"Starting {backend} derived with {workers} worker(s)", file=sys.stderr, flush=True)
			command = [sys.executable, "-m", "stockguidance.cli.archive_study", "--execute-request", str(request_file)] if backend == "python" else [arguments.java_executable, "-Xmx512m", "-XX:ActiveProcessorCount=3", "-XX:+UseSerialGC", "-cp", _classpath(arguments.java_classpath), "stockguidance.validation.ArchiveStudyMain", str(request_file)]
			try:
				run = execute(command, environment, arguments.timeout_seconds)
			except (Exception, KeyboardInterrupt):
				report["failed_run"] = {"backend": backend, "workers": workers}
				report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
				raise
			run.update(backend=backend, workers=workers, mode="derived")
			measurements = run["result"]["measurements"]
			run["first_seconds"] = measurements[0]["seconds"]
			run["warm_median_seconds"] = statistics.median(item["seconds"] for item in measurements[1:])
			run["rows_per_repeat"] = measurements[0]["raw_rows"] + measurements[0]["derived_rows"]
			run["warm_rows_per_second"] = run["rows_per_repeat"] / run["warm_median_seconds"]
			run["warm_query_count"] = statistics.median(item["query_count"] for item in measurements[1:])
			runs.append(run)
			(output / f"{backend}-derived-{workers}.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
			report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
			print(f"Completed {backend} derived with {workers} worker(s)", file=sys.stderr, flush=True)
	latest_hashes = {path: _hash_file(Path(path)) for path in context_paths}
	if latest_hashes != input_hashes:
		report["input_changed"] = True
		report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
		raise ValueError("Derived study input hashes changed during execution")
	all_sessions = [session for run in runs for measurement in run["result"]["measurements"][:1] for session in measurement["sessions"]]
	report["complete_session_count"] = sum(session["state"] == "complete" for session in all_sessions[: len(selected)])
	report["unavailable_session_count"] = len(selected) - report["complete_session_count"]
	report["complete"] = report["unavailable_session_count"] == 0
	report["parity"] = result_parity(runs)
	report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
	(output / "summary.md").write_text(render_derived_summary(report), encoding="utf-8")
	return report


def stratified_sample(paths: tuple[Path, ...], count: int, seed: int) -> list[Path]:
	if count < 3 or count % 3 or len(paths) < count:
		raise ValueError("Sample count must be a multiple of three within archive size")
	generator = random.Random(seed)
	selected = []
	for third in range(3):
		partition = paths[len(paths) * third // 3:len(paths) * (third + 1) // 3]
		selected.extend(generator.sample(list(partition), count // 3))
	return sorted(selected)


def execute(command: list[str], environment: dict[str, str], timeout: float) -> dict[str, Any]:
	if not math.isfinite(timeout) or timeout <= 0:
		raise ValueError("Study timeout must be finite and positive")
	try:
		import psutil
	except ImportError:
		psutil = None
	started = perf_counter()
	peak = 0
	peak_threads = 0
	last_update = started
	with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output, tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors, subprocess.Popen(command, stdout=output, stderr=errors, env=environment, text=True) as process:
		try:
			while process.poll() is None:
				if perf_counter() - last_update >= 30:
					print(f"Study child still running ({perf_counter() - started:.0f}s)", file=sys.stderr, flush=True)
					last_update = perf_counter()
				if psutil is not None:
					try:
						root = psutil.Process(process.pid)
						tree = [root, *root.children(recursive=True)]
						peak = max(peak, sum(item.memory_info().rss for item in tree))
						peak_threads = max(peak_threads, sum(item.num_threads() for item in tree))
					except psutil.Error:
						pass
				if perf_counter() - started > timeout:
					raise TimeoutError("Study child exceeded timeout")
				sleep(0.05)
			process.wait()
			output.seek(0)
			stdout = output.read()
		except BaseException:
			if psutil is not None:
				try:
					for child in psutil.Process(process.pid).children(recursive=True):
						child.kill()
				except psutil.Error:
					pass
			process.kill()
			process.wait()
			raise
	if process.returncode != 0:
		raise RuntimeError("Study child failed; no database writes were attempted")
	return {"process_seconds": perf_counter() - started, "peak_tree_rss_bytes": peak or None, "peak_tree_os_threads": peak_threads or None, "result": json.loads(stdout)}


def study(arguments: argparse.Namespace) -> dict[str, Any]:
	root = resolve_cli_path(arguments.data_root, "data")
	paths = inventory(root)
	selected = stratified_sample(paths, arguments.sample_count, arguments.seed)
	largest = max(paths, key=lambda path: path.stat().st_size)
	if largest not in selected:
		selected.append(largest)
	selected.sort()
	output = Path(arguments.output).resolve()
	output.mkdir(parents=True, exist_ok=True)
	environment = dict(os.environ)
	for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
		environment[name] = "1"
	modes = ("files",) if arguments.files_only else tuple(arguments.modes)
	runs: list[dict[str, Any]] = []
	report: dict[str, Any] = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "seed": arguments.seed, "archive_file_count": len(paths), "selected_paths": [str(path) for path in selected], "largest_file": str(largest), "complete": False, "parity": None, "runs": runs, "limitations": ["First invocation is not an OS-cache-cold measurement.", "Each worker uses its own read-only transaction; database must remain idle for cross-worker consistency.", "RSS sampling includes runtime and child processes; OS thread counts include housekeeping.", "No derived-computation or write benchmark is included; write-backend selection remains pending.", "All repeats must have identical digests before speed results are usable."]}
	report_path = output / "study.json"
	report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
	for mode_index, mode in enumerate(modes):
		for workers in (1, 3):
			request = {"paths": [str(path.resolve()) for path in selected], "mode": mode, "workers": workers, "repeats": 4, "dbinfo_file": str(resolve_cli_path(arguments.dbinfo_file, "dbinfo.id"))}
			request_file = output / f"request-{mode}-{workers}.json"
			request_file.write_text(json.dumps(request), encoding="utf-8")
			for backend in (("python", "java") if mode_index % 2 == 0 else ("java", "python")):
				print(f"Starting {backend} {mode} with {workers} worker(s)", file=sys.stderr, flush=True)
				command = [sys.executable, "-m", "stockguidance.cli.archive_study", "--execute-request", str(request_file)] if backend == "python" else [arguments.java_executable, "-Xmx512m", "-XX:ActiveProcessorCount=3", "-XX:+UseSerialGC", "-cp", _classpath(arguments.java_classpath), "stockguidance.validation.ArchiveStudyMain", str(request_file)]
				try:
					run = execute(command, environment, arguments.timeout_seconds)
				except (Exception, KeyboardInterrupt):
					report["failed_run"] = {"backend": backend, "mode": mode, "workers": workers}
					report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
					raise
				run.update(backend=backend, workers=workers, mode=mode)
				measurements = run["result"]["measurements"]
				run["first_seconds"] = measurements[0]["seconds"]
				run["warm_median_seconds"] = statistics.median(item["seconds"] for item in measurements[1:])
				run["rows_per_repeat"] = sum(item.get("archive_count", item.get("database_count", 0)) for item in measurements[0]["sessions"])
				run["warm_rows_per_second"] = run["rows_per_repeat"] / run["warm_median_seconds"]
				runs.append(run)
				(output / f"{backend}-{mode}-{workers}.json").write_text(json.dumps(run, indent=2), encoding="utf-8")
				report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
				print(f"Completed {backend} {mode} with {workers} worker(s)", file=sys.stderr, flush=True)
	parity = result_parity(runs)
	report.update(complete=True, parity=parity)
	report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
	(output / "summary.md").write_text(render_summary(report), encoding="utf-8")
	return report


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Bounded read-only Java-thread/Python-process archive study")
	parser.add_argument("--execute-request", type=Path)
	parser.add_argument("--data-root", default="data")
	parser.add_argument("--dbinfo-file", default="dbinfo.id")
	parser.add_argument("--output", default="output/legacy-migration/benchmark")
	parser.add_argument("--sample-count", type=int, default=24)
	parser.add_argument("--seed", type=int, default=20261019)
	parser.add_argument("--files-only", action="store_true")
	parser.add_argument("--modes", nargs="+", choices=("files", "database", "reconcile"), default=("files", "database", "reconcile"))
	parser.add_argument("--derived-only", action="store_true")
	parser.add_argument("--study-report", default="output/legacy-migration/benchmark/study.json")
	parser.add_argument("--java-executable", default="java")
	parser.add_argument("--java-classpath")
	parser.add_argument("--timeout-seconds", type=float, default=300)
	arguments = parser.parse_args(argv)
	try:
		if arguments.execute_request is not None:
			print(json.dumps(run_request(json.loads(arguments.execute_request.read_text(encoding="utf-8")))))
			return 0
		if arguments.derived_only:
			report = derived_study(arguments)
			print(json.dumps({"parity": report["parity"], "complete": report["complete"], "report": str(Path(arguments.output) / "study.json")}))
			return 0 if report["complete"] and report["parity"] else 2
		report = study(arguments)
		print(json.dumps({"parity": report["parity"], "report": str(Path(arguments.output) / "study.json")}))
		return 0 if report["parity"] else 2
	except (Exception, KeyboardInterrupt):
		print(json.dumps({"failed": True, "error": "Archive study failed; inspect saved completed runs"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
