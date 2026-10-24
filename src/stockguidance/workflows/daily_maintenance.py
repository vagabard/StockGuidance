import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic
from typing import Any, Mapping
from uuid import uuid4

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.paths import resolve_cli_path
from stockguidance.config.source import DEFAULT_CALENDAR_ID, DataConfiguration, PriceAdjustment, SourceConfig
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.database.update_schema import assert_source, assert_transactional_tables, assert_zscore_tables
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.identity import content_id, data_configuration_key, encode_json
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore, SourceManifest
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.records import bar_payload, decode_bar, records_id


LOOKBACK_YEARS = 2


def maintenance_range(as_of: date) -> tuple[date, date]:
	try:
		start = as_of.replace(year=as_of.year - LOOKBACK_YEARS)
	except ValueError:
		start = as_of.replace(year=as_of.year - LOOKBACK_YEARS, day=28)
	return start, as_of


def session_workload(calendar: SessionCalendar, start: date, end: date, cutoff: datetime, publication: DailyPublicationPolicy) -> tuple[tuple[TradingSession, ...], tuple[TradingSession, ...]]:
	published = tuple(session for session in calendar.sessions(start, end)
		if (available := publication.expected_at(session)) is not None and available <= cutoff and session.closes_at <= cutoff)
	if not published:
		raise ValueError("The maintenance range contains no published exchange sessions")
	first = calendar.shift(published[0], -61)
	last = published[-1]
	for distance in (1, 2, 3):
		following = calendar.shift(published[-1], distance)
		available = publication.expected_at(following)
		if available is not None and available <= cutoff and following.closes_at <= cutoff:
			last = following
	return tuple(calendar.sessions(first.session_date, last.session_date)), published


def export_archive(source: SourceConfig, manifest: SourceManifest) -> dict[str, Any]:
	download = manifest.download
	timestamps = dict(download.response.source_timestamps)
	rows = []
	for bar in download.response.records:
		if bar.ticker not in timestamps:
			raise ValueError(f"Source timestamp is missing for {download.session.session_date}: {bar.ticker}")
		rows.append(encode_json({**bar_payload(bar), "t": timestamps[bar.ticker]}))
	content = b"".join(rows)
	path = source.data_root / "daily-maintenance" / data_configuration_key(source.data_configuration) / f"{download.session.session_date}.ndjson"
	if not path.exists() or path.read_bytes() != content:
		atomic_write(path, content)
	return {"session": download.session.session_date.isoformat(), "path": str(path.resolve()), "valid": True,
		"file_sha256": sha256(content).hexdigest(), "records_id": records_id(download.response.records)}


def _archive_entry(source: SourceConfig, session: TradingSession, raw_id: str | None, available_at: str) -> dict[str, Any] | None:
	path = source.data_root / "daily-maintenance" / data_configuration_key(source.data_configuration) / f"{session.session_date}.ndjson"
	if not path.is_file():
		return None
	return {"session": session.session_date.isoformat(), "path": str(path.resolve()), "valid": True,
		"file_sha256": None, "records_id": raw_id, "available_at": available_at}


def archive_identity(path: Path) -> tuple[str, str]:
	content = path.read_bytes()
	records = []
	for line in content.splitlines():
		row = json.loads(line)
		if not isinstance(row, dict):
			raise ValueError(f"Daily-maintenance archive record is not an object: {path}")
		records.append(decode_bar(row))
	if not records or len({record.ticker for record in records}) != len(records):
		raise ValueError(f"Daily-maintenance archive is empty or contains duplicate tickers: {path}")
	return sha256(content).hexdigest(), records_id(tuple(records))


def _raw_import_state(cursor: Any, names: TableNames, sessions: tuple[TradingSession, ...]) -> dict[date, tuple[str, str, int]]:
	if not sessions:
		return {}
	identifiers = tuple(session.session_date.isoformat() for session in sessions)
	placeholders = ",".join("%s" for _ in identifiers)
	cursor.execute(f"SELECT s.transaction_date, i.raw_id, i.available_at, COUNT(p.ticker_id) FROM {quote_identifier(names.sessions)} s LEFT JOIN {quote_identifier(names.imports)} i ON i.session_date = s.transaction_date LEFT JOIN {quote_identifier(names.prices)} p ON p.date_id = s.id WHERE s.transaction_date IN ({placeholders}) GROUP BY s.transaction_date, i.raw_id, i.available_at", identifiers)
	return {date.fromisoformat(str(row[0])): (str(row[1]), str(row[2]), int(row[3])) for row in cursor.fetchall() if row[1] is not None}


