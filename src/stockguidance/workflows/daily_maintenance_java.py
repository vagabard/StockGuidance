import argparse
from collections.abc import Mapping
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic
from typing import Any, Callable

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, data_configuration_key, encode_json
from stockguidance.config.source import SourceConfig
from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.workflows.daily_maintenance_context import MaintenanceSession
from stockguidance.workflows.daily_maintenance_artifacts import create_run_directory, cleanup_successful_run
from stockguidance.workflows.daily_maintenance_source import SourceStageResult


@dataclass
class JavaStageResult:
	reports: list[dict[str, Any]]
	validations: list[dict[str, Any]]


def prepare_partial_imports(cursor: Any, connection: Any, names: TableNames, sessions: tuple[TradingSession, ...] | None = None) -> int:
	progress = quote_identifier("stockguidance_daily_import_in_progress")
	cursor.execute(f"SELECT session_date FROM {progress} ORDER BY session_date", ())
	allowed = None if sessions is None else {session.session_date.isoformat() for session in sessions}
	partial_dates = tuple(str(row[0]) for row in cursor.fetchall() if allowed is None or str(row[0]) in allowed)
	for session in partial_dates:
		cursor.execute(f"DELETE p FROM {quote_identifier(names.prices)} p JOIN {quote_identifier(names.sessions)} s ON s.id = p.date_id WHERE s.transaction_date = %s", (session,))
		cursor.execute(f"DELETE d FROM {quote_identifier(names.deltas)} d JOIN {quote_identifier(names.sessions)} s ON s.id = d.date_id WHERE s.transaction_date = %s", (session,))
		cursor.execute(f"DELETE FROM {quote_identifier(names.imports)} WHERE session_date = %s", (session,))
		cursor.execute("DELETE FROM `stockguidance_migration_completion` WHERE workload = 'raw' AND session_date = %s AND series_key = 'raw'", (session,))
		cursor.execute(f"DELETE FROM {progress} WHERE session_date = %s", (session,))
	connection.commit()
	return len(partial_dates)


def build_workload(source: SourceConfig, names: TableNames, calendar: SessionCalendar, raw_sessions: tuple[TradingSession, ...], targets: tuple[TradingSession, ...], availability: Mapping[Any, str], refreshed_sessions: tuple[TradingSession, ...] = ()) -> dict[str, Any]:
	derived_end = calendar.shift(targets[-1], 2).session_date
	derived = tuple(session for session in raw_sessions[1:] if session.session_date <= derived_end)
	target_entries = []
	for session in derived:
		prior, following = calendar.shift(session, -1), calendar.shift(session, 1)
		dependencies = calendar.sessions(calendar.shift(session, -60).session_date, session.session_date) if session in targets else ()
		target_entries.append({"session": session.session_date.isoformat(), "derived_dependencies": [prior.session_date.isoformat(), following.session_date.isoformat()],
			"zscore_dependencies": [item.session_date.isoformat() for item in dependencies]})
	root = {"schema_version": 2, "kind": "daily-maintenance", "input_mode": "database", "data_root": str(source.data_root.resolve()), "calendar_id": calendar.calendar_id,
		"archive_directory": str((source.data_root / "market_summary").resolve()), "archive_layout": "year/date",
		"source_key": data_configuration_key(source.data_configuration), "source": {"archive_source": asdict(source.identity)},
		"authoritative_registry": {"schema_version": 1, "tables": names.tables(), "zscore_templates": names.templates()},
		"input_sessions": [session.session_date.isoformat() for session in raw_sessions],
		"session_sequences": {session.session_date.isoformat(): session.sequence for session in raw_sessions},
		"refreshed_sessions": [session.session_date.isoformat() for session in refreshed_sessions],
		"targets": target_entries, "mappings": {"ticker_mappings": []},
		"availability": {session.session_date.isoformat(): availability[session.session_date] for session in raw_sessions},
		"raw_sessions": [session.session_date.isoformat() for session in raw_sessions],
		"derived_sessions": [session.session_date.isoformat() for session in derived],
		"zscore_sessions": [session.session_date.isoformat() for session in targets]}
	root["manifest_id"] = content_id(root)
	return root


