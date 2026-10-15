from dataclasses import asdict
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
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
		identity = json.dumps(asdict(source), sort_keys=True, separators=(",", ":"))
		return self.directory / f"{sha256(identity.encode('utf-8')).hexdigest()}.json"

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
		path = self.path_for(source)
		try:
			with path.open(encoding="utf-8") as input_file:
				value: object = json.load(input_file)
		except FileNotFoundError:
			return None
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