def _prepare_partial_imports(cursor: Any, connection: Any, names: TableNames) -> int:
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


def build_workload(source: SourceConfig, names: TableNames, calendar: SessionCalendar, raw_sessions: tuple[TradingSession, ...], targets: tuple[TradingSession, ...], files_by_date: Mapping[date, dict[str, Any]], availability: Mapping[date, str], raw_sessions_to_import: tuple[TradingSession, ...] | None = None) -> dict[str, Any]:
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


def validate_materialized_zscores(source: SourceConfig, names: TableNames, workload: dict[str, Any], report: dict[str, Any], directory: Path) -> dict[str, Any]:
	from stockguidance.performance.independent_zscore_validation import validate
	selected = set(workload["zscore_sessions"])
	manifest = {**workload, "targets": [target for target in workload["targets"] if target["session"] in selected]}
	connection = MySQLConnectionFactory()(source.database_config)
	try:
		with redirect_stdout(sys.stderr):
			result = validate(connection, names, manifest, directory, report["zscores"]["series"],
				{"workload": workload["manifest_id"], "snapshot": uuid4().hex})
		metadata = result["metadata"]
		result["successful"] = result["status"] == "complete" and not result["mismatch_count"] and not metadata["series_findings"] and not metadata["metadata_findings"]
		return result
	finally:
		connection.rollback()
		connection.close()


