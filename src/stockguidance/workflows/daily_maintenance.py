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
from stockguidance.cli.paths import resolve_cli_path
from stockguidance.cli.validation_setup import assemble_and_validate
from stockguidance.config.source import DataConfiguration, PriceAdjustment, SourceConfig
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
from stockguidance.market_data.records import bar_payload, records_id


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


def build_workload(source: SourceConfig, names: TableNames, calendar: SessionCalendar, raw_sessions: tuple[TradingSession, ...], targets: tuple[TradingSession, ...], files_by_date: Mapping[date, dict[str, Any]], availability: Mapping[date, str]) -> dict[str, Any]:
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
	root = {"schema_version": 1, "kind": "daily-maintenance", "data_root": str(source.data_root.resolve()),
		"source_key": data_configuration_key(source.data_configuration), "source": {"archive_source": asdict(source.identity)},
		"authoritative_registry": {"schema_version": 1, "tables": names.tables(), "zscore_templates": names.templates()},
		"files": files, "targets": target_entries, "mappings": {"ticker_mappings": []},
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


def validate_zscores(source: SourceConfig, names: TableNames, workload: dict[str, Any], report: dict[str, Any], directory: Path) -> dict[str, Any]:
	from stockguidance.performance.stage15 import _date_ids, _imports
	from stockguidance.performance.stage15_pipeline import PipelineOptions, run_pipeline
	selected = set(workload["zscore_sessions"])
	manifest = {**workload, "targets": [target for target in workload["targets"] if target["session"] in selected]}
	files = {item["session"]: item["records_id"] for item in workload["files"] if item["valid"]}
	connection = MySQLConnectionFactory()(source.database_config)
	try:
		cursor = connection.cursor()
		try:
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())
			cursor.execute("START TRANSACTION READ ONLY", ())
		finally:
			cursor.close()
		identifiers = _date_ids(connection, names, tuple(files))
		verified = _imports(connection, names, tuple(files), files)
		with redirect_stdout(sys.stderr):
			result = run_pipeline(connection, names, manifest, identifiers,
				{identifier: verified.get(session, False) for session, identifier in identifiers.items()}, directory,
				{"workload": workload["manifest_id"], "snapshot": uuid4().hex},
				PipelineOptions(sessions_per_batch=20), published_metadata=report["zscores"]["series"])
		metadata = result["metadata"]
		result["successful"] = result["status"] == "complete" and not result["mismatch_count"] and not metadata["series_findings"] and not metadata["metadata_findings"]
		return result
	finally:
		connection.rollback()
		connection.close()


