from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from stockguidance.config.source import SourceIdentity
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.legacy_migration.precision import ArchiveFileInventory


EVIDENCE_SCHEMA_VERSION = 1
ARCHIVE_PROVENANCE = "operator_attested_retained_archive"


def _identity(value: object) -> SourceIdentity:
	if not isinstance(value, dict):
		raise ValueError("Archive evidence source identity must be an object")
	from stockguidance.config.source import PriceAdjustment
	from stockguidance.market_data.intervals import ObservationInterval
	try:
		return SourceIdentity(value["provider"], value["endpoint"], value["market"], value["universe"], PriceAdjustment(value["adjustment"]), ObservationInterval(value["interval"]))
	except (KeyError, TypeError, ValueError):
		raise ValueError("Invalid archive evidence source identity") from None


def _hash(value: object, name: str) -> str:
	if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
		raise ValueError(f"Invalid archive evidence {name}")
	return value


@dataclass(frozen=True)
class ArchiveEvidence:
	source: SourceIdentity
	attestation_id: str
	original_path: str
	original_file_sha256: str
	canonical_records_id: str
	record_count: int
	validated_at: datetime
	original_network_received_at: datetime | None = None
	provenance_kind: str = ARCHIVE_PROVENANCE

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("Archive evidence requires a source identity")
		_hash(self.attestation_id, "attestation ID")
		if not isinstance(self.original_path, str) or not self.original_path or "\x00" in self.original_path:
			raise ValueError("Archive evidence requires an original path")
		_hash(self.original_file_sha256, "file hash")
		_hash(self.canonical_records_id, "records ID")
		if type(self.record_count) is not int or self.record_count <= 0:
			raise ValueError("Archive evidence record count must be positive")
		if self.validated_at.tzinfo is None or self.validated_at.utcoffset() is None:
			raise ValueError("Archive evidence validation time must be timezone-aware")
		if self.original_network_received_at is not None and (self.original_network_received_at.tzinfo is None or self.original_network_received_at.utcoffset() is None):
			raise ValueError("Archive evidence receipt time must be timezone-aware")
		if self.provenance_kind != ARCHIVE_PROVENANCE:
			raise ValueError("Unsupported archive evidence provenance")

	@property
	def evidence_id(self) -> str:
		return content_id(self.payload())

	def payload(self) -> dict[str, object]:
		return {
			"schema_version": EVIDENCE_SCHEMA_VERSION,
			"source": asdict(self.source),
			"provenance_kind": self.provenance_kind,
			"attestation_id": self.attestation_id,
			"original_path": self.original_path,
			"original_file_sha256": self.original_file_sha256,
			"canonical_records_id": self.canonical_records_id,
			"record_count": self.record_count,
			"validated_at": self.validated_at.isoformat(),
			"original_network_received_at": None if self.original_network_received_at is None else self.original_network_received_at.isoformat(),
		}

	@classmethod
	def from_payload(cls, value: object) -> "ArchiveEvidence":
		if not isinstance(value, dict) or set(value) != {"schema_version", "source", "provenance_kind", "attestation_id", "original_path", "original_file_sha256", "canonical_records_id", "record_count", "validated_at", "original_network_received_at"}:
			raise ValueError("Archive evidence fields are incomplete or contain network-response metadata")
		if value["schema_version"] != EVIDENCE_SCHEMA_VERSION:
			raise ValueError("Unsupported archive evidence schema version")
		try:
			return cls(_identity(value["source"]), value["attestation_id"], value["original_path"], value["original_file_sha256"], value["canonical_records_id"], value["record_count"], datetime.fromisoformat(value["validated_at"]), None if value["original_network_received_at"] is None else datetime.fromisoformat(value["original_network_received_at"]), value["provenance_kind"])
		except (KeyError, TypeError, ValueError):
			raise ValueError("Invalid archive evidence payload") from None


def evidence_for_file(source: SourceIdentity, attestation: str, item: ArchiveFileInventory, validated_at: datetime | None = None) -> ArchiveEvidence:
	if not item.valid or item.records_id is None:
		raise ValueError("Invalid archive files cannot receive adoption evidence")
	if not isinstance(attestation, str) or not attestation.strip():
		raise ValueError("Operator attestation is required")
	return ArchiveEvidence(source, content_id(attestation.strip()), item.path, item.file_sha256, item.records_id, item.record_count, validated_at or datetime.now(timezone.utc))


class ArchiveEvidenceStore:
	def __init__(self, root: Path) -> None:
		self.root = root

	def path_for(self, evidence: ArchiveEvidence) -> Path:
		return self.root / evidence.source.provider / evidence.source.adjustment.value / f"{Path(evidence.original_path).stem}.json"

	def save(self, evidence: ArchiveEvidence) -> Path:
		path = self.path_for(evidence)
		atomic_write(path, encode_json(evidence.payload()))
		return path

	def load(self, source: SourceIdentity, session: str) -> ArchiveEvidence:
		path = self.root / source.provider / source.adjustment.value / f"{session}.json"
		try:
			return ArchiveEvidence.from_payload(json.loads(path.read_text(encoding="utf-8")))
		except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
			raise ValueError("Invalid or missing archive evidence") from None

	def verify(self, source: SourceIdentity, session: str, item: ArchiveFileInventory) -> ArchiveEvidence:
		evidence = self.load(source, session)
		if evidence.source != source or evidence.original_file_sha256 != item.file_sha256 or evidence.canonical_records_id != item.records_id or evidence.record_count != item.record_count:
			raise ValueError("Archive evidence does not match the retained file")
		return evidence
