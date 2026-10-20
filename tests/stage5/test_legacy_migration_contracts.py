from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.database.table_names import load_table_names
from stockguidance.database.update_schema import update_schema_statements
from stockguidance.legacy_migration.audit import ARCHIVE_SOURCE, audit_archive
from stockguidance.legacy_migration.evidence import ArchiveEvidence, ArchiveEvidenceStore, evidence_for_file
from stockguidance.legacy_migration.identity import TickerIdentity, identity_report
from stockguidance.legacy_migration.precision import ArchiveFileInventory, ArchiveInventory, ExactArchiveBar, exact_records_id, read_exact_archive
from stockguidance.legacy_migration.snapshot import load_archive_snapshot


def write_archive(root: Path, session: str = "2024-07-01") -> Path:
	path = root / "market_summary" / "2024" / f"{session}.ndjson"
	path.parent.mkdir(parents=True)
	path.write_text(json.dumps({"T": "AAp", "o": "1702500000000000.000000", "h": "1702500000000000.000001", "l": "1702500000000000.000000", "c": "1702500000000000.000001", "v": "14982.400000", "t": 1719864000000}) + "\n", encoding="utf-8")
	return path


def test_exact_archive_contract_preserves_decimal_source_values(tmp_path: Path) -> None:
	path = write_archive(tmp_path)
	bars, file_hash = read_exact_archive(path)
	assert bars[0].ticker == "AAp"
	assert bars[0].volume == Decimal("14982.400000")
	assert bars[0].open == Decimal("1702500000000000.000000")
	assert len(exact_records_id(bars)) == 64
	assert len(file_hash) == 64


def test_identity_report_does_not_fold_exact_or_preferred_keys() -> None:
	archive = ArchiveInventory(Path("."), keys={"AAP", "AAp", "ABKPZ", "ABKpZ"}, key_dates={"AAP": ("2024-01-01", "2024-01-01"), "AAp": ("2024-01-01", "2024-01-01"), "ABKPZ": ("2024-01-01", "2024-01-01"), "ABKpZ": ("2024-01-01", "2024-01-01")})
	report = identity_report(archive, (TickerIdentity(7, "AAP"), TickerIdentity(8, "ABKPZ")), load_table_names())
	assert {item["label"] for item in report["exact_matches"]} == {"AAP", "ABKPZ"}
	assert {tuple(item["keys"]) for item in report["collisions"]} == {("AAP", "AAp"), ("ABKPZ", "ABKpZ")}
	assert "utf8mb4_bin" in report["migration_ddl"][0]


def test_evidence_rejects_network_response_fields_and_snapshot_binds_hash(tmp_path: Path) -> None:
	path = write_archive(tmp_path)
	bars, file_hash = read_exact_archive(path)
	item = ArchiveFileInventory(str(path), path.stem, file_hash, True, len(bars), exact_records_id(bars), ())
	evidence = evidence_for_file(ARCHIVE_SOURCE, "Operator attestation", item, datetime(2026, 10, 19, tzinfo=timezone.utc))
	with pytest.raises(ValueError):
		ArchiveEvidence.from_payload({**evidence.payload(), "http_status": 200})
	store = ArchiveEvidenceStore(tmp_path / "evidence")
	store.save(evidence)
	snapshot = load_archive_snapshot(path, ARCHIVE_SOURCE, store, datetime(2026, 10, 19, 1, tzinfo=timezone.utc))
	assert snapshot.provenance_kind == "adopted_archive_snapshot"
	assert snapshot.evidence.evidence_id == evidence.evidence_id


def test_audit_is_database_incomplete_without_apply_and_schema_is_case_sensitive(tmp_path: Path) -> None:
	path = write_archive(tmp_path)
	report = audit_archive(tmp_path, None)
	assert not report["complete"]
	assert report["invalid_archive_files"] == []
	assert any("utf8mb4_bin" in statement for statement in update_schema_statements(load_table_names()))
