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
from stockguidance.workflows.daily_maintenance_source import SourceStageResult


@dataclass
class JavaStageResult:
	reports: list[dict[str, Any]]
	validations: list[dict[str, Any]]


def prepare_partial_imports(cursor: Any, connection: Any, names: TableNames) -> int:
	progress = quote_identifier("stockguidance_daily_import_in_progress")
	cursor.execute(f"CREATE TABLE IF NOT EXISTS {progress} (session_date DATE NOT NULL PRIMARY KEY, started_at VARCHAR(40) NOT NULL) ENGINE=InnoDB", ())
	cursor.execute(f"SELECT session_date FROM {progress} ORDER BY session_date", ())
	partial_dates = tuple(str(row[0]) for row in cursor.fetchall())
	for session in partial_dates:
		cursor.execute(f"DELETE p FROM {quote_identifier(names.prices)} p JOIN {quote_identifier(names.sessions)} s ON s.id = p.date_id WHERE s.transaction_date = %s", (session,))
		cursor.execute(f"DELETE d FROM {quote_identifier(names.deltas)} d JOIN {quote_identifier(names.sessions)} s ON s.id = d.date_id WHERE s.transaction_date = %s", (session,))
		cursor.execute(f"DELETE FROM {quote_identifier(names.imports)} WHERE session_date = %s", (session,))
		cursor.execute("DELETE FROM `stockguidance_migration_completion` WHERE workload = 'raw' AND session_date = %s AND series_key = 'raw'", (session,))
		cursor.execute(f"DELETE FROM {progress} WHERE session_date = %s", (session,))
	connection.commit()
	return len(partial_dates)


def build_workload(source: SourceConfig, names: TableNames, calendar: SessionCalendar, raw_sessions: tuple[TradingSession, ...], targets: tuple[TradingSession, ...], files_by_date: Mapping[Any, dict[str, Any]], availability: Mapping[Any, str], raw_sessions_to_import: tuple[TradingSession, ...] | None = None) -> dict[str, Any]:
	files = [files_by_date[session.session_date] for session in raw_sessions]
	available_dates = {session.session_date for session in raw_sessions}
	derived_end = calendar.shift(targets[-1], 2).session_date
	derived = tuple(session for session in raw_sessions[1:] if session.session_date <= derived_end)
	target_entries = []
	for session in derived:
		prior, following = calendar.shift(session, -1), calendar.shift(session, 1)
		for neighbor in (prior, following):
			if neighbor.session_date not in available_dates:
				files.append({"session": neighbor.session_date.isoformat(), "path": None, "valid": False,
					"file_sha256": None, "records_id": None})
		dependencies = calendar.sessions(calendar.shift(session, -60).session_date, session.session_date) if session in targets else ()
		target_entries.append({"session": session.session_date.isoformat(), "derived_dependencies": [prior.session_date.isoformat(), following.session_date.isoformat()],
			"zscore_dependencies": [item.session_date.isoformat() for item in dependencies]})
	root = {"schema_version": 1, "kind": "daily-maintenance", "data_root": str(source.data_root.resolve()), "calendar_id": calendar.calendar_id,
		"source_key": data_configuration_key(source.data_configuration), "source": {"archive_source": asdict(source.identity)},
		"authoritative_registry": {"schema_version": 1, "tables": names.tables(), "zscore_templates": names.templates()},
		"files": files, "targets": target_entries, "mappings": {"ticker_mappings": []},
		"availability": {session.session_date.isoformat(): availability[session.session_date] for session in raw_sessions},
		"raw_sessions": [session.session_date.isoformat() for session in (raw_sessions if raw_sessions_to_import is None else raw_sessions_to_import)],
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
	checkpoint = Path(arguments.java_checkpoint_directory).resolve()
	checkpoint.mkdir(parents=True, exist_ok=True)
	reports: list[dict[str, Any]] = []
	validations: list[dict[str, Any]] = []
	java_started = monotonic()
	for offset in range(0, len(context.targets), arguments.batch_sessions):
		maintenance.assert_lock()
		batch = context.targets[offset:offset + arguments.batch_sessions]
		first = context.calendar.shift(batch[0], -61).session_date
		last = min(context.calendar.shift(batch[-1], 3).session_date, context.raw_sessions[-1].session_date)
		inputs = tuple(session for session in context.raw_sessions if first <= session.session_date <= last)
		batch_raw_dates = tuple(session for session in inputs if session.session_date not in maintenance.imports or maintenance.imports[session.session_date][2] == 0 or session.session_date in source_result.refreshed_dates)
		workload = build_workload(context.source, context.names, context.calendar, inputs, batch, source_result.files_by_date, source_result.availability, batch_raw_dates)
		path = checkpoint / f"workload-{workload['manifest_id']}.json"
		atomic_write(path, encode_json(workload))
		report = execute_java(arguments, command, path, checkpoint)
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
	return JavaStageResult(reports, validations)