def java_preflight(arguments: argparse.Namespace) -> list[str]:
	if not re.fullmatch(r"[1-9][0-9]*[mg]", arguments.java_heap) or not 1 <= arguments.workers <= 128:
		raise ValueError("Java heap must be an integer followed by m or g; workers must be between 1 and 128")
	if not 1 <= arguments.batch_sessions <= 100 or not math.isfinite(arguments.timeout_seconds) or arguments.timeout_seconds <= 0:
		raise ValueError("Batch sessions must be between 1 and 100 and timeout must be positive")
	root = Path(__file__).resolve().parents[3] / "java"
	if not (root / "target/classes/stockguidance/migration/JavaDailyMaintenanceMain.class").exists():
		raise ValueError("Build the operational Java backend first: mvn -f java/pom.xml package -DskipTests")
	classpath = os.pathsep.join(str(root / path) for path in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))
	return ["java", "--add-modules=jdk.incubator.vector", f"-Xmx{arguments.java_heap}", "-cp", classpath,
		"stockguidance.migration.JavaDailyMaintenanceMain"]


def execute_java(arguments: argparse.Namespace, command: list[str], workload: Path, checkpoint: Path) -> dict[str, Any]:
	print("executing java command ",command," ","--workload", str(workload), "--dbinfo-file", str(Path(arguments.dbinfo_file).resolve()), "--checkpoint-directory", str(checkpoint), "--workers", str(arguments.workers), "--writers", "1")
	result = subprocess.run([*command, "--workload", str(workload), "--dbinfo-file", str(Path(arguments.dbinfo_file).resolve()),
		"--checkpoint-directory", str(checkpoint), "--workers", str(arguments.workers), "--writers", "1"],
		stdout=subprocess.PIPE, text=True, encoding="utf-8", timeout=arguments.timeout_seconds, check=False)
	try:
		report = json.loads(result.stdout)
	except ValueError as error:
		raise RuntimeError("Java maintenance returned invalid JSON; see console diagnostics") from error
	if not isinstance(report, dict) or result.returncode or report.get("failed") or report.get("status") != "complete":
		raise RuntimeError("Java maintenance failed; committed sessions can be resumed by rerunning")
	return report


def run_java_stage(
	maintenance: MaintenanceSession,
	command: list[str],
	source_result: SourceStageResult,
	*,
	validate_batch: Callable[[MaintenanceSession, dict[str, Any], dict[str, Any], Path], dict[str, Any]] | None = None,
) -> JavaStageResult:
	context = maintenance.context
	arguments = context.arguments
	checkpoint = create_run_directory(maintenance, "java")
	reports: list[dict[str, Any]] = []
	validations: list[dict[str, Any]] = []
	java_started = monotonic()
	processed: set[Any] = set()
	for offset in range(0, len(context.targets), arguments.batch_sessions):
		maintenance.assert_lock()
		batch = context.targets[offset:offset + arguments.batch_sessions]
		first = context.calendar.shift(batch[0], -61).session_date
		last = min(context.calendar.shift(batch[-1], 3).session_date, context.raw_sessions[-1].session_date)
		inputs = tuple(session for session in context.raw_sessions if first <= session.session_date <= last)
		batch_raw_dates = tuple(session for session in inputs if session.session_date in source_result.refreshed_dates and session.session_date not in processed)
		workload = build_workload(context.source, context.names, context.calendar, inputs, batch, source_result.availability, batch_raw_dates)
		workload["quality_history"] = getattr(arguments, "quality_history", "clean")
		workload["retrospective_quality"] = getattr(arguments, "retrospective_quality", False)
		workload["scan_quality"] = getattr(arguments, "scan_quality", False)
		workload.pop("manifest_id", None)
		workload["manifest_id"] = content_id(workload)
		path = checkpoint / f"workload-{workload['manifest_id']}.json"
		atomic_write(path, encode_json(workload))
		report = execute_java(arguments, command, path, checkpoint)
		processed.update(session.session_date for session in inputs)
		reports.append(report)
		atomic_write(checkpoint / f"report-{workload['manifest_id']}.json", encode_json(report))
		if validate_batch is None:
			continue
		validation_report = validate_batch(maintenance, workload, report, checkpoint)
		validations.append(validation_report)
		if not validation_report["complete"]:
			break
		completed = min(offset + len(batch), len(context.targets))
		remaining = (monotonic() - java_started) / completed * (len(context.targets) - completed)
		print(f"Updated and validated {completed}/{len(context.targets)} sessions; estimated {remaining:.0f}s remaining", file=sys.stderr, flush=True)
	if all(item["complete"] for item in validations):
		cleanup_successful_run(checkpoint)
	return JavaStageResult(reports, validations)
