from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from time import perf_counter
from typing import Any, Iterable, cast

from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.legacy_migration.audit import ARCHIVE_SOURCE
from stockguidance.legacy_migration.evidence import ArchiveEvidenceStore
from stockguidance.legacy_migration.precision import ArchiveFileInventory
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.performance.stage12 import (
	MIGRATION_COMPLETION,
	MIGRATION_RECEIPTS,
	PROVENANCE_KIND,
	_adopt_raw_session,
	_active_writers,
	_connect,
	_manifest_names,
	_mapping_digest,
)


STAGE13_SCHEMA_VERSION = 1
RAW_SERIES = "raw-adoption-v1"
DERIVED_SERIES = "legacy-open-to-close-v1"


class Stage13Error(ValueError):
	pass


@dataclass(frozen=True)
class Stage13Context:
	decision_path: Path
	decision: dict[str, Any]
	base_manifest_path: Path
	base_manifest: dict[str, Any]
	audit_path: Path
	precision_path: Path
	stage11b_path: Path
	stage12_path: Path
	evidence_path: Path
	cutoff: datetime
	decision_sha256: str
	base_manifest_sha256: str
	audit_sha256: str
	precision_sha256: str
	stage11b_sha256: str
	stage12_sha256: str

	@property
	def manifest(self) -> dict[str, Any]:
		return self.base_manifest


def _json(path: Path) -> dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
		raise Stage13Error(f"Cannot read JSON artifact: {path}") from error
	if not isinstance(value, dict):
		raise Stage13Error(f"JSON artifact is not an object: {path}")
	return value


def _sha256(path: Path) -> str:
	digest = sha256()
	try:
		with path.open("rb") as stream:
			for chunk in iter(lambda: stream.read(1024 * 1024), b""):
				digest.update(chunk)
	except OSError as error:
		raise Stage13Error(f"Cannot hash required artifact: {path}") from error
	return digest.hexdigest()


def _require_file(path: Path, label: str) -> Path:
	path = path.resolve()
	if not path.is_file():
		raise Stage13Error(f"Missing {label}: {path}")
	return path


def _require_hash(path: Path, expected: object, label: str) -> str:
	actual = _sha256(path)
	if not isinstance(expected, str) or actual != expected:
		raise Stage13Error(f"Changed {label} identity")
	return actual


def _source_files(precision: dict[str, Any], audit: dict[str, Any]) -> tuple[dict[str, Any], ...]:
	precision_files = precision.get("files")
	audit_files = {str(item.get("session")): item for item in audit.get("sessions", ()) if isinstance(item, dict)}
	if not isinstance(precision_files, list) or not precision_files:
		raise Stage13Error("Precision inventory has no retained files")
	result: list[dict[str, Any]] = []
	seen: set[str] = set()
	for raw in precision_files:
		if not isinstance(raw, dict) or not isinstance(raw.get("session"), str):
			raise Stage13Error("Precision inventory contains an invalid file entry")
		session = str(raw["session"])
		if session in seen:
			raise Stage13Error(f"Duplicate retained session: {session}")
		seen.add(session)
		audit_item = audit_files.get(session)
		if audit_item is None:
			raise Stage13Error(f"Audit does not cover retained session: {session}")
		if bool(raw.get("valid")) != bool(audit_item.get("file_valid")) or raw.get("file_sha256") != audit_item.get("file_sha256"):
			raise Stage13Error(f"Precision and audit identities differ: {session}")
		item = dict(raw)
		item["path"] = str(Path(str(raw["path"])).resolve())
		result.append(item)
	if len(result) != len(audit_files):
		raise Stage13Error("Precision and audit session coverage differs")
	return tuple(sorted(result, key=lambda item: str(item["session"])))


