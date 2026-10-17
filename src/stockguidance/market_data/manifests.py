from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceIdentity
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.history import EvidenceQuality, InventoryIssue, LocalCoverageInventory, LocalObservation
from stockguidance.market_data.identity import decode_source, encode_json, mapping, source_key, text_field
from stockguidance.market_data.providers.contracts import DownloadedSession, ProviderResponse, ResponseState
from stockguidance.market_data.records import bar_payload, decode_bar, records_id


class InvalidManifest(ValueError):
	pass


@dataclass(frozen=True)
class SourceManifest:
	source: SourceIdentity
	download: DownloadedSession
	content_id: str
	import_complete: bool = False
	derived_complete: bool = False


def response_payload(response: ProviderResponse) -> dict[str, object]:
	return {"state": response.state.value, "http_status": response.http_status, "provider_status": response.provider_status, "counts": dict(response.counts), "has_more": response.has_more, "evidence": response.evidence, "source_timestamps": dict(response.source_timestamps), "records": [bar_payload(bar) for bar in response.records]}


def decode_response(value: object) -> ProviderResponse:
	payload = mapping(value)
	rows, counts = payload["records"], mapping(payload["counts"])
	status, has_more, evidence = payload["http_status"], payload["has_more"], payload["evidence"]
	if not isinstance(rows, list) or type(status) is not int or type(has_more) is not bool or (evidence is not None and not isinstance(evidence, str)):
		raise ValueError("Invalid response envelope")
	if any(type(count) is not int for count in counts.values()):
		raise ValueError("Invalid count metadata")
	validated_counts = tuple((name, count) for name, count in counts.items() if isinstance(count, int))
	timestamps = mapping(payload["source_timestamps"])
	if any(type(timestamp) is not int for timestamp in timestamps.values()):
		raise ValueError("Invalid source timestamps")
	validated_timestamps = tuple((ticker, timestamp) for ticker, timestamp in timestamps.items() if isinstance(timestamp, int))
	return ProviderResponse(ResponseState(text_field(payload, "state")), tuple(decode_bar(row) for row in rows), status, text_field(payload, "provider_status"), validated_counts, has_more, evidence, validated_timestamps)


