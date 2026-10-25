from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any, cast

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import SourceConfig, SourceIdentity
from stockguidance.calendar.sessions import TradingSession
from stockguidance.market_data.archive import FIELDS, archive_content, archive_path, decode_archive, unique_object
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.canonical_store import CanonicalArchiveStore
from stockguidance.market_data.consolidation import archive_rows, file_id, normalize, response_rows
from stockguidance.market_data.identity import decode_source, encode_json
from stockguidance.market_data.manifests import decode_response
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState
from stockguidance.market_data.records import records_id


def checked_path(path: Path, root: Path) -> Path:
	root = root.absolute()
	path = path.absolute()
	if not path.is_relative_to(root) or path == root:
		raise ValueError(f"Path escapes cleanup root: {path}")
	for component in (root, *reversed(path.parents), path):
		if component.is_symlink() or component.is_junction():
			raise ValueError(f"Cleanup does not follow links: {component}")
	if not path.resolve().is_relative_to(root.resolve()):
		raise ValueError(f"Resolved path escapes cleanup root: {path}")
	return path


@dataclass
class LegacyCopy:
	path: Path
	manifest: Path | None = None
	day: str | None = None
	problem: str | None = None


def inventory(root: Path, *, quarantined: bool = False) -> list[LegacyCopy]:
	base = root / "market_summary_conflicts" if quarantined else root
	copies = []
	referenced = set()
	for manifest in sorted((base / "market_data").glob("*/*.manifest.json")):
		checked_path(manifest, root)
		try:
			payload = json.loads(manifest.read_bytes(), object_pairs_hook=unique_object)
			day = date.fromisoformat(payload["session_date"]).isoformat()
			identity = payload["content_id"]
			if not isinstance(identity, str) or len(identity) != 64 or any(character not in "0123456789abcdef" for character in identity):
				raise ValueError("Invalid response checksum")
			response = checked_path(manifest.parent / "responses" / f"{identity}.json", root)
			referenced.add(response)
			copies.append(LegacyCopy(response, manifest, day))
		except (ValueError, KeyError, TypeError) as error:
			copies.append(LegacyCopy(manifest, problem=str(error)))
	for path in sorted((base / "daily-maintenance").glob("*/*.ndjson")):
		checked_path(path, root)
		try:
			copies.append(LegacyCopy(path, day=date.fromisoformat(path.stem).isoformat()))
		except ValueError as error:
			copies.append(LegacyCopy(path, problem=str(error)))
	for path in sorted((base / "market_data").glob("*/responses/*.json")):
		checked_path(path, root)
		if path not in referenced:
			copies.append(LegacyCopy(path, problem="Orphaned response: no source manifest"))
	references = Counter(copy.path for copy in copies)
	for copy in copies:
		if references[copy.path] > 1:
			copy.problem = "Shared response reference requires explicit review"
	return copies


def equivalent(left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]]) -> bool:
	if any(set(row) - {"T", "t", *FIELDS} for row in (*left.values(), *right.values())):
		return False
	return left.keys() == right.keys() and all(
		normalize(left[ticker].get(field)) == normalize(right[ticker].get(field))
		for ticker in left for field in (*FIELDS, "t")
	)


