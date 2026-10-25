import argparse
from contextlib import nullcontext
from datetime import date, datetime, timezone
import json
import sys
from pathlib import Path
from typing import Any, cast

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import SourceConfig, SourceIdentity
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.table_names import load_table_names, quote_identifier, TableRole
from stockguidance.database.repositories.archive_catalog import ArchiveCatalog
from stockguidance.market_data.archive import archive_content, decode_archive
from hashlib import sha256
from stockguidance.market_data.canonical_store import CanonicalArchiveStore
from stockguidance.market_data.consolidation import audit, file_id
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.workflows.market_lock import market_update_lock
from stockguidance.market_data.securities import security_metadata
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.market_data.consolidation import candidates
from stockguidance.market_data.cleanup import ArchiveCleanup
from stockguidance.market_data.reimport import apply_reimport, plan as reimport_plan


def configured_source(arguments: argparse.Namespace) -> SourceConfig:
	configuration = load_database_configuration(arguments.dbinfo_file)
	if configuration.data_configuration_file is None:
		raise ValueError("Consolidation requires an explicit unadjusted data configuration")
	data_configuration = configuration.load_data_configuration()
	if data_configuration.raw_price_split_adjusted or data_configuration.raw_price_dividend_adjusted or data_configuration.raw_price_vendor_defined_adjustment is not None:
		raise ValueError("Consolidation destination must use unadjusted data")
	return SourceConfig(data_configuration.to_source_identity(), arguments.data_root.resolve(), arguments.dbinfo_file.resolve(), "archive migration", _configuration=data_configuration)


def export_catalog(arguments: argparse.Namespace) -> dict[str, object]:
	source = configured_source(arguments)
	entries = ArchiveCatalog(source, MySQLConnectionFactory()).inventory()
	path = arguments.export_catalog.resolve()
	if path.is_relative_to(source.data_root):
		raise ValueError("Catalog exports belong outside the market-data archive")
	atomic_write(path, encode_json({"schema_version": 1, "data_configuration": source.data_configuration.semantic_payload(), "entries": {str(session): metadata for session, metadata in entries.items()}}))
	return {"exported_dates": len(entries), "path": str(path)}


def retire_legacy(arguments: argparse.Namespace) -> dict[str, object]:
	source = configured_source(arguments)
	store = CanonicalArchiveStore(source.data_root, ArchiveCatalog(source, MySQLConnectionFactory()))
	backup = source.data_root / "archive-backup" / "retired" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
	paths = set()
	with market_update_lock(source, MySQLConnectionFactory()):
		for day, copies in candidates(source.data_root).items():
			legacy = [item for item in copies if item["location"] != "market_summary"]
			if not legacy:
				continue
			session_date = date.fromisoformat(day)
			calendar = ExchangeCalendar(source.calendar_id, session_date.replace(month=1, day=1), session_date.replace(month=12, day=31))
			if store.load(cast(SourceIdentity, source.identity), calendar.session(session_date)) is None:
				raise ValueError(f"Cannot retire legacy data before verified canonical coverage exists for {day}")
			for item in legacy:
				path = Path(item.get("manifest", item["path"])).resolve().parent
				if path.parent not in {source.data_root / "market_data", source.data_root / "daily-maintenance"} or len(path.name) != 64 or any(character not in "0123456789abcdef" for character in path.name):
					raise ValueError("Retirement target is not a legacy source directory")
				paths.add(path)
		moves = [{"from": str(path), "to": str(backup / path.relative_to(source.data_root))} for path in sorted(paths)]
		if arguments.apply:
			backup.mkdir(parents=True, exist_ok=False)
			atomic_write(backup / "restore.json", encode_json(moves))
			for move in moves:
				original, destination = Path(move["from"]), Path(move["to"])
				if not original.is_relative_to(source.data_root) or not destination.is_relative_to(backup):
					raise ValueError("Retirement path escapes its validated roots")
				destination.parent.mkdir(parents=True, exist_ok=True)
				original.rename(destination)
	return {"applied": arguments.apply, "moves": moves, "deleted_files": 0}


