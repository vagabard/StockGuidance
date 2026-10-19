from dataclasses import asdict
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.history import DiscoveryMetadata, HistoryBoundary, HistoryProvenance
from stockguidance.market_data.identity import source_key
from stockguidance.market_data.intervals import ObservationInterval


def _mapping(value: object) -> dict[str, object]:
	if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
		raise ValueError("History metadata must contain JSON objects")
	return dict(value)


def _text(payload: dict[str, object], field: str) -> str:
	value = payload[field]
	if not isinstance(value, str):
		raise ValueError(f"History metadata {field} must be a string")
	return value


class JsonHistoryStore:
	"""Atomic, versioned metadata keyed by non-secret provider request identity."""

	def __init__(self, directory: Path) -> None:
		self.directory = directory

	def path_for(self, source: SourceIdentity) -> Path:
		return self.directory / f"{source_key(source)}.json"

	def _legacy_path_for(self, source: SourceIdentity) -> Path:
		identity = json.dumps(asdict(source), sort_keys=True, separators=(",", ":"))
		return self.directory / f"{sha256(identity.encode('utf-8')).hexdigest()}.json"

	def discovery_path_for(self, source: SourceIdentity) -> Path:
		return self.path_for(source).with_name(self.path_for(source).stem + ".discovery.json")

	def save(self, boundary: HistoryBoundary) -> None:
		payload = {
			"schema_version": 1, "source": asdict(boundary.source),
			"calendar_id": boundary.calendar_id, "history_start": boundary.history_start.isoformat(),
			"provenance": boundary.provenance.value, "evidence": boundary.evidence,
			"established_at": boundary.established_at.isoformat(),
		}
		self.directory.mkdir(parents=True, exist_ok=True)
		temporary: Path | None = None
		try:
			with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as output:
				temporary = Path(output.name)
				json.dump(payload, output, sort_keys=True)
				output.write("\n")
				output.flush()
				os.fsync(output.fileno())
			os.replace(temporary, self.path_for(boundary.source))
		finally:
			if temporary is not None:
				temporary.unlink(missing_ok=True)

	def load(self, source: SourceIdentity) -> HistoryBoundary | None:
		path = next((candidate for candidate in (self.path_for(source), self._legacy_path_for(source)) if candidate.exists()), None)
		if path is None:
			return None
		with path.open(encoding="utf-8") as input_file:
			value: object = json.load(input_file)
		try:
			payload = _mapping(value)
			if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
				raise ValueError("Unsupported history schema version")
			scope = _mapping(payload["source"])
			stored_source = SourceIdentity(
				_text(scope, "provider"), _text(scope, "endpoint"), _text(scope, "market"), _text(scope, "universe"),
				PriceAdjustment(_text(scope, "adjustment")), ObservationInterval(_text(scope, "interval")),
			)
			if stored_source != source:
				raise ValueError("Stored history source identity does not match its key")
			return HistoryBoundary(
				stored_source, _text(payload, "calendar_id"), date.fromisoformat(_text(payload, "history_start")),
				HistoryProvenance(_text(payload, "provenance")), _text(payload, "evidence"),
				datetime.fromisoformat(_text(payload, "established_at")),
			)
		except (KeyError, TypeError, ValueError) as error:
			raise ValueError(f"Invalid history metadata at {path.name}") from error

	def save_discovery_metadata(self, metadata: DiscoveryMetadata) -> None:
		payload = {
			"schema_version": metadata.schema_version, "source": asdict(metadata.source), "calendar_id": metadata.calendar_id,
			"recorded_at": metadata.recorded_at.isoformat(), "observed_sessions": [item.isoformat() for item in metadata.observed_sessions],
			"verified_sessions": [item.isoformat() for item in metadata.verified_sessions], "holes": [item.isoformat() for item in metadata.holes],
			"provider": {
				"state": metadata.provider_state, "checked_at": metadata.provider_checked_at.isoformat(), "provenance": metadata.provider_provenance,
				"earliest": None if metadata.provider_earliest is None else metadata.provider_earliest.isoformat(),
				"latest": None if metadata.provider_latest is None else metadata.provider_latest.isoformat(),
				"freshness_seconds": None if metadata.provider_freshness is None else metadata.provider_freshness.total_seconds(),
				"earliest_observed_success": None if metadata.provider_earliest_observed_success is None else metadata.provider_earliest_observed_success.isoformat(),
			},
		}
		atomic_write(self.discovery_path_for(metadata.source), json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")

	def load_discovery_metadata(self, source: SourceIdentity) -> DiscoveryMetadata | None:
		path = self.discovery_path_for(source)
		try:
			value: object = json.loads(path.read_bytes())
		except FileNotFoundError:
			return None
		try:
			payload = _mapping(value)
			if type(payload.get("schema_version")) is not int or payload["schema_version"] != 2:
				raise ValueError("Unsupported discovery metadata version")
			stored_source = SourceIdentity(
				_text(_mapping(payload["source"]), "provider"), _text(_mapping(payload["source"]), "endpoint"), _text(_mapping(payload["source"]), "market"), _text(_mapping(payload["source"]), "universe"),
				PriceAdjustment(_text(_mapping(payload["source"]), "adjustment")), ObservationInterval(_text(_mapping(payload["source"]), "interval")),
			)
			if stored_source != source:
				raise ValueError("Stored discovery source identity does not match its key")
			def dates(field: str) -> tuple[date, ...]:
				values = payload[field]
				if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
					raise ValueError("Discovery session list must be an array")
				return tuple(sorted(date.fromisoformat(item) for item in values))
			provider = _mapping(payload["provider"])
			freshness = provider.get("freshness_seconds")
			if freshness is not None and (isinstance(freshness, bool) or not isinstance(freshness, (int, float))):
				raise ValueError("Invalid discovery freshness")
			return DiscoveryMetadata(
				stored_source, _text(payload, "calendar_id"), datetime.fromisoformat(_text(payload, "recorded_at")), dates("observed_sessions"), dates("verified_sessions"), dates("holes"),
				_text(provider, "state"), datetime.fromisoformat(_text(provider, "checked_at")), _text(provider, "provenance"),
				None if provider.get("earliest") is None else date.fromisoformat(_text(provider, "earliest")),
				None if provider.get("latest") is None else date.fromisoformat(_text(provider, "latest")),
				None if freshness is None else timedelta(seconds=float(freshness)),
				None if provider.get("earliest_observed_success") is None else date.fromisoformat(_text(provider, "earliest_observed_success")),
			)
		except (KeyError, TypeError, ValueError) as error:
			raise ValueError(f"Invalid discovery metadata at {path.name}") from error