def run_daily_maintenance(arguments: argparse.Namespace) -> dict[str, object]:
	stage = getattr(arguments, "stage", None)
	run_source = stage in (None, "source")
	run_java = stage in (None, "java")
	run_validation = stage in (None, "validation")
	command = java_preflight(arguments) if run_java else None
	clock = SystemClock()
	now = clock.now()
	default_start, default_end = maintenance_range(now.date())
	start, end = arguments.start_date or default_start, arguments.end_date or default_end
	if start > end or start < date(2006, 11, 16):
		raise ValueError("Maintenance bounds must be ordered and start on or after 2006-11-16")
	publication = DailyPublicationPolicy(timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence)
	correction = CorrectionPolicy(timedelta(days=arguments.recent_window_days), timedelta(hours=arguments.recent_refresh_hours),
		timedelta(days=arguments.archive_refresh_days), arguments.correction_evidence)
	database_path = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
	arguments.dbinfo_file = str(database_path)
	configuration = load_database_configuration(database_path)
	names = configuration.load_table_names()
	data_configuration = configuration.load_data_configuration() if configuration.data_configuration_file is not None else None
	calendar = ExchangeCalendar(DEFAULT_CALENDAR_ID if data_configuration is None else data_configuration.calendar_id, min(start, date(2005, 1, 1)), max(end, now.date()) + timedelta(days=14))
	raw_sessions, targets = session_workload(calendar, start, end, now, publication)
	connection = MySQLConnectionFactory()(database_path)
	cursor = connection.cursor()
	locked = False
	try:
		cursor.execute("SELECT GET_LOCK('stockguidance_daily_maintenance', 0)", ())
		locked = cursor.fetchall() == [(1,)]
		if not locked:
			raise RuntimeError("Another maintenance process is running on this database server")
		if data_configuration is None:
			cursor.execute(f"SELECT source_family, endpoint, market, universe, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment FROM {quote_identifier(names.source)} WHERE id = 1", ())
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise ValueError("Database data configuration must be initialized before maintenance")
			values = rows[0]
			data_configuration = DataConfiguration(str(values[0]), str(values[1]), str(values[2]), str(values[3]), bool(values[4]), bool(values[5]), None if values[6] is None else str(values[6]))
		if arguments.adjustment is not None and PriceAdjustment(arguments.adjustment) is not data_configuration.adjustment:
			raise ValueError("Requested adjustment conflicts with the database data configuration")
		arguments.adjustment = data_configuration.adjustment.value
		identity = data_configuration.to_source_identity()
		source = SourceConfig(identity, resolve_cli_path(arguments.data_root, "data"), database_path,
			"Configured Massive/Polygon credential", _configuration=data_configuration)
		assert_source(cursor, source, names, write=True)
		assert_transactional_tables(cursor, names)
		assert_zscore_tables(cursor, names, (20, 60))
		cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", ("stockguidance_migration_completion",))
		if cursor.fetchall() != [("InnoDB",)]:
			raise ValueError("The migrated production completion table must exist and use InnoDB")
		partial_imports_removed = _prepare_partial_imports(cursor, connection, names) if run_java else 0
		imports = _raw_import_state(cursor, names, raw_sessions)
		files_by_date: dict[date, dict[str, Any]] = {}
		availability: dict[date, str] = {}
		downloaded = 0
		refreshed_dates: set[date] = set()
		if run_source:
			store = ManifestStore(source.data_root)
			provider = ConfiguredPolygonProvider(3)
			started = monotonic()
			last_lock_check = started
			for index, session in enumerate(raw_sessions, 1):
				if monotonic() - last_lock_check >= 30:
					cursor.execute("SELECT IS_USED_LOCK('stockguidance_daily_maintenance') = CONNECTION_ID()", ())
					if cursor.fetchall() != [(1,)]:
						raise RuntimeError("The maintenance database lock was lost")
					connection.rollback()
					last_lock_check = monotonic()
				imported = imports.get(session.session_date)
				available_at = imported[1] if imported is not None else (publication.expected_at(session) or session.closes_at).isoformat()
				refresh_due = imported is not None and correction.refresh_due(session, datetime.fromisoformat(imported[1]), now)
				entry = None if refresh_due else _archive_entry(source, session, None if imported is None else imported[0], available_at)
				if refresh_due or entry is None:
					try:
						manifest = None if refresh_due else store.load(identity, session)
					except InvalidManifest:
						manifest = None
					if manifest is not None and correction.refresh_due(session, manifest.download.available_at, now):
						manifest = None
					if manifest is None or manifest.download.response.state is not ResponseState.COMPLETE:
						response = provider.fetch(source, session)
						if response.state is not ResponseState.COMPLETE:
							raise RuntimeError(f"Massive has not supplied a complete response for {session.session_date}; resolve publication or provider access and rerun")
						manifest = store.save_download(identity, DownloadedSession(session, response, clock.now()))
						downloaded += 1
					entry = export_archive(source, manifest)
					entry["available_at"] = manifest.download.available_at.isoformat()
					available_at = manifest.download.available_at.isoformat()
					refreshed_dates.add(session.session_date)
				files_by_date[session.session_date] = entry
				availability[session.session_date] = available_at
				remaining = (monotonic() - started) / index * (len(raw_sessions) - index)
				print(f"Source {index}/{len(raw_sessions)} {session.session_date}; downloaded {downloaded}; estimated {remaining:.0f}s remaining in source phase", file=sys.stderr, flush=True)
		elif run_java:
			for session in raw_sessions:
				imported = imports.get(session.session_date)
				available_at = imported[1] if imported is not None else (publication.expected_at(session) or session.closes_at).isoformat()
				entry = _archive_entry(source, session, None if imported is None else imported[0], available_at)
				if entry is None:
					raise ValueError(f"Stage 'java' requires the source archive for {session.session_date}; run --stage source first")
				file_sha256, archive_records_id = archive_identity(Path(entry["path"]))
				entry["file_sha256"] = file_sha256
				entry["records_id"] = archive_records_id
				files_by_date[session.session_date] = entry
				availability[session.session_date] = available_at
				if imported is None or imported[0] != archive_records_id:
					refreshed_dates.add(session.session_date)
		else:
			for session in raw_sessions:
				imported = imports.get(session.session_date)
				availability[session.session_date] = imported[1] if imported is not None else (publication.expected_at(session) or session.closes_at).isoformat()

		if stage == "source":
			return {"successful": True, "stage": "source", "lookback_years": 2,
				"start_date": targets[0].session_date.isoformat(), "end_date": targets[-1].session_date.isoformat(),
				"downloaded_sessions": downloaded, "java_updates": [], "partial_imports_removed": 0,
				"python_validation": {"complete": False, "skipped": True, "batches": []},
				"forecast_session": targets[-1].session_date.isoformat(), "forecast_input_session": targets[-1].session_date.isoformat(),
				"training_outcomes_through": (targets[-1] if calendar.shift(targets[-1], 1).session_date in availability else calendar.shift(targets[-1], -1)).session_date.isoformat()}

		checkpoint = Path(arguments.java_checkpoint_directory).resolve()
		if run_java:
			checkpoint.mkdir(parents=True, exist_ok=True)
		reports: list[dict[str, Any]] = []
		validations: list[dict[str, Any]] = []
		if stage == "validation":
			for offset in range(0, len(targets), arguments.batch_sessions):
				batch = targets[offset:offset + arguments.batch_sessions]
				expected_sessions = [session.session_date.isoformat() for session in batch]
				candidates = []
				for workload_path in sorted(checkpoint.glob("workload-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
					with workload_path.open(encoding="utf-8") as stream:
						workload = json.load(stream)
					if workload.get("zscore_sessions") == expected_sessions:
						candidates.append((workload_path, workload))
				if not candidates:
					raise ValueError(f"Stage 'validation' requires a completed Java workload for {batch[0].session_date} through {batch[-1].session_date}; run --stage java first")
				workload_path, workload = candidates[0]
				report_path = checkpoint / f"report-{workload['manifest_id']}.json"
				if not report_path.is_file():
					raise ValueError(f"Stage 'validation' requires the Java report for {workload_path.name}; run --stage java first")
				with report_path.open(encoding="utf-8") as stream:
					report = json.load(stream)
				reports.append(report)
				print(f"Independently validating {batch[0].session_date} through {batch[-1].session_date}", file=sys.stderr, flush=True)
				zscores = validate_materialized_zscores(source, names, workload, report, checkpoint.parent / "python-validation")
				validation_report = {"exit_code": 0 if zscores["successful"] else 1, "complete": zscores["successful"], "zscores": zscores,
					"findings": [] if zscores["successful"] else ["Independent z-score validation found mismatches"]}
				validations.append(validation_report)
				atomic_write(checkpoint / f"validation-{workload['manifest_id']}.json", encode_json({**validation_report,
					"findings": validation_report["findings"]}))
				if not zscores["successful"]:
					break
		else:
			assert command is not None
			java_started = monotonic()
			for offset in range(0, len(targets), arguments.batch_sessions):
				cursor.execute("SELECT IS_USED_LOCK('stockguidance_daily_maintenance') = CONNECTION_ID()", ())
				if cursor.fetchall() != [(1,)]:
					raise RuntimeError("The maintenance database lock was lost")
				connection.rollback()
				batch = targets[offset:offset + arguments.batch_sessions]
				first = calendar.shift(batch[0], -61).session_date
				last = min(calendar.shift(batch[-1], 3).session_date, raw_sessions[-1].session_date)
				inputs = tuple(session for session in raw_sessions if first <= session.session_date <= last)
				batch_raw_dates = tuple(session for session in inputs if session.session_date not in imports or imports[session.session_date][2] == 0 or session.session_date in refreshed_dates)
				workload = build_workload(source, names, calendar, inputs, batch, files_by_date, availability, batch_raw_dates)
				path = checkpoint / f"workload-{workload['manifest_id']}.json"
				atomic_write(path, encode_json(workload))
				report = execute_java(arguments, command, path, checkpoint)
				reports.append(report)
				atomic_write(checkpoint / f"report-{workload['manifest_id']}.json", encode_json(report))
				if not run_validation:
					continue
				print(f"Independently validating {batch[0].session_date} through {batch[-1].session_date}", file=sys.stderr, flush=True)
				zscores = validate_materialized_zscores(source, names, workload, report, checkpoint.parent / "python-validation")
				validation_report = {"exit_code": 0 if zscores["successful"] else 1, "complete": zscores["successful"], "zscores": zscores,
					"findings": [] if zscores["successful"] else ["Independent z-score validation found mismatches"]}
				validations.append(validation_report)
				atomic_write(checkpoint / f"validation-{workload['manifest_id']}.json", encode_json({**validation_report,
					"findings": validation_report["findings"]}))
				if not zscores["successful"]:
					break
				completed = min(offset + len(batch), len(targets))
				remaining = (monotonic() - java_started) / completed * (len(targets) - completed)
				print(f"Updated and validated {completed}/{len(targets)} sessions; estimated {remaining:.0f}s remaining", file=sys.stderr, flush=True)
		successful = all(item["complete"] and item["zscores"]["successful"] for item in validations)
		if stage == "java":
			successful = True
		elif stage == "validation":
			successful = all(item["complete"] and item["zscores"]["successful"] for item in validations)
		return {"successful": successful, "stage": stage or "all", "lookback_years": 2,
			"start_date": targets[0].session_date.isoformat(), "end_date": targets[-1].session_date.isoformat(),
			"downloaded_sessions": downloaded, "java_updates": reports,
			"partial_imports_removed": partial_imports_removed,
			"python_validation": {"complete": successful if run_validation else False, "skipped": not run_validation,
				"batches": [{**item, "findings": [str(finding) for finding in item["findings"]]} for item in validations]},
			"forecast_session": targets[-1].session_date.isoformat(),
			"forecast_input_session": targets[-1].session_date.isoformat(),
			"training_outcomes_through": (targets[-1] if calendar.shift(targets[-1], 1).session_date in availability else calendar.shift(targets[-1], -1)).session_date.isoformat()}
	finally:
		try:
			if locked:
				cursor.execute("SELECT RELEASE_LOCK('stockguidance_daily_maintenance')", ())
				cursor.fetchall()
		finally:
			cursor.close()
			connection.close()