def _population_manifest(base: dict[str, Any], precision: dict[str, Any], audit: dict[str, Any], evidence_path: Path, cutoff: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
	files = _source_files(precision, audit)
	valid_sessions = {str(item["session"]) for item in files if item.get("valid") is True}
	evidence = ArchiveEvidenceStore(evidence_path)
	for item in files:
		if item.get("valid") is not True:
			continue
		path = Path(str(item["path"]))
		if not path.is_file():
			raise Stage13Error(f"Missing retained archive file: {path}")
		try:
			proof = evidence.verify(ARCHIVE_SOURCE, str(item["session"]), ArchiveFileInventory(str(path), str(item["session"]), str(item["file_sha256"]), True, int(item["record_count"]), str(item["records_id"]), ()))
		except ValueError as error:
			raise Stage13Error(f"Invalid archive evidence: {item['session']}") from error
		if proof.validated_at > cutoff:
			raise Stage13Error(f"Archive evidence is outside the Stage 13 cutoff: {item['session']}")
	population_files: list[dict[str, Any]] = []
	population_targets: list[dict[str, Any]] = []
	missing_dependencies: list[dict[str, Any]] = []
	for index, item in enumerate(files):
		session = str(item["session"])
		entry = dict(item)
		entry["path"] = str(Path(str(item["path"])).resolve())
		entry["relative_path"] = str(Path(str(item["path"])).resolve().relative_to(Path(str(base["inputs"]["data_root"])).resolve())).replace("\\", "/")
		population_files.append(entry)
		if item.get("valid") is not True:
			continue
		prior = str(files[index - 1]["session"]) if index > 0 and files[index - 1].get("valid") is True else None
		following = str(files[index + 1]["session"]) if index + 1 < len(files) and files[index + 1].get("valid") is True else None
		dependencies = tuple(value for value in (prior, following) if value is not None)
		missing = tuple(name for name, value in (("prior", prior), ("following", following)) if value is None)
		if len(dependencies) != 2:
			missing_dependencies.append({"session": session, "missing": list(missing), "reason": "neighboring retained archive dependency is invalid or absent"})
		population_targets.append({"session": session, "roles": ["retained_history"], "derived_dependencies": list(dependencies), "zscore_dependencies": []})
	manifest = {key: value for key, value in base.items() if key != "manifest_id"}
	manifest["mappings"] = dict(base.get("mappings", {}))
	manifest["files"] = population_files
	manifest["targets"] = population_targets
	manifest["fixed_targets"] = []
	manifest["target_blocks"] = {"retained_history": [str(item["session"]) for item in files if item.get("valid") is True]}
	manifest["population"] = {"stage": 13, "valid_sessions": len(valid_sessions), "missing_dependency_sessions": len(missing_dependencies), "cutoff": cutoff.isoformat()}
	manifest["manifest_id"] = content_id(manifest)
	return manifest, {"missing_dependencies": missing_dependencies, "invalid_files": [{"session": str(item["session"]), "finding": item.get("finding")} for item in files if item.get("valid") is not True]}


def validate_prerequisites(
	decision_path: Path,
	base_manifest_path: Path,
	audit_path: Path,
	precision_path: Path,
	stage11b_path: Path,
	stage12_path: Path,
	evidence_path: Path,
) -> Stage13Context:
	decision_path = _require_file(decision_path, "Stage 10 decision")
	base_manifest_path = _require_file(base_manifest_path, "Stage 6 manifest")
	audit_path = _require_file(audit_path, "Stage 5 audit")
	precision_path = _require_file(precision_path, "precision inventory")
	stage11b_path = _require_file(stage11b_path, "Stage 11b handoff")
	stage12_path = _require_file(stage12_path, "Stage 12 report")
	evidence_path = evidence_path.resolve()
	if not evidence_path.is_dir():
		raise Stage13Error(f"Missing archive evidence directory: {evidence_path}")
	decision = _json(decision_path)
	base_manifest = _json(base_manifest_path)
	audit = _json(audit_path)
	precision = _json(precision_path)
	stage11b = _json(stage11b_path)
	stage12 = _json(stage12_path)
	if decision.get("stage") != 10 or decision.get("status") != "complete" or decision.get("production_ready") is not False:
		raise Stage13Error("Stage 10 decision is not the frozen pre-production decision")
	if decision.get("production_backend_selected") is not True or decision.get("no_production_mutations") is not True:
		raise Stage13Error("Stage 10 production gate is missing")
	if base_manifest.get("stage") != 6 or base_manifest.get("schema_version") != 1:
		raise Stage13Error("Stage 6 manifest is invalid")
	if content_id({key: value for key, value in base_manifest.items() if key != "manifest_id"}) != base_manifest.get("manifest_id"):
		raise Stage13Error("Stage 6 manifest identity changed")
	if not audit.get("complete") or not audit.get("database_available"):
		raise Stage13Error("Stage 5 audit is incomplete")
	if stage11b.get("stage") != "11b" or stage11b.get("status") != "complete" or stage11b.get("recovery", {}).get("status") != "passed":
		raise Stage13Error("Stage 11b recovery handoff is incomplete")
	if stage12.get("stage") != 12 or stage12.get("status") != "complete" or stage12.get("production_ready") is not True:
		raise Stage13Error("Stage 12 pilot gate is incomplete")
	inputs = decision.get("inputs", {})
	base_hash = _require_hash(base_manifest_path, inputs.get("manifest", {}).get("sha256"), "Stage 6 manifest")
	audit_hash = _require_hash(audit_path, inputs.get("audit", {}).get("sha256"), "Stage 5 audit")
	precision_hash = _sha256(precision_path)
	if stage12.get("prerequisites", {}).get("manifest_sha256") != base_hash or stage12.get("prerequisites", {}).get("audit_sha256") != audit_hash:
		raise Stage13Error("Stage 12 report does not bind the current source artifacts")
	if stage12.get("prerequisites", {}).get("precision_sha256") != precision_hash:
		raise Stage13Error("Stage 12 report does not bind the current precision inventory")
	cutoff_text = stage11b.get("prerequisites", {}).get("cutoff")
	try:
		cutoff = datetime.fromisoformat(str(cutoff_text))
	except ValueError as error:
		raise Stage13Error("Stage 11b cutoff is invalid") from error
	if cutoff.tzinfo is None or cutoff.utcoffset() is None or cutoff > datetime.now(timezone.utc):
		raise Stage13Error("Stage 13 cutoff is invalid or in the future")
	if not isinstance(precision.get("files"), list) or not precision["files"]:
		raise Stage13Error("Precision inventory is empty")
	for key, expected in (("raw_adoption_repair", "python"), ("derived_computation_persistence", "java")):
		if decision.get("workloads", {}).get(key, {}).get("selected_backend") != expected:
			raise Stage13Error(f"Stage 10 selected backend for {key} is not {expected}")
	return Stage13Context(decision_path, decision, base_manifest_path, base_manifest, audit_path, precision_path, stage11b_path, stage12_path, evidence_path, cutoff.astimezone(timezone.utc), _sha256(decision_path), base_hash, audit_hash, precision_hash, _sha256(stage11b_path), _sha256(stage12_path))


def _quote(name: str) -> str:
	return quote_identifier(name)


def _database_revision(connection: Any, names: TableNames) -> str:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT workload, session_date, series_key, content_digest FROM {_quote(MIGRATION_COMPLETION)} ORDER BY workload, session_date, series_key", ())
		completion = [[str(row[0]), str(row[1]), str(row[2]), str(row[3])] for row in cursor.fetchall()]
		cursor.execute(f"SELECT session_date, raw_id, derived_dependency_id, derived_content_id FROM {_quote(names.imports)} ORDER BY session_date", ())
		imports = [[str(value) if value is not None else None for value in row] for row in cursor.fetchall()]
		cursor.execute(f"SELECT COUNT(*), COALESCE(SUM(DATA_LENGTH), 0), COALESCE(SUM(INDEX_LENGTH), 0) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN (%s, %s, %s, %s)", (names.prices, names.deltas, names.imports, MIGRATION_COMPLETION))
		counts = [str(value) if value is not None else None for value in cursor.fetchone()]
		return content_id({"mapping": _mapping_digest(connection, names), "completion": completion, "imports": imports, "table_size": counts})
	finally:
		cursor.close()


def _active_writer_guard(connection: Any) -> None:
	if _active_writers(connection):
		raise Stage13Error("Competing production writers are active")


def _java_classpath() -> str:
	java_root = Path(__file__).resolve().parents[3] / "java"
	return ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))