def migrate(arguments: argparse.Namespace) -> dict[str, object]:
	source = configured_source(arguments)
	data_configuration = source.data_configuration
	decisions = json.loads(arguments.decisions.read_text(encoding="utf-8"))
	if not isinstance(decisions, list) or not decisions:
		raise ValueError("Migration decisions must be a nonempty list")
	prepared = []
	seen = set()
	calendars: dict[int, ExchangeCalendar] = {}
	def validated_download(path: Path, decision: dict[str, Any]) -> DownloadedSession:
		session_date = date.fromisoformat(decision["date"])
		if file_id(path) != decision["sha256"]:
			raise ValueError(f"Migration source changed: {session_date}")
		if session_date.year not in calendars:
			calendars[session_date.year] = ExchangeCalendar(data_configuration.calendar_id, session_date.replace(month=1, day=1), session_date.replace(month=12, day=31))
		session = calendars[session_date.year].session(session_date)
		return DownloadedSession(session, decode_archive(path.read_bytes(), session), datetime.fromisoformat(decision["available_at"]))

	for decision in decisions:
		if decision.get("adjustment") != "unadjusted" or decision.get("complete") is not True or not isinstance(decision.get("evidence"), str) or not decision["evidence"].strip():
			raise ValueError("Every selected source requires complete unadjusted verification and evidence")
		for superseded in decision.get("supersedes", []):
			identity = superseded["sha256"]
			if not isinstance(identity, str) or len(identity) != 64 or any(character not in "0123456789abcdef" for character in identity) or not isinstance(superseded.get("evidence"), str) or not superseded["evidence"].strip():
				raise ValueError("Superseded sources require a SHA-256 and explicit replacement evidence")
		session_date = date.fromisoformat(decision["date"])
		if session_date in seen:
			raise ValueError("Duplicate migration date")
		seen.add(session_date)
		path = Path(decision["path"]).resolve()
		validated_download(path, decision)
		prepared.append((path, decision))
	if not arguments.apply:
		return {"validated_dates": len(prepared), "applied": False}
	connect = MySQLConnectionFactory()
	store = CanonicalArchiveStore(source.data_root, ArchiveCatalog(source, connect))
	def retain_decision(decision: dict[str, Any], download: DownloadedSession) -> None:
		if decision.get("supersedes"):
			metadata = store.load_metadata(cast(SourceIdentity, source.identity), download.session)
			if metadata is None:
				raise ValueError("Replacement metadata was not published")
			approved = {item["sha256"]: item for item in [*metadata.get("supersedes", []), *decision["supersedes"]]}
			metadata = {**metadata, "supersedes": list(approved.values())}
			store.catalog.put(download.session.session_date, metadata)
	with market_update_lock(source, connect):
		for path, decision in prepared:
			download = validated_download(path, decision)
			existing = store.load_metadata(cast(SourceIdentity, source.identity), download.session)
			if existing is not None and not existing.get("pending") and existing["content_id"] == sha256(archive_content(download)).hexdigest():
				loaded = store.load(cast(SourceIdentity, source.identity), download.session)
				if loaded is not None:
					retain_decision(decision, download)
					continue
			if existing is not None and datetime.fromisoformat(existing["available_at"]) > download.available_at:
				raise ValueError(f"A later evidenced canonical source already exists for {download.session.session_date}")
			store.save_download(cast(SourceIdentity, source.identity), download, evidence=decision["evidence"])
			retain_decision(decision, download)
	return {"validated_dates": len(prepared), "applied": True, "retired_files": 0}


def cleanup(arguments: argparse.Namespace) -> dict[str, Any]:
	source = configured_source(arguments)
	connect = MySQLConnectionFactory()
	store = CanonicalArchiveStore(source.data_root, ArchiveCatalog(source, connect))
	with market_update_lock(source, connect) if arguments.apply else nullcontext():
		operation = ArchiveCleanup(source, store, arguments.output, apply=arguments.apply, progress=lambda message: print(message, file=sys.stderr, flush=True))
		report = operation.run(consolidate=arguments.consolidate, cleanup_conflicts=arguments.cleanup_conflicts)
	def quoted(path: Path) -> str:
		return "'" + str(path).replace("'", "''") + "'"
	command = ".\\.venv\\Scripts\\python.exe -m stockguidance.cli.consolidate_market_data"
	command += f" --data-root {quoted(arguments.data_root)} --dbinfo-file {quoted(arguments.dbinfo_file)} --output {quoted(arguments.output)}"
	flags = (" --consolidate" if arguments.consolidate else "") + (" --cleanup-conflicts" if arguments.cleanup_conflicts else "")
	report["next_commands"] = [command + flags + ("" if arguments.apply else " --apply")]
	if report["unresolved"]:
		report["instructions"] = "Review unresolved entries; publish verified decisions with --decisions FILE (preview, then --apply), then run --cleanup-conflicts (preview, then --apply). Unresolved files are never forcibly deleted. See docs/market_storage.md."
		report["next_commands"].append(command + " --cleanup-conflicts")
	else:
		report["instructions"] = "Review cleanup-report.json and cleanup-journal.jsonl. Repeating the same command safely resumes interrupted work."
	atomic_write(arguments.output / "cleanup-report.json", encode_json(report))
	return report