def run_daily_maintenance(arguments: argparse.Namespace) -> dict[str, object]:
	from stockguidance.cli.maintain_daily import maintenance_range
	command = java_preflight(arguments)
	clock = SystemClock()
	now = clock.now()
	default_start, default_end = maintenance_range(now.date())
	start, end = arguments.start_date or default_start, arguments.end_date or default_end
	if start > end or start < date(2006, 11, 16):
		raise ValueError("Maintenance bounds must be ordered and start on or after 2006-11-16")
	publication = DailyPublicationPolicy(timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence)
	correction = CorrectionPolicy(timedelta(days=arguments.recent_window_days), timedelta(hours=arguments.recent_refresh_hours),
		timedelta(days=arguments.archive_refresh_days), arguments.correction_evidence)
	calendar = ExchangeCalendar("XNYS", min(start, date(2005, 1, 1)), max(end, now.date()) + timedelta(days=14))
	raw_sessions, targets = session_workload(calendar, start, end, now, publication)
	database_path = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
	arguments.dbinfo_file = str(database_path)
	configuration = load_database_configuration(database_path)
	names = configuration.load_table_names()
	connection = MySQLConnectionFactory()(database_path)
	cursor = connection.cursor()
	locked = False
	try:
		cursor.execute("SELECT GET_LOCK('stockguidance_daily_maintenance', 0)", ())
		locked = cursor.fetchall() == [(1,)]
		if not locked:
			raise RuntimeError("Another maintenance process is running on this database server")
		if configuration.data_configuration_file is not None:
			data_configuration = configuration.load_data_configuration()
		else:
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
		connection.rollback()
		store = ManifestStore(source.data_root)
		provider = ConfiguredPolygonProvider(3)
		files_by_date = {}
		availability = {}
		downloaded = 0
		started = monotonic()
		for index, session in enumerate(raw_sessions, 1):
			cursor.execute("SELECT IS_USED_LOCK('stockguidance_daily_maintenance') = CONNECTION_ID()", ())
			if cursor.fetchall() != [(1,)]:
				raise RuntimeError("The maintenance database lock was lost")
			connection.rollback()
			try:
				manifest = store.load(identity, session)
			except InvalidManifest:
				manifest = None
			if manifest is None or manifest.download.response.state is not ResponseState.COMPLETE or correction.refresh_due(session, manifest.download.available_at, now):
				response = provider.fetch(source, session)
				if response.state is not ResponseState.COMPLETE:
					raise RuntimeError(f"Massive has not supplied a complete response for {session.session_date}; resolve publication or provider access and rerun")
				manifest = store.save_download(identity, DownloadedSession(session, response, clock.now()))
				downloaded += 1
			files_by_date[session.session_date] = export_archive(source, manifest)
			availability[session.session_date] = manifest.download.available_at.isoformat()
			remaining = (monotonic() - started) / index * (len(raw_sessions) - index)
			print(f"Source {index}/{len(raw_sessions)} {session.session_date}; downloaded {downloaded}; estimated {remaining:.0f}s remaining in source phase", file=sys.stderr, flush=True)
		checkpoint = Path(arguments.java_checkpoint_directory).resolve()
		checkpoint.mkdir(parents=True, exist_ok=True)
		reports = []
		validations: list[dict[str, Any]] = []
		java_started = monotonic()
		for offset in range(0, len(targets), arguments.batch_sessions):
			batch = targets[offset:offset + arguments.batch_sessions]
			first = calendar.shift(batch[0], -61).session_date
			last = min(calendar.shift(batch[-1], 3).session_date, raw_sessions[-1].session_date)
			inputs = tuple(session for session in raw_sessions if first <= session.session_date <= last)
			workload = build_workload(source, names, calendar, inputs, batch, files_by_date, availability)
			path = checkpoint / f"workload-{workload['manifest_id']}.json"
			atomic_write(path, encode_json(workload))
			report = execute_java(arguments, command, path, checkpoint)
			reports.append(report)
			for session in inputs[1:]:
				if session.session_date <= calendar.shift(batch[-1], 2).session_date:
					manifest = store.load(identity, session)
					if manifest is not None:
						store.mark_complete(manifest)
			validation_arguments = argparse.Namespace(data_root=str(source.data_root), dbinfo_file=str(database_path),
				start_date=batch[0].session_date.isoformat(), end_date=batch[-1].session_date.isoformat(),
				history_start=arguments.history_start, history_evidence=arguments.history_evidence, adjustment=arguments.adjustment,
				as_of=None, publication_delay_minutes=arguments.publication_delay_minutes, publication_evidence=arguments.publication_evidence,
				format="json", ticker_ids=None, lookback_sessions=None, signal_mode="previous_close", zscore_windows=None)
			print(f"Independently validating {batch[0].session_date} through {batch[-1].session_date}", file=sys.stderr, flush=True)
			validation = assemble_and_validate(validation_arguments)
			zscores = validate_zscores(source, names, workload, report, checkpoint.parent / "python-validation")
			validation_report = {"exit_code": validation.exit_code, "complete": validation.complete, "zscores": zscores,
				"findings": [asdict(finding) for finding in validation.findings]}
			validations.append(validation_report)
			atomic_write(checkpoint / f"validation-{workload['manifest_id']}.json", encode_json({**validation_report,
				"findings": [str(finding) for finding in validation.findings]}))
			if validation.exit_code != 0 or not zscores["successful"]:
				break
			completed = min(offset + len(batch), len(targets))
			remaining = (monotonic() - java_started) / completed * (len(targets) - completed)
			print(f"Updated and validated {completed}/{len(targets)} sessions; estimated {remaining:.0f}s remaining", file=sys.stderr, flush=True)
		successful = all(item["exit_code"] == 0 and item["zscores"]["successful"] for item in validations)
		return {"successful": successful, "lookback_years": 2,
			"start_date": targets[0].session_date.isoformat(), "end_date": targets[-1].session_date.isoformat(),
			"downloaded_sessions": downloaded, "java_updates": reports,
			"python_validation": {"complete": successful, "batches": [{**item, "findings": [str(finding) for finding in item["findings"]]} for item in validations]},
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
