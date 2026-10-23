import argparse
from datetime import date
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Sequence

from stockguidance.cli.update_arguments import add_update_policy_arguments
from stockguidance.cli.update_setup import assemble_and_update
from stockguidance.cli.update_zscores import assemble_and_update_zscores, parse_windows
from stockguidance.cli.validation_setup import assemble_and_validate
from stockguidance.market_data.history import UnknownHistoryBoundary
from stockguidance.calendar.clock import SystemClock


LOOKBACK_YEARS = 2
ZSCORE_WINDOWS = (20, 60)


def maintenance_range(as_of: date) -> tuple[date, date]:
	try:
		start = as_of.replace(year=as_of.year - LOOKBACK_YEARS)
	except ValueError:
		start = as_of.replace(year=as_of.year - LOOKBACK_YEARS, day=28)
	return start, as_of


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Maintain recent market data and dependent z-score tables")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--dbinfo-file", default="dbinfo.id")
	arguments.add_argument("--windows", type=parse_windows, default=ZSCORE_WINDOWS, help="Z-score windows; defaults to 20,60")
	add_update_policy_arguments(arguments)
	arguments.add_argument("--initialize-zscore-schema", action="store_true", help="Explicitly create missing selected z-score tables")
	arguments.add_argument("--java-manifest", help="Stage 6 production manifest used by the Java derived/z-score writers")
	arguments.add_argument("--java-checkpoint-directory", default="output/daily-maintenance/java", help="Resume/checkpoint directory for Java writers")
	return arguments


def _update_arguments(arguments: argparse.Namespace, start: date, end: date) -> argparse.Namespace:
	return argparse.Namespace(
		data_root=arguments.data_root, dbinfo_file=arguments.dbinfo_file, start_date=start, end_date=end,
		repair_plan=None, max_retries=3, history_start=arguments.history_start, history_evidence=arguments.history_evidence,
		adjustment=arguments.adjustment, publication_delay_minutes=arguments.publication_delay_minutes,
		publication_evidence=arguments.publication_evidence, recent_window_days=arguments.recent_window_days,
		recent_refresh_hours=arguments.recent_refresh_hours, archive_refresh_days=arguments.archive_refresh_days,
		correction_evidence=arguments.correction_evidence, initialize_schema=arguments.initialize_schema,
	)


def _zscore_arguments(arguments: argparse.Namespace, start: date, end: date) -> argparse.Namespace:
	return argparse.Namespace(
		data_root=arguments.data_root, dbinfo_file=arguments.dbinfo_file, adjustment=arguments.adjustment,
		windows=arguments.windows, start_date=start, end_date=end, history_start=arguments.history_start,
		history_evidence=arguments.history_evidence, publication_delay_minutes=arguments.publication_delay_minutes,
		publication_evidence=arguments.publication_evidence, initialize_schema=arguments.initialize_zscore_schema,
	)


def _java_classpath() -> str:
	java_root = Path(__file__).resolve().parents[3] / "java"
	return ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))


def _manifest_sessions(manifest_path: Path, start: date, end: date) -> tuple[str, ...]:
	manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
	sessions = tuple(sorted(
		str(target["session"])
		for target in manifest.get("targets", ())
		if start.isoformat() <= str(target["session"]) <= end.isoformat()
	))
	if not sessions:
		raise ValueError("Java manifest has no targets in the maintenance range")
	return sessions


def _run_java_updates(arguments: argparse.Namespace, start: date, end: date) -> dict[str, Any]:
	if tuple(arguments.windows) != ZSCORE_WINDOWS:
		raise ValueError("The Java daily backend currently writes z-score windows 20,60 only")
	if arguments.initialize_zscore_schema:
		raise ValueError("Initialize z-score tables before selecting the Java daily backend")
	if not arguments.java_manifest:
		raise ValueError("Java daily maintenance requires --java-manifest")
	manifest = Path(arguments.java_manifest).resolve()
	checkpoint = Path(arguments.java_checkpoint_directory).resolve()
	checkpoint.mkdir(parents=True, exist_ok=True)
	sessions = _manifest_sessions(manifest, start, end)
	classpath = _java_classpath()
	commands = (
		["java", "-Xmx512m", "-cp", classpath, "stockguidance.migration.JavaDerivedBenchmarkMain",
		"--manifest", str(manifest), "--dbinfo-file", str(Path(arguments.dbinfo_file).resolve()),
		"--production", "true", "--preparers", "1", "--sessions", ",".join(sessions),
		"--checkpoint-directory", str(checkpoint),
	], ["java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "-cp", classpath,
		"stockguidance.migration.JavaZScoreBenchmarkMain", "--manifest", str(manifest),
		"--dbinfo-file", str(Path(arguments.dbinfo_file).resolve()), "--production", "true",
		"--sessions", ",".join(sessions), "--checkpoint-directory", str(checkpoint),
	])
	reports = []
	for command in commands:
		result = subprocess.run(command, capture_output=True, text=True, check=False)
		try:
			report = json.loads(result.stdout)
		except (TypeError, ValueError, json.JSONDecodeError) as error:
			raise RuntimeError("Java maintenance backend returned invalid JSON") from error
		if result.returncode != 0 or report.get("failed") or report.get("status") != "complete":
			raise RuntimeError("Java maintenance backend failed")
		reports.append(report)
	return {"backend": "java", "sessions": list(sessions), "derived": reports[0], "zscores": reports[1]}


