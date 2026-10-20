import argparse
from datetime import date, datetime
import json
from pathlib import Path
from typing import Sequence

from stockguidance.database.table_names import load_table_names
from stockguidance.legacy_migration.audit import ARCHIVE_SOURCE, audit_archive, read_database_tickers
from stockguidance.legacy_migration.evidence import ArchiveEvidenceStore, evidence_for_file
from stockguidance.legacy_migration.identity import TickerIdentity, identity_report
from stockguidance.legacy_migration.precision import ArchiveFileInventory, ArchiveInventory, inventory_payload, scan_archive, scan_archive_keys
from stockguidance.legacy_migration.snapshot import load_archive_snapshot
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.cli.paths import resolve_cli_path


def _date(value: str) -> date:
	try:
		return date.fromisoformat(value)
	except ValueError:
		raise argparse.ArgumentTypeError("Expected YYYY-MM-DD") from None


def _path(value: str) -> Path:
	if not value:
		raise argparse.ArgumentTypeError("Path must be nonempty")
	return Path(value)


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Read-only Stage 5 legacy archive contracts and audit")
	arguments.add_argument("--mode", choices=("audit", "identity", "precision", "evidence", "snapshot"), default="audit")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--dbinfo-file", type=Path)
	arguments.add_argument("--table-names-file", type=Path)
	arguments.add_argument("--output", type=Path, default=Path("output/legacy-migration/stage5.json"))
	arguments.add_argument("--precision-report", type=Path, default=Path("output/legacy-migration/precision-inventory.json"))
	arguments.add_argument("--checkpoint", type=Path, default=Path("output/legacy-migration/audit-checkpoint.json"))
	arguments.add_argument("--decision-file", type=Path, default=Path("output/legacy-migration/stage5-decision.json"))
	arguments.add_argument("--evidence-path", type=Path, default=Path("output/legacy-migration/evidence"))
	arguments.add_argument("--attestation")
	arguments.add_argument("--snapshot-file", type=Path)
	arguments.add_argument("--cutoff", type=str)
	arguments.add_argument("--start-date", type=_date)
	arguments.add_argument("--end-date", type=_date)
	arguments.add_argument("--apply", action="store_true")
	return arguments


def _write(path: Path, value: object) -> None:
	atomic_write(path, encode_json(value))


def _source_inventory(arguments: argparse.Namespace):
	root = resolve_cli_path(arguments.data_root, "data")
	return root, scan_archive(root, arguments.start_date, arguments.end_date)


def _precision_inventory(root: Path, path: Path) -> ArchiveInventory | None:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
		if payload.get("operation") != "archive_precision_inventory" or not isinstance(payload.get("files"), list):
			return None
		files = [ArchiveFileInventory(item["path"], item["session"], item["file_sha256"], item["valid"], item["record_count"], item.get("records_id"), tuple(item.get("keys", ())), item.get("finding")) for item in payload["files"]]
		return ArchiveInventory(root, files=files, keys=set(payload.get("archive_keys", ())), key_dates={key: tuple(value) for key, value in payload.get("archive_key_dates", {}).items()})
	except (OSError, UnicodeError, TypeError, ValueError, KeyError, json.JSONDecodeError):
		return None


