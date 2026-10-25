from dataclasses import asdict, replace
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.database.repositories.archive_catalog import ArchiveCatalog
from stockguidance.market_data.archive import archive_content, archive_path, decode_archive
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.history import EvidenceQuality, LocalCoverageInventory, LocalObservation
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore, SourceManifest
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.market_data.records import records_id
from stockguidance.config.source import SourceConfig
from stockguidance.database.write_connections import WriteConnectionFactory


class CanonicalArchiveStore(ManifestStore):
	def __init__(self, data_root: Path, catalog: ArchiveCatalog) -> None:
		super().__init__(data_root)
		self.data_root, self.catalog = data_root, catalog
		self._metadata: dict[date, dict[str, Any]] | None = None

	def path_for(self, source: SourceIdentity, session: TradingSession) -> Path:
		self._require_source(source)
		return archive_path(self.data_root, session.session_date)

	def _require_source(self, source: SourceIdentity) -> None:
		if source != self.catalog.source.identity or source.adjustment is not PriceAdjustment.UNADJUSTED:
			raise ValueError("Canonical market_summary requires the configured unadjusted source; migrate adjusted data before cutover")

	def inventory(self, source: SourceIdentity, calendar: SessionCalendar, *, start: date | None = None, end: date | None = None) -> LocalCoverageInventory:
		self._require_source(source)
		self._metadata = self.catalog.inventory()
		observations = tuple(LocalObservation(source, calendar.calendar_id, session_date, EvidenceQuality.UNVERIFIED,
			"Canonical archive catalog; content verification is deferred until requested", True,
			datetime.fromisoformat(metadata["available_at"]), metadata["content_id"])
			for session_date, metadata in self._metadata.items()
			if (start is None or session_date >= start) and (end is None or session_date <= end) and not metadata.get("pending"))
		return LocalCoverageInventory(source, calendar.calendar_id, observations)

	def load_metadata(self, source: SourceIdentity, session: TradingSession) -> dict[str, Any] | None:
		self._require_source(source)
		metadata = self.catalog.get(session.session_date) if self._metadata is None else self._metadata.get(session.session_date)
		if metadata is not None and (metadata.get("source") != asdict(source) or metadata.get("calendar_id") != session.calendar_id):
			raise InvalidManifest("Canonical archive metadata scope mismatch")
		return metadata

	def save_download(self, source: SourceIdentity, download: DownloadedSession, *, evidence: str = "Complete provider response validated during download") -> SourceManifest:
		self._require_source(source)
		content = archive_content(download)
		identity = sha256(content).hexdigest()
		metadata = {"source": asdict(source), "calendar_id": download.session.calendar_id,
			"session_date": download.session.session_date.isoformat(), "available_at": download.available_at.isoformat(),
			"content_id": identity, "records_id": records_id(download.response.records), "pending": True, "evidence": evidence}
		path = self.path_for(source, download.session)
		if path.exists():
			previous = path.read_bytes()
			if previous != content:
				atomic_write(self.data_root / "archive-backup" / sha256(previous).hexdigest() / path.name, previous)
		self.catalog.put(download.session.session_date, metadata)
		if self._metadata is not None:
			self._metadata[download.session.session_date] = metadata
		atomic_write(path, content)
		metadata = {**metadata, "pending": False}
		self.catalog.put(download.session.session_date, metadata)
		if self._metadata is not None:
			self._metadata[download.session.session_date] = metadata
		return SourceManifest(source, download, identity)

	def load(self, source: SourceIdentity, session: TradingSession) -> SourceManifest | None:
		metadata = self.load_metadata(source, session)
		if metadata is None:
			return None
		try:
			content = self.path_for(source, session).read_bytes()
			if sha256(content).hexdigest() != metadata["content_id"]:
				raise ValueError("Archive publication is incomplete or content has changed")
			response = decode_archive(content, session)
			if records_id(response.records) != metadata["records_id"]:
				raise ValueError("Archive record identity differs from its catalog")
			return SourceManifest(source, DownloadedSession(session, response, datetime.fromisoformat(metadata["available_at"])), metadata["content_id"])
		except (OSError, ValueError, KeyError, TypeError) as error:
			raise InvalidManifest(f"Invalid canonical archive for {session.session_date}") from error

	def save(self, manifest: SourceManifest) -> None:
		self._require_source(manifest.source)

	def mark_complete(self, manifest: SourceManifest) -> SourceManifest:
		return replace(manifest, import_complete=True, derived_complete=True)

	def mark_imported(self, manifest: SourceManifest) -> SourceManifest:
		metadata = self.load_metadata(manifest.source, manifest.download.session)
		if metadata is not None and metadata.get("pending") and metadata["content_id"] == manifest.content_id:
			metadata = {**metadata, "pending": False}
			self.catalog.put(manifest.download.session.session_date, metadata)
			if self._metadata is not None:
				self._metadata[manifest.download.session.session_date] = metadata
		return replace(manifest, import_complete=True)


def archive_store(source: SourceConfig, connect: WriteConnectionFactory) -> ManifestStore:
	if source.identity.adjustment is PriceAdjustment.ADJUSTED:
		return ManifestStore(source.data_root)
	return CanonicalArchiveStore(source.data_root, ArchiveCatalog(source, connect))
