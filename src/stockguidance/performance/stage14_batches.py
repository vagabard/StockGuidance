from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.performance.stage14 import Stage14Error, _json, _sha256, _write_json, _session_ids
from stockguidance.performance.stage12 import _connect, _manifest_names, _active_writers
from stockguidance.performance.stage13 import _database_revision


METADATA_COLUMNS = ("series_key", "date_id", "dependency_digest", "availability_digest", "actual_input_available", "completed_at", "row_count", "ready_count", "status_counts", "content_digest")


def _now() -> str:
	return datetime.now(timezone.utc).isoformat()


def migrate_dependencies(state: dict[str, Any], old: dict[str, Any], new: dict[str, Any], output: Path, revision: str) -> dict[str, Any]:
	if state.get("manifest_id") != old.get("manifest_id") or content_id({key: value for key, value in old.items() if key != "manifest_id"}) != old.get("manifest_id"):
		raise Stage14Error("Old manifest/checkpoint identity is invalid")
	if state.get("manifest_sha256") != _sha256(output.parent / "stage14/zscore-manifest.json"):
		raise Stage14Error("Old checkpoint does not bind its manifest")
	if state.get("database_revision_after") != revision:
		raise Stage14Error("Source revision changed before dependency repair")
	expected = deepcopy(old)
	files = [str(item["session"]) for item in old["files"]]
	positions = {session: index for index, session in enumerate(files)}
	for target in expected["targets"]:
		index = positions[str(target["session"])]
		if target["zscore_dependencies"] != files[max(0, index - 59):index + 1]:
			raise Stage14Error("Only the known 60-to-61 dependency correction can migrate automatically")
		target["zscore_dependencies"] = files[max(0, index - 60):index + 1]
	expected["zscore_population"]["dependency_sessions"] = 61
	expected.pop("manifest_id")
	if content_id(expected) != new["manifest_id"]:
		raise Stage14Error("Migration contains changes beyond the authorized dependency correction")
	history = output.parent / "stage14/history" / str(old["manifest_id"])
	history.mkdir(parents=True, exist_ok=True)
	for name, value in (("zscores.json", state), ("zscore-manifest.json", old)):
		path = history / name
		if path.exists() and _json(path) != value:
			raise Stage14Error("Historical checkpoint already exists with different contents")
		_write_json(path, value)
	result = deepcopy(state)
	result.update({"status": "partial", "production_ready": False, "manifest_id": new["manifest_id"], "manifest_sha256": sha256(encode_json(new)).hexdigest(), "chunks": [], "series": [], "coverage": {"requested_sessions": len(new["targets"]), "completed_sessions": 0, "completed_series": 0, "expected_series": len(new["targets"]) * 4}, "dependency_migration": {"from_manifest_id": old["manifest_id"], "history": str(history.resolve()), "previous_completed_sessions": sum(len(chunk["sessions"]) for chunk in state.get("chunks", ())), "policy": "Revalidate all four series; preserve matching materialization rows and replace only differing output rows", "status": "repair_pending"}})
	result["configuration"]["dependency_sessions"] = 61
	result["scope"]["maximum_dependency_count"] = max(len(target["zscore_dependencies"]) for target in new["targets"])
	result["scope"]["minimum_dependency_count"] = min(len(target["zscore_dependencies"]) for target in new["targets"])
	result["handoff"] = {"next": "resume Stage 14", "reason": "authorized dependency repair", "remaining_sessions": len(new["targets"]), "remaining_series": len(new["targets"]) * 4}
	_write_json(output, result)
	return result