def _run_java_derived(dbinfo_file: Path, manifest_path: Path, sessions: tuple[str, ...], checkpoint_directory: Path, timeout: float) -> dict[str, Any]:
	command = ["java", "-Xmx512m", "-cp", _java_classpath(), "stockguidance.migration.JavaDerivedBenchmarkMain", "--manifest", str(manifest_path.resolve()), "--dbinfo-file", str(dbinfo_file.resolve()), "--production", "true", "--preparers", "1", "--sessions", ",".join(sessions), "--checkpoint-directory", str(checkpoint_directory.resolve())]
	try:
		result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
	except (OSError, subprocess.TimeoutExpired) as error:
		raise Stage13Error("Selected Java derived entry point could not run") from error
	try:
		payload = json.loads(result.stdout)
	except (TypeError, ValueError, json.JSONDecodeError) as error:
		raise Stage13Error("Selected Java derived entry point returned invalid JSON") from error
	if result.returncode != 0 or payload.get("failed") or payload.get("status") != "complete":
		raise Stage13Error("Selected Java derived entry point failed")
	return cast(dict[str, Any], payload)


def _write_json(path: Path, value: dict[str, Any]) -> None:
	atomic_write(path.resolve(), encode_json(value))


def _read_resume(output: Path, manifest_id: str, expected_revision: str | None) -> dict[str, Any] | None:
	if not output.exists():
		return None
	report = _json(output)
	if report.get("schema_version") != STAGE13_SCHEMA_VERSION or report.get("stage") != 13:
		raise Stage13Error("Existing Stage 13 report has an unsupported schema")
	if report.get("manifest_id") != manifest_id:
		raise Stage13Error("Existing Stage 13 report belongs to a different retained input set")
	if expected_revision is not None and report.get("database_revision_after") != expected_revision:
		raise Stage13Error("Database revision differs from the last durable Stage 13 marker")
	return report


