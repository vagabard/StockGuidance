from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from stockguidance.config.source import SourceIdentity
from stockguidance.legacy_migration.evidence import ArchiveEvidence, ArchiveEvidenceStore
from stockguidance.legacy_migration.precision import ExactArchiveBar, exact_records_id, read_exact_archive


@dataclass(frozen=True)
class ArchiveSnapshot:
	source: SourceIdentity
	session: str
	cutoff: datetime
	records: tuple[ExactArchiveBar, ...]
	evidence: ArchiveEvidence

	def __post_init__(self) -> None:
		if self.cutoff.tzinfo is None or self.cutoff.utcoffset() is None:
			raise ValueError("Archive snapshot cutoff must be timezone-aware")
		if self.evidence.source != self.source or self.evidence.original_network_received_at is not None:
			raise ValueError("Archive snapshot requires retained archive evidence")
		if self.evidence.validated_at > self.cutoff:
			raise ValueError("Archive evidence was validated after the snapshot cutoff")
		if exact_records_id(self.records) != self.evidence.canonical_records_id:
			raise ValueError("Archive snapshot records differ from evidence")

	@property
	def provenance_kind(self) -> str:
		return "adopted_archive_snapshot"


def load_archive_snapshot(path: Path, source: SourceIdentity, evidence_store: ArchiveEvidenceStore, cutoff: datetime) -> ArchiveSnapshot:
	if cutoff.tzinfo is None or cutoff.utcoffset() is None:
		raise ValueError("Archive snapshot cutoff must be timezone-aware")
	if cutoff > datetime.now(timezone.utc):
		raise ValueError("Archive snapshot cutoff cannot be in the future")
	records, file_hash = read_exact_archive(path)
	item = type("ArchiveFile", (), {"valid": True, "records_id": exact_records_id(records), "record_count": len(records), "file_sha256": file_hash})()
	evidence = evidence_store.verify(source, path.stem, item)
	return ArchiveSnapshot(source, path.stem, cutoff, records, evidence)