class BatchJournal:
	def __init__(self, dbinfo_file: Path, directory: Path) -> None:
		self.dbinfo_file = dbinfo_file.resolve()
		self.directory = directory.resolve()
		self.path = self.directory / "batches.json"
		self.state: dict[str, Any] = {}
		self.checkpoints = self.directory
		self.manifest: dict[str, Any] = {}
		self.names: Any = None
		self.revision: str | None = None

	def initialize(self, manifest: dict[str, Any], report: dict[str, Any], checkpoints: Path, chunk_size: int) -> None:
		self.manifest = manifest
		self.names = _manifest_names(manifest)
		self.checkpoints = checkpoints
		self.revision = str(report["database_revision_after"])
		sessions = [str(item["session"]) for item in manifest["targets"]]
		self.directory.mkdir(parents=True, exist_ok=True)
		self.path = self.directory / f"batches-{manifest['manifest_id'][:12]}.json"
		if self.path.exists():
			self.state = _json(self.path)
			if self.state.get("manifest_id") != manifest["manifest_id"] or self.state.get("source_revision") != self.revision or self.state.get("chunk_size") != chunk_size:
				raise Stage14Error("Batch journal identity changed")
			if [batch["sessions"] for batch in self.state["batches"]] != [sessions[start:start + chunk_size] for start in range(0, len(sessions), chunk_size)]:
				raise Stage14Error("Batch journal session scope changed")
		else:
			completed = {int(item["index"]) for item in report.get("chunks", ())}
			self.state = {"schema_version": 1, "manifest_id": manifest["manifest_id"], "source_revision": self.revision, "chunk_size": chunk_size, "batches": [{"index": index, "sessions": sessions[start:start + chunk_size], "status": "complete" if index in completed else "pending", "attempts": []} for index, start in enumerate(range(0, len(sessions), chunk_size))]}
			self._save()

	def _save(self) -> None:
		self.state["updated_at"] = _now()
		_write_json(self.path, self.state)

	def _connection(self) -> Any:
		from stockguidance.database.configuration import load_database_configuration
		return _connect(self.dbinfo_file, load_database_configuration(self.dbinfo_file).database)

	def _guard(self, connection: Any) -> None:
		if _active_writers(connection) or _database_revision(connection, self.names) != self.revision:
			raise Stage14Error("Competing activity or changed source revision prevents batch recovery")

	def begin(self, index: int, sessions: tuple[str, ...]) -> None:
		batch = self.state["batches"][index]
		if batch["status"] != "pending" or tuple(batch["sessions"]) != sessions:
			raise Stage14Error("Batch is not pending or has a different session scope")
		attempt = {"id": uuid4().hex, "status": "in_progress", "phase": "saving_before_image", "started_at": _now()}
		batch["status"] = "in_progress"
		batch["attempts"].append(attempt)
		self._save()
		destination = self.directory / "before-images" / attempt["id"]
		connection = self._connection()
		try:
			self._guard(connection)
			backup = self._capture(connection, sessions, destination)
		finally:
			connection.rollback()
			connection.close()
		attempt.update({"phase": "prepared", "before_image": str(backup), "before_image_sha256": _sha256(backup)})
		self._save()

	def publishing(self) -> None:
		active = [batch for batch in self.state["batches"] if batch["status"] == "in_progress"]
		if len(active) != 1 or active[0]["attempts"][-1]["phase"] != "prepared":
			raise Stage14Error("Publication requires one fully backed-up prepared batch")
		active[0]["attempts"][-1]["phase"] = "publishing"
		self._save()

	def complete(self, index: int, report: dict[str, Any]) -> None:
		batch = self.state["batches"][index]
		chunk = next((chunk for chunk in report["chunks"] if chunk["index"] == index), None)
		if chunk is None or chunk["sessions"] != batch["sessions"]:
			raise Stage14Error("Cannot mark a batch complete without an accepted chunk")
		batch.update({"status": "complete", "completed_at": _now(), "java_report": chunk["java_report"], "java_report_sha256": _sha256(Path(chunk["java_report"]))})
		if batch["attempts"]:
			batch["attempts"][-1].update({"status": "complete", "phase": "accepted", "completed_at": _now()})
		self._save()

	def recover(self, report: dict[str, Any]) -> None:
		accepted = {int(chunk["index"]): chunk for chunk in report["chunks"]}
		for batch in self.state["batches"]:
			index = int(batch["index"])
			if batch["status"] == "complete":
				if index not in accepted:
					raise Stage14Error("Completed batch is missing from the durable report")
				if batch.get("java_report_sha256") and _sha256(Path(batch["java_report"])) != batch["java_report_sha256"]:
					raise Stage14Error("Completed batch evidence changed")
				continue
			if index in accepted:
				self.complete(index, report)
				continue
			if batch["status"] != "in_progress":
				continue
			attempt = batch["attempts"][-1]
			if attempt["phase"] in ("publishing", "rolling_back"):
				attempt["phase"] = "rolling_back"
				self._save()
				self._restore(attempt, tuple(batch["sessions"]))
			for session in batch["sessions"]:
				for family in ("price", "price_delta"):
					for window in (20, 60):
						(self.checkpoints / f"{session}-{family}-{window}.json").unlink(missing_ok=True)
			attempt.update({"status": "rolled_back", "phase": "restored", "restored_at": _now()})
			batch["status"] = "pending"
			self._save()

	def _capture(self, connection: Any, sessions: tuple[str, ...], destination: Path) -> Path:
		destination.mkdir(parents=True, exist_ok=True)
		date_ids = _session_ids(connection, self.names, sessions)
		identifiers = tuple(date_ids.values())
		parameters = ",".join("%s" for _ in identifiers)
		files = []
		for window, tables in self.names.resolve_zscore_tables((20, 60)).items():
			for table in (tables.price, tables.price_delta):
				path = destination / f"{table}.jsonl.gz"
				count = 0
				cursor = connection.cursor(buffered=False)
				try:
					cursor.execute(f"SELECT date_id,ticker_id,zscore,status FROM {quote_identifier(table)} WHERE date_id IN ({parameters}) ORDER BY date_id,ticker_id", identifiers)
					with path.with_suffix(".tmp").open("wb") as raw:
						with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as compressed:
							for row in cursor:
								compressed.write((json.dumps(list(row), allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8"))
								count += 1
						raw.flush()
						os.fsync(raw.fileno())
					os.replace(path.with_suffix(".tmp"), path)
				finally:
					cursor.close()
				files.append({"table": table, "path": str(path), "sha256": _sha256(path), "rows": count})
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT series_key FROM {quote_identifier(self.names.zscore_series)} WHERE family IN ('price','price_delta') AND `window` IN (20,60)")
			series = [str(row[0]) for row in cursor.fetchall()]
			if len(series) != 4:
				raise Stage14Error("Expected four unambiguous z-score series for rollback")
			series_parameters = ",".join("%s" for _ in series)
			cursor.execute(f"SELECT {','.join(quote_identifier(column) for column in METADATA_COLUMNS)} FROM {quote_identifier(self.names.zscore_sessions)} WHERE date_id IN ({parameters}) AND series_key IN ({series_parameters}) ORDER BY series_key,date_id", identifiers + tuple(series))
			metadata = [list(row) for row in cursor.fetchall()]
		finally:
			cursor.close()
		backup = destination / "before.json"
		_write_json(backup, {"schema_version": 1, "manifest_id": self.manifest["manifest_id"], "source_revision": self.revision, "sessions": list(sessions), "date_ids": list(identifiers), "series_keys": series, "files": files, "metadata": metadata})
		return backup

	def _restore(self, attempt: dict[str, Any], sessions: tuple[str, ...]) -> None:
		path = Path(attempt["before_image"])
		if _sha256(path) != attempt["before_image_sha256"]:
			raise Stage14Error("Rollback before-image identity changed")
		backup = _json(path)
		if backup["manifest_id"] != self.manifest["manifest_id"] or backup["source_revision"] != self.revision or tuple(backup["sessions"]) != sessions:
			raise Stage14Error("Rollback before-image belongs to another batch")
		allowed = {table for tables in self.names.resolve_zscore_tables((20, 60)).values() for table in (tables.price, tables.price_delta)}
		if {entry["table"] for entry in backup["files"]} != allowed or len(backup["files"]) != 4:
			raise Stage14Error("Rollback table scope changed")
		for entry in backup["files"]:
			if _sha256(Path(entry["path"])) != entry["sha256"]:
				raise Stage14Error("Rollback rows are missing or changed")
		if any(len(row) != len(METADATA_COLUMNS) or row[0] not in backup["series_keys"] or row[1] not in backup["date_ids"] for row in backup["metadata"]):
			raise Stage14Error("Rollback metadata lies outside its batch scope")
		connection = self._connection()
		try:
			self._guard(connection)
			if list(_session_ids(connection, self.names, sessions).values()) != backup["date_ids"]:
				raise Stage14Error("Rollback date mappings changed")
			parameters = ",".join("%s" for _ in backup["date_ids"])
			cursor = connection.cursor()
			try:
				for entry in backup["files"]:
					table = quote_identifier(entry["table"])
					cursor.execute(f"DELETE FROM {table} WHERE date_id IN ({parameters})", tuple(backup["date_ids"]))
					rows = []
					count = 0
					with gzip.open(entry["path"], "rt", encoding="utf-8") as stream:
						for line in stream:
							row = json.loads(line)
							if len(row) != 4 or row[0] not in backup["date_ids"]:
								raise Stage14Error("Rollback row lies outside its date scope")
							rows.append(tuple(row))
							count += 1
							if len(rows) == 1000:
								cursor.executemany(f"INSERT INTO {table} (date_id,ticker_id,zscore,status) VALUES (%s,%s,%s,%s)", rows)
								rows = []
						if rows:
							cursor.executemany(f"INSERT INTO {table} (date_id,ticker_id,zscore,status) VALUES (%s,%s,%s,%s)", rows)
					if count != entry["rows"]:
						raise Stage14Error("Rollback row count changed")
				series_parameters = ",".join("%s" for _ in backup["series_keys"])
				cursor.execute(f"DELETE FROM {quote_identifier(self.names.zscore_sessions)} WHERE date_id IN ({parameters}) AND series_key IN ({series_parameters})", tuple(backup["date_ids"]) + tuple(backup["series_keys"]))
				if backup["metadata"]:
					cursor.executemany(f"INSERT INTO {quote_identifier(self.names.zscore_sessions)} ({','.join(quote_identifier(column) for column in METADATA_COLUMNS)}) VALUES ({','.join('%s' for _ in METADATA_COLUMNS)})", backup["metadata"])
				connection.commit()
			finally:
				cursor.close()
		except BaseException:
			connection.rollback()
			raise
		finally:
			connection.close()
