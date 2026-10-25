from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import monotonic
from typing import Any

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.market_data.manifests import SourceManifest
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState
from stockguidance.market_data.records import decode_bar, records_id
from stockguidance.workflows.daily_maintenance_context import MaintenanceSession
from stockguidance.market_data.quality import invalid_fields
from stockguidance.market_data.archive import archive_path
from stockguidance.market_data.canonical_store import CanonicalArchiveStore
from stockguidance.database.repositories.archive_catalog import ArchiveCatalog
from stockguidance.database.connections import MySQLConnectionFactory


@dataclass
class SourceStageResult:
	files_by_date: dict[date, dict[str, Any]]
	availability: dict[date, str]
	downloaded: int
	refreshed_dates: set[date]


def export_archive(source: SourceConfig, download: DownloadedSession | SourceManifest) -> dict[str, Any]:
	if isinstance(download, SourceManifest):
		download = download.download
	store = CanonicalArchiveStore(source.data_root, ArchiveCatalog(source, MySQLConnectionFactory()))
	manifest = store.save_download(source.identity, download)
	path = store.path_for(source.identity, download.session)
	return {"session": download.session.session_date.isoformat(), "path": str(path.resolve()), "valid": True,
		"file_sha256": manifest.content_id, "records_id": records_id(download.response.records)}


def archive_entry(source: SourceConfig, session: TradingSession, raw_id: str | None, available_at: str) -> dict[str, Any] | None:
	path = archive_path(source.data_root, session.session_date)
	if not path.is_file():
		return None
	return {"session": session.session_date.isoformat(), "path": str(path.resolve()), "valid": True,
		"file_sha256": None, "records_id": raw_id, "available_at": available_at}


def archive_identity(path: Path) -> tuple[str, str]:
	content = path.read_bytes()
	records = []
	tickers: set[str] = set()
	for line in content.splitlines():
		row = json.loads(line)
		if not isinstance(row, dict):
			raise ValueError(f"Daily-maintenance archive record is not an object: {path}")
		ticker = row.get("T")
		if not isinstance(ticker, str) or not ticker or ticker in tickers:
			raise ValueError(f"Daily-maintenance archive has an invalid or duplicate ticker: {path}")
		tickers.add(ticker)
		if invalid_fields(tuple(row.get(field) for field in ("o", "h", "l", "c", "v", "vw", "n"))):
			continue
		records.append(decode_bar(row))
	if not tickers:
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
		refresh_due = getattr(getattr(context, "arguments", None), "refresh_source", False) and session in context.targets
		entry = None
		if imported is not None and not refresh_due:
			entry = {"session": session.session_date.isoformat(), "path": None, "valid": True, "records_id": imported[0]}
		elif not refresh_due:
			store = CanonicalArchiveStore(context.source.data_root, ArchiveCatalog(context.source, MySQLConnectionFactory()))
			try:
				manifest = store.load(context.source.identity, session)
			except ValueError:
				manifest = None
			if manifest is not None:
				available_at = manifest.download.available_at.isoformat()
				entry = archive_entry(context.source, session, manifest.content_id, available_at)
		if refresh_due or entry is None:
			response = provider.fetch(context.source, session)
			if response.state is not ResponseState.COMPLETE:
				raise RuntimeError(f"Massive has not supplied a complete response for {session.session_date}; resolve publication or provider access and rerun")
			download = DownloadedSession(session, response, context.clock.now())
			downloaded += 1
			maintenance.cursor.execute("INSERT INTO stockguidance_daily_source_files (source_key, session_date, file_size, modified_ns, download_pending) VALUES (%s, %s, 0, 0, 1) ON DUPLICATE KEY UPDATE download_pending = 1", (data_configuration_key(context.source.data_configuration), session.session_date))
			maintenance.connection.commit()
			entry = export_archive(context.source, download)
			entry["available_at"] = download.available_at.isoformat()
			available_at = download.available_at.isoformat()
			refreshed_dates.add(session.session_date)
		files_by_date[session.session_date] = entry
		availability[session.session_date] = available_at
		remaining = (monotonic() - started) / index * (len(context.raw_sessions) - index)
		print(f"Source {index}/{len(context.raw_sessions)} {session.session_date}; downloaded {downloaded}; estimated {remaining:.0f}s remaining in source phase", file=sys.stderr, flush=True)
	return SourceStageResult(files_by_date, availability, downloaded, refreshed_dates)


def read_archives_for_java(maintenance: MaintenanceSession) -> SourceStageResult:
	context = maintenance.context
	print(f"Preparing {len(context.raw_sessions)} sessions from database receipts; Java will inspect pending downloads", file=sys.stderr, flush=True)
	return SourceStageResult({}, availability_from_database(maintenance), 0, set())
