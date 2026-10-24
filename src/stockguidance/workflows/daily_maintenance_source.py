from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import monotonic
from typing import Any

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import data_configuration_key, encode_json
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore, SourceManifest
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState
from stockguidance.market_data.records import bar_payload, decode_bar, records_id
from stockguidance.workflows.daily_maintenance_context import MaintenanceSession


@dataclass
class SourceStageResult:
	files_by_date: dict[date, dict[str, Any]]
	availability: dict[date, str]
	downloaded: int
	refreshed_dates: set[date]


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


def archive_entry(source: SourceConfig, session: TradingSession, raw_id: str | None, available_at: str) -> dict[str, Any] | None:
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


def availability_from_database(maintenance: MaintenanceSession) -> dict[date, str]:
	context = maintenance.context
	return {
		session.session_date: maintenance.imports[session.session_date][1]
		if session.session_date in maintenance.imports
		else (context.publication.expected_at(session) or session.closes_at).isoformat()
		for session in context.raw_sessions
	}


def run_source_stage(maintenance: MaintenanceSession) -> SourceStageResult:
	context = maintenance.context
	store = ManifestStore(context.source.data_root)
	identity = context.source.data_configuration.to_source_identity()
	provider = ConfiguredPolygonProvider(3)
	started = monotonic()
	last_lock_check = started
	files_by_date: dict[date, dict[str, Any]] = {}
	availability: dict[date, str] = {}
	refreshed_dates: set[date] = set()
	downloaded = 0
	for index, session in enumerate(context.raw_sessions, 1):
		if monotonic() - last_lock_check >= 30:
			maintenance.assert_lock()
			last_lock_check = monotonic()
		imported = maintenance.imports.get(session.session_date)
		available_at = imported[1] if imported is not None else (context.publication.expected_at(session) or session.closes_at).isoformat()
		refresh_due = imported is not None and context.correction.refresh_due(session, datetime.fromisoformat(imported[1]), context.now)
		entry = None if refresh_due else archive_entry(context.source, session, None if imported is None else imported[0], available_at)
		if refresh_due or entry is None:
			try:
				manifest = None if refresh_due else store.load(identity, session)
			except InvalidManifest:
				manifest = None
			if manifest is not None and context.correction.refresh_due(session, manifest.download.available_at, context.now):
				manifest = None
			if manifest is None or manifest.download.response.state is not ResponseState.COMPLETE:
				response = provider.fetch(context.source, session)
				if response.state is not ResponseState.COMPLETE:
					raise RuntimeError(f"Massive has not supplied a complete response for {session.session_date}; resolve publication or provider access and rerun")
				manifest = store.save_download(identity, DownloadedSession(session, response, context.clock.now()))
				downloaded += 1
			entry = export_archive(context.source, manifest)
			entry["available_at"] = manifest.download.available_at.isoformat()
			available_at = manifest.download.available_at.isoformat()
			refreshed_dates.add(session.session_date)
		files_by_date[session.session_date] = entry
		availability[session.session_date] = available_at
		remaining = (monotonic() - started) / index * (len(context.raw_sessions) - index)
		print(f"Source {index}/{len(context.raw_sessions)} {session.session_date}; downloaded {downloaded}; estimated {remaining:.0f}s remaining in source phase", file=sys.stderr, flush=True)
	return SourceStageResult(files_by_date, availability, downloaded, refreshed_dates)


def read_archives_for_java(maintenance: MaintenanceSession) -> SourceStageResult:
	context = maintenance.context
	files_by_date: dict[date, dict[str, Any]] = {}
	availability: dict[date, str] = {}
	refreshed_dates: set[date] = set()
	for session in context.raw_sessions:
		imported = maintenance.imports.get(session.session_date)
		available_at = imported[1] if imported is not None else (context.publication.expected_at(session) or session.closes_at).isoformat()
		entry = archive_entry(context.source, session, None if imported is None else imported[0], available_at)
		if entry is None:
			raise ValueError(f"Stage 'java' requires the source archive for {session.session_date}; run --stage source first")
		file_sha256, archive_records_id = archive_identity(Path(entry["path"]))
		entry["file_sha256"] = file_sha256
		entry["records_id"] = archive_records_id
		files_by_date[session.session_date] = entry
		availability[session.session_date] = available_at
		if imported is None or imported[0] != archive_records_id:
			refreshed_dates.add(session.session_date)
	return SourceStageResult(files_by_date, availability, 0, refreshed_dates)