class ManifestStore:
	def __init__(self, data_root: Path) -> None:
		self.directory = data_root / "market_data"

	def inventory(self, source: SourceIdentity, calendar: SessionCalendar, *, start: date | None = None, end: date | None = None) -> LocalCoverageInventory:
		"""Read source-bound manifests once for a bounded calendar scope.

		The manifest payload supplies the session date; filenames are only directory
		entries and never establish coverage.
		"""
		calendar_id = calendar.calendar_id
		bounded_start = calendar.start if start is None else start
		bounded_end = calendar.end if end is None else end
		if bounded_start > bounded_end:
			return LocalCoverageInventory(source, calendar_id)
		loaded_start, loaded_end = max(bounded_start, calendar.start), min(bounded_end, calendar.end)
		if loaded_start > loaded_end:
			return LocalCoverageInventory(source, calendar_id)
		allowed = {session.session_date: session for session in calendar.sessions(loaded_start, loaded_end)}
		root = self.directory / source_key(source)
		if not root.exists():
			return LocalCoverageInventory(source, calendar_id)
		observations: list[LocalObservation] = []
		issues: list[InventoryIssue] = []
		seen_dates: set[date] = set()
		for path in sorted(root.glob("*.manifest.json")):
			session_date = None
			try:
				payload = mapping(json.loads(path.read_bytes()))
				session_date = date.fromisoformat(text_field(payload, "session_date"))
				if session_date not in allowed:
					continue
				if session_date in seen_dates:
					raise ValueError("Duplicate source manifest session")
				seen_dates.add(session_date)
				manifest = self.load(source, allowed[session_date])
				if manifest is None:
					raise ValueError("Source manifest disappeared during inventory")
				response = manifest.download.response
				quality = EvidenceQuality.VERIFIED if response.state in {ResponseState.COMPLETE, ResponseState.NO_TRADING} else EvidenceQuality.UNVERIFIED
				observations.append(LocalObservation(source, calendar_id, session_date, quality, response.evidence or f"Source manifest response state: {response.state.value}", True, manifest.download.available_at, manifest.content_id))
			except (OSError, TypeError, ValueError, KeyError) as error:
				if session_date is not None and session_date in allowed and not any(item.session_date == session_date for item in observations):
					observations.append(LocalObservation(source, calendar_id, session_date, EvidenceQuality.CORRUPT, "Source manifest or response content is corrupt", False))
				issues.append(InventoryIssue(calendar_id, "Source manifest could not establish source-bound evidence", session_date, path.name))
		return LocalCoverageInventory(source, calendar_id, tuple(observations), tuple(issues))

	def path_for(self, source: SourceIdentity, session: TradingSession) -> Path:
		return self.directory / source_key(source) / f"{session.session_date.isoformat()}.manifest.json"

	def _raw_path(self, source: SourceIdentity, identity: str) -> Path:
		if len(identity) != 64 or any(character not in "0123456789abcdef" for character in identity):
			raise ValueError("Invalid content identity")
		return self.directory / source_key(source) / "responses" / f"{identity}.json"

	def save_download(self, source: SourceIdentity, download: DownloadedSession) -> SourceManifest:
		content = encode_json(response_payload(download.response))
		identity = sha256(content).hexdigest()
		atomic_write(self._raw_path(source, identity), content)
		manifest = SourceManifest(source, download, identity)
		self.save(manifest)
		return manifest

	def save(self, manifest: SourceManifest) -> None:
		download = manifest.download
		payload = {
			"schema_version": 1, "source": asdict(manifest.source), "calendar_id": download.session.calendar_id,
			"session_date": download.session.session_date.isoformat(), "observed_at": download.session.closes_at.isoformat(),
			"available_at": download.available_at.isoformat(), "content_id": manifest.content_id,
			"record_keys": [bar.ticker for bar in download.response.records], "record_count": len(download.response.records),
			"records_id": records_id(download.response.records), "import_complete": manifest.import_complete, "derived_complete": manifest.derived_complete,
		}
		atomic_write(self.path_for(manifest.source, download.session), encode_json(payload))

	def mark_complete(self, manifest: SourceManifest) -> SourceManifest:
		complete = replace(manifest, import_complete=True, derived_complete=True)
		if complete != manifest:
			self.save(complete)
		return complete

	def mark_imported(self, manifest: SourceManifest) -> SourceManifest:
		imported = replace(manifest, import_complete=True)
		if imported != manifest:
			self.save(imported)
		return imported

	def load(self, source: SourceIdentity, session: TradingSession) -> SourceManifest | None:
		path = self.path_for(source, session)
		try:
			content = path.read_bytes()
		except FileNotFoundError:
			return None
		try:
			payload = mapping(json.loads(content))
			if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or decode_source(payload["source"]) != source or payload["calendar_id"] != session.calendar_id or payload["session_date"] != session.session_date.isoformat() or payload["observed_at"] != session.closes_at.isoformat():
				raise ValueError("Manifest scope mismatch")
			identity = text_field(payload, "content_id")
			raw = self._raw_path(source, identity).read_bytes()
			if sha256(raw).hexdigest() != identity:
				raise ValueError("Response content mismatch")
			response = decode_response(json.loads(raw))
			if payload["record_keys"] != [bar.ticker for bar in response.records] or type(payload["record_count"]) is not int or payload["record_count"] != len(response.records) or payload["records_id"] != records_id(response.records):
				raise ValueError("Source keys/counts mismatch")
			imported, derived = payload["import_complete"], payload["derived_complete"]
			if type(imported) is not bool or type(derived) is not bool or (derived and not imported):
				raise ValueError("Invalid completion flags")
			download = DownloadedSession(session, response, datetime.fromisoformat(text_field(payload, "available_at")))
			return SourceManifest(source, download, identity, imported, derived)
		except (ValueError, TypeError, KeyError, FileNotFoundError) as error:
			raise InvalidManifest(f"Invalid source evidence for {session.session_date}") from error