def reimport(arguments: argparse.Namespace) -> dict[str, Any]:
	source = configured_source(arguments)
	connect = MySQLConnectionFactory()
	names = load_table_names()
	connection = connect(source.database_config)
	try:
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT MAX(session_date) FROM {quote_identifier(names.table(TableRole.IMPORTS))}", ())
			rows = cursor.fetchall()
			row = rows[0] if rows else None
		finally:
			cursor.close()
	finally:
		connection.close()
	if row is None or row[0] is None:
		raise ValueError("Cannot determine the database's latest imported session")
	end = date.fromisoformat(str(row[0]))
	calendar = ExchangeCalendar(source.calendar_id, arguments.reimport_from, end)
	plan_value = reimport_plan(source.data_root, arguments.reimport_from, end, calendar)
	report: dict[str, Any] = {"operation": "reimport", "applied": False, "start": plan_value.start.isoformat(), "end": plan_value.end.isoformat(), "derived_start": plan_value.derived_start.isoformat(), "canonical_dates": len(plan_value.files), "requested_dates": len(plan_value.canonical_dates), "missing_dates": [item.isoformat() for item in plan_value.missing_dates], "missing_count": len(plan_value.missing_dates), "files": [{"date": item.day.isoformat(), "path": str(item.path), "sha256": item.content_id, "rows": item.row_count} for item in plan_value.files], "warning": "Apply is blocked until every database session in the reimport range has a validated market_summary archive."}
	if arguments.apply:
		with market_update_lock(source, connect):
			report = apply_reimport(source, connect, names, "reimport", arguments.backup_confirmation or "", plan_value)
	atomic_write(arguments.output / "reimport-report.json", encode_json(report))
	return report


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Audit and explicitly consolidate daily archives without guessing adjustment provenance")
	parser.add_argument("--data-root", type=Path, default=Path("data"))
	parser.add_argument("--output", type=Path, default=Path("output/storage-consolidation"))
	parser.add_argument("--security-metadata", type=Path)
	parser.add_argument("--decisions", type=Path, help="Explicit verified-source decisions; omitted for audit-only operation")
	parser.add_argument("--export-catalog", type=Path, help="Export canonical provenance outside the archive alongside database backups")
	parser.add_argument("--retire-legacy", action="store_true", help="Move old source directories into a recoverable backup after complete canonical verification")
	parser.add_argument("--consolidate", action="store_true", help="Consolidate verified daily data, delete redundant copies, and quarantine unresolved files")
	parser.add_argument("--cleanup-conflicts", action="store_true", help="Reevaluate quarantined files; delete only verified duplicates or explicitly approved superseded sources")
	parser.add_argument("--reimport-from", type=date.fromisoformat, help="Reset and reimport database sessions from this date through the latest imported session")
	parser.add_argument("--backup-confirmation", help="Required with reimport --apply: describe the completed database backup")
	parser.add_argument("--dbinfo-file", type=Path, default=Path("dbinfo.id"))
	parser.add_argument("--apply", action="store_true", help="Apply the selected operation; cleanup can permanently delete verified redundant files")
	arguments = parser.parse_args(argv)
	try:
		operations = (arguments.decisions is not None, arguments.export_catalog is not None, arguments.retire_legacy, arguments.consolidate or arguments.cleanup_conflicts, arguments.reimport_from is not None)
		if sum(operations) > 1:
			raise ValueError("Choose one consolidation operation")
		if arguments.reimport_from is not None:
			report = reimport(arguments)
		elif arguments.consolidate or arguments.cleanup_conflicts:
			report = cleanup(arguments)
		elif arguments.export_catalog is not None:
			report = export_catalog(arguments)
		elif arguments.retire_legacy:
			report = retire_legacy(arguments)
		elif arguments.decisions is not None:
			report = migrate(arguments)
		else:
			if arguments.apply:
				raise ValueError("--apply requires explicit --decisions")
			securities = security_metadata(arguments.data_root)
			if arguments.security_metadata is not None:
				securities.update(json.loads(arguments.security_metadata.read_text(encoding="utf-8")))
			report = audit(arguments.data_root, arguments.output, securities=securities)
		print(json.dumps(report, sort_keys=True))
		return 2 if report.get("unresolved") else 0
	except (OSError, ValueError, KeyError, TypeError) as error:
		print(json.dumps({"successful": False, "error": str(error)}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