def _validation_arguments(arguments: argparse.Namespace, start: date, end: date) -> argparse.Namespace:
	return argparse.Namespace(
		data_root=arguments.data_root, dbinfo_file=arguments.dbinfo_file, start_date=start.isoformat(), end_date=end.isoformat(),
		history_start=arguments.history_start, history_evidence=arguments.history_evidence, adjustment=arguments.adjustment,
		as_of=None, publication_delay_minutes=arguments.publication_delay_minutes,
		publication_evidence=arguments.publication_evidence, format="json", ticker_ids=None, lookback_sessions=None,
		signal_mode="previous_close", zscore_windows=arguments.windows,
	)


def maintain(arguments: argparse.Namespace, *, update: Callable[[argparse.Namespace], object] = assemble_and_update, update_zscores: Callable[[argparse.Namespace], object] = assemble_and_update_zscores, java_update: Callable[[argparse.Namespace, date, date], dict[str, Any]] = _run_java_updates, validate: Callable[[argparse.Namespace], object] = assemble_and_validate) -> dict[str, object]:
	start, end = maintenance_range(SystemClock().now().date())
	market_result = update(_update_arguments(arguments, start, end))
	market_successful = bool(getattr(market_result, "successful"))
	if not market_successful:
		return {
			"successful": False,
			"lookback_years": LOOKBACK_YEARS,
			"start_date": start.isoformat(),
			"end_date": end.isoformat(),
			"market_data": {
				"successful": False,
				"downloaded_sessions": int(getattr(market_result, "downloaded_sessions")),
				"stale_refreshes": [item.isoformat() for item in getattr(market_result, "stale_refreshes")],
				"failures": [str(item) for item in getattr(market_result, "failures")],
			},
			"java_updates": {"skipped": True, "reason": "market_data_reconciliation_failed"},
		}
	if update_zscores is assemble_and_update_zscores:
		java_result = java_update(arguments, start, end)
		validation_result = validate(_validation_arguments(arguments, start, end))
		validation_exit_code = int(getattr(validation_result, "exit_code"))
		return {
			"successful": market_successful and validation_exit_code == 0,
			"lookback_years": LOOKBACK_YEARS, "start_date": start.isoformat(), "end_date": end.isoformat(),
			"market_data": {"successful": market_successful, "downloaded_sessions": int(getattr(market_result, "downloaded_sessions")), "stale_refreshes": [item.isoformat() for item in getattr(market_result, "stale_refreshes")], "failures": [str(item) for item in getattr(market_result, "failures")]},
			"java_updates": java_result, "python_validation": {"exit_code": validation_exit_code, "complete": bool(getattr(validation_result, "complete"))},
		}
	zscore_result = update_zscores(_zscore_arguments(arguments, start, end))
	return {
		"successful": market_successful and int(getattr(zscore_result, "exit_code")) == 0,
		"lookback_years": LOOKBACK_YEARS,
		"start_date": start.isoformat(),
		"end_date": end.isoformat(),
		"market_data": {
			"successful": market_successful,
			"downloaded_sessions": int(getattr(market_result, "downloaded_sessions")),
			"stale_refreshes": [item.isoformat() for item in getattr(market_result, "stale_refreshes")],
			"failures": [str(item) for item in getattr(market_result, "failures")],
		},
		"zscores": {
			"exit_code": int(getattr(zscore_result, "exit_code")),
			"complete": bool(getattr(zscore_result, "complete")),
			"written_sessions": [item.session_date.isoformat() for item in getattr(zscore_result, "written_sessions")],
			"rebuilt_results": [
				{"family": item.family.value, "window": item.window, "date": item.session.session_date.isoformat()}
				for item in getattr(zscore_result, "rebuilt_results")
			],
			"unavailable_count": int(getattr(zscore_result, "unavailable_count")),
			"failures": list(getattr(zscore_result, "failures")),
		},
	}


def main(argv: Sequence[str] | None = None, *, run: Callable[[argparse.Namespace], dict[str, object]] = maintain) -> int:
	arguments = parser().parse_args(argv)
	try:
		payload = run(arguments)
	except UnknownHistoryBoundary as error:
		print(json.dumps({"successful": False, "error": str(error)}, sort_keys=True))
		return 2
	except ValueError:
		print(json.dumps({"successful": False, "error": "Invalid daily maintenance configuration or supported-history metadata"}, sort_keys=True))
		return 2
	except Exception:
		print(json.dumps({"successful": False, "error": "Daily market-data maintenance failed"}, sort_keys=True))
		return 1
	print(json.dumps(payload, sort_keys=True))
	return 0 if payload["successful"] else 2


if __name__ == "__main__":
	raise SystemExit(main())