def _run(arguments: argparse.Namespace) -> dict[str, object]:
	if arguments.apply:
		raise ValueError("Stage 5 is audit-only; production apply is gated by Stage 11 and Stage 12")
	root = resolve_cli_path(arguments.data_root, "data")
	if arguments.mode == "audit":
		if arguments.decision_file is not None and not arguments.decision_file.exists():
			_write(arguments.decision_file, {"schema_version": 1, "mode": "audit", "production_apply": False, "provisional": True})
		return audit_archive(root, arguments.dbinfo_file, start=arguments.start_date, end=arguments.end_date, checkpoint_path=arguments.checkpoint, decision_path=arguments.decision_file, evidence_path=arguments.evidence_path if arguments.evidence_path.exists() else None, archive_inventory=_precision_inventory(root, arguments.precision_report) if arguments.precision_report.exists() else None)
	if arguments.mode in {"identity", "evidence", "snapshot"} and arguments.precision_report.exists():
		archive = _precision_inventory(root, arguments.precision_report)
		if archive is None:
			archive = scan_archive_keys(root, arguments.start_date, arguments.end_date) if arguments.mode == "identity" else _source_inventory(arguments)[1]
	else:
		_, archive = _source_inventory(arguments)
	if arguments.mode == "precision":
		return inventory_payload(archive)
	if arguments.mode == "identity":
		existing = ()
		names = load_table_names(arguments.table_names_file)
		if arguments.dbinfo_file is not None:
			existing = tuple(TickerIdentity(identifier, label) for identifier, label in read_database_tickers(arguments.dbinfo_file))
		return identity_report(archive, tuple(existing), names)
	if arguments.mode == "evidence":
		if not arguments.attestation or not arguments.attestation.strip():
			raise ValueError("--attestation is required for evidence materialization")
		store = ArchiveEvidenceStore(arguments.evidence_path)
		paths: list[str] = []
		for item in archive.valid_files:
			paths.append(str(store.save(evidence_for_file(ARCHIVE_SOURCE, arguments.attestation, item))))
		return {"schema_version": 1, "operation": "archive_evidence_materialization", "source": {"provider": ARCHIVE_SOURCE.provider, "endpoint": ARCHIVE_SOURCE.endpoint, "market": ARCHIVE_SOURCE.market, "universe": ARCHIVE_SOURCE.universe, "adjustment": ARCHIVE_SOURCE.adjustment.value, "interval": ARCHIVE_SOURCE.interval.value}, "file_count": len(paths), "invalid_file_count": len(archive.invalid_files), "evidence_root": str(arguments.evidence_path.resolve()), "evidence_count": len(paths), "attestation_id": evidence_for_file(ARCHIVE_SOURCE, arguments.attestation, archive.valid_files[0]).attestation_id if paths else None, "invalid_files": [item.__dict__ for item in archive.invalid_files]}
	if arguments.snapshot_file is None or not arguments.cutoff:
		raise ValueError("--snapshot-file and --cutoff are required for snapshot mode")
	try:
		cutoff = datetime.fromisoformat(arguments.cutoff)
	except ValueError:
		raise ValueError("Snapshot cutoff must be an ISO timestamp") from None
	snapshot = load_archive_snapshot(arguments.snapshot_file, ARCHIVE_SOURCE, ArchiveEvidenceStore(arguments.evidence_path), cutoff)
	return {"schema_version": 1, "operation": "offline_archive_snapshot", "provenance_kind": snapshot.provenance_kind, "source": {"provider": ARCHIVE_SOURCE.provider, "endpoint": ARCHIVE_SOURCE.endpoint, "market": ARCHIVE_SOURCE.market, "universe": ARCHIVE_SOURCE.universe, "adjustment": ARCHIVE_SOURCE.adjustment.value, "interval": ARCHIVE_SOURCE.interval.value}, "session": snapshot.session, "cutoff": snapshot.cutoff.isoformat(), "record_count": len(snapshot.records), "records_id": snapshot.evidence.canonical_records_id, "evidence_id": snapshot.evidence.evidence_id}


def main(argv: Sequence[str] | None = None) -> int:
	arguments = parser().parse_args(argv)
	try:
		result = _run(arguments)
		_write(arguments.output, result)
		successful = result.get("matches", True) if arguments.mode == "audit" else True
		print(json.dumps({"successful": successful, "mode": arguments.mode, "report": str(arguments.output)}))
		return 0 if successful else 2
	except Exception:
		print(json.dumps({"successful": False, "error": "Legacy migration Stage 5 operation could not complete"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