def _chunked(values: tuple[str, ...], size: int) -> Iterable[tuple[str, ...]]:
	if size < 1 or size > 20:
		raise Stage13Error("Stage 13 chunk size must be between one and twenty sessions")
	for start in range(0, len(values), size):
		yield values[start:start + size]


def _completion_summary(connection: Any, names: TableNames) -> dict[str, int]:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT workload, COUNT(*) FROM {_quote(MIGRATION_COMPLETION)} GROUP BY workload", ())
		return {str(row[0]): int(row[1]) for row in cursor.fetchall()}
	finally:
		cursor.close()


def _mapping_snapshot(connection: Any, names: TableNames) -> dict[str, Any]:
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT id, label FROM {_quote(names.tickers)} ORDER BY id", ())
		tickers = tuple((int(row[0]), str(row[1])) for row in cursor.fetchall())
		cursor.execute(f"SELECT id, transaction_date FROM {_quote(names.sessions)} ORDER BY id", ())
		dates = tuple((int(row[0]), str(row[1])) for row in cursor.fetchall())
		return {"identity": _mapping_digest(connection, names), "ticker_rows": tickers, "date_rows": dates}
	finally:
		cursor.close()


def run_stage13(
	context: Stage13Context,
	dbinfo_file: Path,
	data_root: Path,
	output: Path,
	*,
	chunk_size: int = 20,
	java_timeout: float = 7200.0,
	resume: bool = True,
) -> dict[str, Any]:
	if not data_root.resolve().is_dir():
		raise Stage13Error(f"Missing retained data root: {data_root.resolve()}")
	manifest, scope = _population_manifest(context.base_manifest, _json(context.precision_path), _json(context.audit_path), context.evidence_path, context.cutoff)
	output = output.resolve()
	stage_directory = output.parent / "stage13"
	manifest_path = stage_directory / "population-manifest.json"
	checkpoint_directory = stage_directory / "derived-checkpoints"
	stage_directory.mkdir(parents=True, exist_ok=True)
	_write_json(manifest_path, manifest)
	names = _manifest_names(manifest)
	population_context = Stage13Context(context.decision_path, context.decision, manifest_path, manifest, context.audit_path, context.precision_path, context.stage11b_path, context.stage12_path, context.evidence_path, context.cutoff, context.decision_sha256, context.base_manifest_sha256, context.audit_sha256, context.precision_sha256, context.stage11b_sha256, context.stage12_sha256)
	production = load_database_configuration(dbinfo_file)
	connection = _connect(dbinfo_file, production.database)
	try:
		_active_writer_guard(connection)
		before_mapping = _mapping_snapshot(connection, names)
		before_revision = _database_revision(connection, names)
	finally:
		connection.rollback()
		connection.close()
	old_report = _read_resume(output, manifest["manifest_id"], before_revision) if resume else None
	if old_report is not None and old_report.get("status") == "complete":
		return old_report
	valid_sessions = tuple(str(item["session"]) for item in manifest["files"] if item.get("valid") is True)
	derived_sessions = tuple(str(item["session"]) for item in manifest["targets"] if len(item.get("derived_dependencies", ())) == 2)
	raw_results: list[dict[str, Any]] = [] if old_report is None else list(old_report.get("raw_sessions", ()))
	derived_results: list[dict[str, Any]] = [] if old_report is None else list(old_report.get("derived_sessions", ()))
	raw_chunks: list[dict[str, Any]] = [] if old_report is None else list(old_report.get("raw_chunks", ()))
	derived_chunks: list[dict[str, Any]] = [] if old_report is None else list(old_report.get("derived_chunks", ()))
	completed_raw = {str(item.get("session")) for item in raw_results}
	completed_derived = {str(item.get("session")) for item in derived_results}
	started = perf_counter()
	state: dict[str, Any] = {"schema_version": STAGE13_SCHEMA_VERSION, "stage": 13, "status": "running", "manifest_id": manifest["manifest_id"], "manifest_sha256": _sha256(manifest_path), "prerequisites": {"decision_sha256": context.decision_sha256, "base_manifest_sha256": context.base_manifest_sha256, "audit_sha256": context.audit_sha256, "precision_sha256": context.precision_sha256, "stage11b_sha256": context.stage11b_sha256, "stage12_sha256": context.stage12_sha256, "cutoff": context.cutoff.isoformat()}, "scope": {"valid_archive_sessions": len(valid_sessions), "derived_sessions_with_two_neighbors": len(derived_sessions), "invalid_files": scope["invalid_files"], "missing_dependencies": scope["missing_dependencies"]}, "raw_chunks": raw_chunks, "derived_chunks": derived_chunks, "raw_sessions": raw_results, "derived_sessions": derived_results, "database_revision_before": before_revision}
	for chunk_index, chunk in enumerate(_chunked(tuple(session for session in valid_sessions if session not in completed_raw), chunk_size), len(raw_chunks)):
		connection = _connect(dbinfo_file, production.database)
		chunk_started = perf_counter()
		try:
			_active_writer_guard(connection)
			current_revision = _database_revision(connection, names)
			if state.get("database_revision_after", before_revision) != current_revision:
				raise Stage13Error("Database revision changed before a raw chunk")
			for session in chunk:
				affected = {str(target["session"]) for target in manifest["targets"] if session in target.get("derived_dependencies", ()) or session == target["session"]}
				result = _adopt_raw_session(connection, population_context, names, session, datetime.now(timezone.utc).isoformat(), affected, set(), context.cutoff.isoformat())
				raw_results.append(result)
				completed_raw.add(session)
			connection.rollback()
			chunk_revision = _database_revision(connection, names)
			chunk_record = {"index": chunk_index, "sessions": list(chunk), "seconds": perf_counter() - chunk_started, "database_revision_after": chunk_revision}
			raw_chunks.append(chunk_record)
			state["raw_chunks"] = raw_chunks
			state["raw_sessions"] = raw_results
			state["database_revision_after"] = chunk_revision
			_write_json(output, state)
		finally:
			connection.close()
	for chunk_index, chunk in enumerate(_chunked(tuple(session for session in derived_sessions if session not in completed_derived), chunk_size), len(derived_chunks)):
		connection = _connect(dbinfo_file, production.database)
		try:
			_active_writer_guard(connection)
			current_revision = _database_revision(connection, names)
			if state.get("database_revision_after") != current_revision:
				raise Stage13Error("Database revision changed before a derived chunk")
		finally:
			connection.rollback()
			connection.close()
		chunk_started = perf_counter()
		java_result = _run_java_derived(dbinfo_file, manifest_path, chunk, checkpoint_directory, java_timeout)
		for item in java_result.get("sessions", ()):
			if isinstance(item, dict) and item.get("status") == "complete":
				derived_results.append({"session": str(item["session"]), "row_count": int(item.get("rows", 0)), "content_digest": str(item.get("content_id")), "status": "complete"})
				completed_derived.add(str(item["session"]))
		if set(chunk) != completed_derived.intersection(chunk):
			raise Stage13Error("Java derived chunk did not complete every session")
		connection = _connect(dbinfo_file, production.database)
		try:
			connection.rollback()
			chunk_revision = _database_revision(connection, names)
		finally:
			connection.close()
		derived_chunks.append({"index": chunk_index, "sessions": list(chunk), "seconds": perf_counter() - chunk_started, "database_revision_after": chunk_revision, "rows": int(java_result.get("rows", 0))})
		state["derived_chunks"] = derived_chunks
		state["derived_sessions"] = derived_results
		state["database_revision_after"] = chunk_revision
		_write_json(output, state)
	connection = _connect(dbinfo_file, production.database)
	try:
		_active_writer_guard(connection)
		after_mapping = _mapping_snapshot(connection, names)
		final_revision = _database_revision(connection, names)
		summary = _completion_summary(connection, names)
	finally:
		connection.rollback()
		connection.close()
	original_tickers = dict(before_mapping["ticker_rows"])
	after_tickers = dict(after_mapping["ticker_rows"])
	original_dates = dict(before_mapping["date_rows"])
	after_dates = dict(after_mapping["date_rows"])
	if any(after_tickers.get(identifier) != label for identifier, label in original_tickers.items()):
		raise Stage13Error("Ticker mapping rows were removed")
	if any(after_dates.get(identifier) != session for identifier, session in original_dates.items()):
		raise Stage13Error("Date mapping rows were changed")
	if len(completed_raw) != len(valid_sessions) or len(completed_derived) != len(derived_sessions):
		raise Stage13Error("Stage 13 did not complete the retained raw/derived scope")
	state.update({"status": "complete", "production_ready": True, "database_revision_after": final_revision, "coverage": {"valid_archive_sessions": len(valid_sessions), "adopted_raw_sessions": len(completed_raw), "derived_sessions": len(completed_derived), "invalid_files": len(scope["invalid_files"]), "missing_dependency_sessions": len(scope["missing_dependencies"])}, "mapping_identity": {"before": before_mapping["identity"], "after": after_mapping["identity"], "original_ticker_ids_retained": True, "original_date_ids_retained": True}, "rows": {"original_raw": sum(int(item.get("original_row_count", 0)) for item in raw_results), "current_raw": sum(int(item.get("current_row_count", 0)) for item in raw_results), "adopted_raw_records": sum(int(item.get("record_count", 0)) for item in raw_results), "derived_rows": sum(int(item.get("row_count", 0)) for item in derived_results)}, "receipts": {"completion_markers": summary, "raw_receipts": len(completed_raw), "derived_receipts": len(completed_derived)}, "repaired_sessions": [item["session"] for item in raw_results if item.get("repaired")], "matching_sessions": [item["session"] for item in raw_results if item.get("matching")], "unresolved": {"invalid_files": scope["invalid_files"], "missing_dependencies": scope["missing_dependencies"]}, "elapsed_seconds": perf_counter() - started, "handoff": {"next": "Stage 14", "selected_backends": {"raw": "python", "derived": "java"}, "one_writer": True, "chunk_size": chunk_size, "zscore_state": "Stage 14 must republish z-scores after raw repairs; Stage 13 does not certify z-score rows", "resume": "Rerun with the same output and manifest; durable per-session markers are reused only after database-revision validation"}})
	_write_json(output, state)
	return state