class ArchiveCleanup:
	def __init__(self, source: SourceConfig, store: CanonicalArchiveStore, output: Path, *, apply: bool, progress: Callable[[str], None] | None = None) -> None:
		self.source, self.store, self.output, self.apply = source, store, output.absolute(), apply
		self.identity = cast(SourceIdentity, source.identity)
		self.progress = progress
		self.root = source.data_root.absolute()
		if self.output.resolve().is_relative_to(self.root.resolve()):
			raise ValueError("Cleanup reports and journals must be outside data")
		checked_path(self.root / "market_summary_conflicts", self.root)
		self.calendars: dict[int, ExchangeCalendar] = {}
		self.totals: Counter[str] = Counter()
		self.unresolved: list[dict[str, Any]] = []
		self.preview: dict[str, DownloadedSession] = {}
		self.prune: set[Path] = set()
		self.deleted: dict[str, dict[str, Any]] = {}
		self.output.mkdir(parents=True, exist_ok=True)
		journal = self.output / "cleanup-journal.jsonl"
		if journal.exists():
			with journal.open("rb") as stream:
				for line in stream:
					try:
						event = json.loads(line)
						if event.get("applied") is True and event.get("operation") == "delete" and event.get("status") in {"prepared", "completed"}:
							self.deleted[event["path"]] = event
					except (ValueError, KeyError, AttributeError):
						continue

	def record(self, event: dict[str, Any]) -> None:
		with (self.output / "cleanup-journal.jsonl").open("a+b") as stream:
			if stream.tell():
				stream.seek(-1, os.SEEK_END)
				if stream.read(1) != b"\n":
					stream.write(b"\n")
			stream.write(encode_json({"applied": self.apply, **event}))
			stream.flush()
			os.fsync(stream.fileno())

	def session(self, day: str) -> TradingSession:
		session_date = date.fromisoformat(day)
		if session_date.year not in self.calendars:
			self.calendars[session_date.year] = ExchangeCalendar(self.source.calendar_id, session_date.replace(month=1, day=1), session_date.replace(month=12, day=31))
		return self.calendars[session_date.year].session(session_date)

	def verified(self, day: str) -> Any:
		if not self.apply and day in self.preview:
			from stockguidance.market_data.manifests import SourceManifest
			return SourceManifest(self.identity, self.preview[day], "preview")
		session = self.session(day)
		checked_path(archive_path(self.root, session.session_date), self.root)
		metadata = self.store.load_metadata(self.identity, session)
		if metadata is None or metadata.get("pending"):
			return None
		return self.store.load(self.identity, session)

	def download_rows(self, download: DownloadedSession) -> dict[str, dict[str, Any]]:
		rows = [json.loads(line, parse_float=Decimal) for line in archive_content(download).splitlines()]
		return {row["T"]: row for row in rows}

	def proven_download(self, copy: LegacyCopy, payload: dict[str, Any]) -> DownloadedSession:
		if copy.day is None:
			raise ValueError("Response has no session date")
		if payload.get("schema_version") != 1:
			raise ValueError("Unsupported manifest schema")
		if decode_source(payload["source"]) != self.source.identity or payload["calendar_id"] != self.source.calendar_id:
			raise ValueError("Adjusted, unknown, or different source configuration")
		if file_id(copy.path) != payload["content_id"]:
			raise ValueError("Response checksum differs from manifest")
		response = decode_response(json.loads(copy.path.read_bytes(), parse_float=Decimal, object_pairs_hook=unique_object))
		if response.state is not ResponseState.COMPLETE or response.has_more or response.http_status != 200:
			raise ValueError("Incomplete provider response")
		if payload["records_id"] != records_id(response.records) or payload["record_count"] != len(response.records):
			raise ValueError("Manifest record identity or count mismatch")
		if payload.get("record_keys") != [bar.ticker for bar in response.records]:
			raise ValueError("Manifest ticker inventory differs from response")
		count = dict(response.counts).get("resultsCount")
		if count != len(response.records) + len(response.bad_records) + len(response.ignored_placeholders):
			raise ValueError("Complete provider result count is not evidenced")
		download = DownloadedSession(self.session(copy.day), response, datetime.fromisoformat(payload["available_at"]))
		archive_content(download)
		if not equivalent(self.download_rows(download), response_rows(copy.path)):
			raise ValueError("Conversion would change source values")
		return download

	def decide(self, copy: LegacyCopy) -> tuple[str, str, dict[str, Any]]:
		if copy.problem or copy.day is None:
			return "quarantine", copy.problem or "Unknown session", {}
		payload = {} if copy.manifest is None else json.loads(copy.manifest.read_bytes(), object_pairs_hook=unique_object)
		canonical = self.verified(copy.day)
		if canonical is not None:
			if copy.manifest is not None and not copy.path.exists():
				previous = self.deleted.get(str(copy.path), {})
				if previous.get("sha256") == payload.get("content_id") and previous.get("canonical_sha256") == canonical.content_id and previous.get("provenance", {}).get("manifest") == payload:
					return "delete", "Resume journaled deletion; canonical checksum and manifest still match", payload
				return "quarantine", "Missing response without a matching verified deletion journal", payload
			metadata = self.store.load_metadata(self.identity, self.session(copy.day)) or {}
			approved = metadata.get("supersedes", [])
			if any(item["sha256"] == file_id(copy.path) and item.get("evidence") for item in approved):
				return "delete", "Explicitly approved replacement", payload
			rows = archive_rows(copy.path) if copy.manifest is None else response_rows(copy.path)
			canonical_rows = self.download_rows(canonical.download) if copy.day in self.preview else archive_rows(archive_path(self.root, date.fromisoformat(copy.day)))
			if equivalent(canonical_rows, rows):
				return "delete", "Complete numeric equivalence to verified canonical archive", payload
		if copy.manifest is not None:
			download = self.proven_download(copy, payload)
			destination = archive_path(self.root, download.session.session_date)
			checked_path(destination, self.root)
			if canonical is None and destination.exists():
				decode_archive(destination.read_bytes(), download.session)
				if not equivalent(archive_rows(destination), response_rows(copy.path)):
					return "quarantine", "Existing canonical source has unknown provenance and differs", payload
			if canonical is not None and download.available_at < canonical.download.available_at:
				return "delete", "Later evidenced complete unadjusted canonical response supersedes this source", payload
			if canonical is not None and download.available_at == canonical.download.available_at:
				return "quarantine", "Differing sources have the same evidenced availability", payload
			self.record({"operation": "publish", "date": copy.day, "source": str(copy.path), "sha256": file_id(copy.path), "provenance": payload})
			if self.apply:
				self.store.save_download(self.identity, download, evidence="Verified legacy response and source-bound manifest")
				if self.verified(copy.day) is None or not equivalent(archive_rows(destination), response_rows(copy.path)):
					raise ValueError("Published destination failed verification")
			else:
				self.preview[copy.day] = download
			self.totals["consolidated_dates"] += 1
			return "delete", "Verified complete unadjusted response published", payload
		return "quarantine", "No verified equivalent canonical data or unadjusted provenance", payload

	def transfer(self, path: Path, *, operation: str, reason: str, day: str | None, provenance: dict[str, Any], expected: str) -> None:
		checked_path(path, self.root)
		if not path.exists():
			return
		identity = file_id(path)
		if identity != expected:
			raise ValueError("Source changed during consolidation")
		size = path.stat().st_size
		quarantine = self.root / "market_summary_conflicts"
		if operation == "quarantine" and path.is_relative_to(quarantine):
			return
		destination = quarantine / path.relative_to(self.root)
		checked_path(destination, self.root)
		if operation == "quarantine" and destination.exists() and file_id(destination) != identity:
			raise ValueError(f"Quarantine destination collision; review retained files: {destination}")
		event = {"operation": operation, "path": str(path), "sha256": identity, "bytes": size, "date": day,
			"reason": reason, "provenance": provenance, "destination": str(destination) if operation == "quarantine" else None}
		canonical = self.verified(day) if operation == "delete" and day is not None else None
		event["canonical_sha256"] = None if canonical is None else canonical.content_id
		self.record({"status": "prepared", **event})
		if self.apply:
			if operation == "delete":
				current = None if day is None else self.verified(day)
				if current is None or current.content_id != event["canonical_sha256"]:
					raise ValueError("Deletion requires unchanged verified canonical coverage")
			if file_id(checked_path(path, self.root)) != identity:
				raise ValueError("Source changed before cleanup")
			if operation == "delete":
				path.unlink()
			else:
				destination.parent.mkdir(parents=True, exist_ok=True)
				checked_path(destination, self.root)
				if destination.exists():
					if file_id(destination) != identity:
						raise ValueError("Quarantine destination changed")
					path.unlink()
				else:
					path.rename(destination)
		self.record({"status": "completed" if self.apply else "preview", **event})
		self.prune.update(parent for parent in path.parents if parent != self.root and parent.is_relative_to(self.root))
		self.totals["deleted_files" if operation == "delete" else "quarantined_files"] += 1
		if operation == "delete":
			self.totals["reclaimed_bytes"] += size

	def process(self, copies: list[LegacyCopy], *, already_quarantined: bool = False) -> None:
		for index, copy in enumerate(sorted(copies, key=lambda item: (item.day or "", item.manifest is None, str(item.path))), start=1):
			if self.progress is not None and (index == 1 or index % 25 == 0 or index == len(copies)):
				self.progress(f"[consolidation] processing source {index}/{len(copies)}; date={copy.day or 'unknown'}")
			if not copy.path.exists() and copy.manifest is None:
				continue
			checksums = {path: file_id(checked_path(path, self.root)) for path in (copy.path, copy.manifest) if path is not None and path.exists()}
			try:
				operation, reason, provenance = self.decide(copy)
			except (ValueError, KeyError, TypeError, OSError, ArithmeticError) as error:
				operation, reason, provenance = "quarantine", str(error), {}
			if operation == "quarantine":
				self.unresolved.append({"date": copy.day, "path": str(copy.path), "reason": reason})
			retained = {"manifest": provenance}
			if copy.manifest is not None and copy.path.exists():
				try:
					envelope = json.loads(copy.path.read_bytes())
					retained["response_metadata"] = {key: value for key, value in envelope.items() if key not in {"records", "bad_records", "source_timestamps"}}
				except (ValueError, AttributeError):
					retained["response_metadata"] = {"unreadable": True}
			for path in (copy.path, copy.manifest):
				if already_quarantined and operation == "quarantine":
					continue
				if path is not None and operation == "delete" and not path.exists():
					self.prune.update(parent for parent in path.parents if parent != self.root and parent.is_relative_to(self.root))
				if path is not None and path in checksums:
					self.transfer(path, operation=operation, reason=reason, day=copy.day, provenance=retained, expected=checksums[path])

	def run(self, *, consolidate: bool, cleanup_conflicts: bool) -> dict[str, Any]:
		legacy_copies = inventory(self.root) if consolidate else []
		if consolidate:
			self.process(legacy_copies)
		if cleanup_conflicts:
			pending = {entry["path"] for entry in self.unresolved}
			self.unresolved.clear()
			if not self.apply:
				self.process([copy for copy in legacy_copies if str(copy.path) in pending], already_quarantined=True)
			self.process(inventory(self.root, quarantined=True))
		if self.apply:
			for directory in sorted(self.prune, key=lambda path: len(path.parts), reverse=True):
				checked_path(directory, self.root)
				if directory.is_dir() and not any(directory.iterdir()):
					directory.rmdir()
		report = {"applied": self.apply, "counts": dict(self.totals), "unresolved": self.unresolved,
			"journal": str(self.output / "cleanup-journal.jsonl"), "report": str(self.output / "cleanup-report.json")}
		atomic_write(self.output / "cleanup-report.json", encode_json(report))
		return report
