from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
from typing import Any


THRESHOLD = 0.10
WORKLOADS = ("raw_adoption_repair", "derived_computation_persistence", "zscores_persistence")
BACKENDS = ("python", "java", "sql")


class Stage10Error(ValueError):
	pass


def _read_json(path: Path) -> dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as error:
		raise Stage10Error(f"Unable to read JSON artifact: {path}") from error
	if not isinstance(value, dict):
		raise Stage10Error(f"Expected JSON object: {path}")
	return value


def _hash_file(path: Path) -> str:
	digest = sha256()
	with path.open("rb") as stream:
		for chunk in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _number(value: object, label: str) -> float:
	if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
		raise Stage10Error(f"Expected finite number for {label}")
	return float(value)


def _require(condition: bool, message: str) -> None:
	if not condition:
		raise Stage10Error(message)


def _report_configs(report: dict[str, Any], workload: str) -> list[dict[str, Any]]:
	configs = report.get("configurations")
	_require(isinstance(configs, dict), f"{workload} report lacks configurations")
	result: list[dict[str, Any]] = []
	for name, raw_config in configs.items():
		_require(isinstance(raw_config, dict), f"{workload}/{name} configuration is invalid")
		status = raw_config.get("status")
		_require(status == "measured", f"{workload}/{name} is not measured")
		backend = raw_config.get("backend")
		_require(backend in BACKENDS, f"{workload}/{name} has an unknown backend")
		preparers = raw_config.get("preparers")
		_require(isinstance(preparers, int) and not isinstance(preparers, bool) and preparers > 0, f"{workload}/{name} has invalid preparer count")
		median = _number(raw_config.get("warm_median_seconds"), f"{workload}/{name}.warm_median_seconds")
		throughput = _number(raw_config.get("throughput_rows_per_second"), f"{workload}/{name}.throughput_rows_per_second")
		_require(median > 0 and throughput > 0, f"{workload}/{name} has nonpositive measurement")
		units = raw_config.get("scenarios", raw_config.get("blocks"))
		_require(isinstance(units, dict) and units, f"{workload}/{name} lacks measured units")
		for unit_name, unit in units.items():
			_require(isinstance(unit, dict), f"{workload}/{name}/{unit_name} is invalid")
			_require(unit.get("status") == "measured", f"{workload}/{name}/{unit_name} is not measured")
			correctness = unit.get("correctness")
			_require(isinstance(correctness, list) and correctness, f"{workload}/{name}/{unit_name} lacks correctness audits")
			_require(all(isinstance(item, dict) and item.get("status") == "passed" for item in correctness), f"{workload}/{name}/{unit_name} failed correctness audit")
		result.append({"name": str(name), "backend": backend, "preparers": preparers, "status": status, "warm_median_seconds": median, "throughput_rows_per_second": throughput})
	return result


def _validate_recovery(study: dict[str, Any], diagnostic: dict[str, Any]) -> dict[str, Any]:
	recovery: dict[str, Any] = {}
	for workload in ("raw", "derived"):
		evidence = study[workload].get("recovery_evidence")
		_require(isinstance(evidence, dict), f"{workload} recovery evidence is missing")
		for backend in BACKENDS:
			entry = evidence.get(backend)
			_require(isinstance(entry, dict), f"{workload}/{backend} recovery evidence is missing")
			allowed = ("passed", "passed_reference") if backend == "python" else ("passed",)
			_require(entry.get("status") in allowed, f"{workload}/{backend} recovery gate failed")
			if backend != "python":
				_require(isinstance(entry.get("checks"), list) and entry["checks"], f"{workload}/{backend} recovery checks are missing")
		recovery[workload] = evidence
	_require(diagnostic.get("status") == "passed", "z-score recovery diagnostic did not pass")
	_require(diagnostic.get("comparison_eligible") is False, "synthetic z-score diagnostic was incorrectly made ranking evidence")
	_require(diagnostic.get("audit_cache_tamper_rejected") is True, "z-score audit tamper check is missing")
	seed_recovery = diagnostic.get("seed_recovery")
	_require(isinstance(seed_recovery, list) and "restored_seed_verified" in seed_recovery, "z-score seed recovery is incomplete")
	for backend in BACKENDS:
		entry = diagnostic.get("backends", {}).get(backend)
		_require(isinstance(entry, dict) and entry.get("status") == "complete", f"z-score/{backend} diagnostic did not complete")
		_require(isinstance(entry.get("recovery"), list) and entry["recovery"], f"z-score/{backend} recovery checks are missing")
		audits = entry.get("audits")
		_require(isinstance(audits, list) and audits and all(isinstance(audit, dict) and audit.get("status") == "passed" for audit in audits), f"z-score/{backend} audits are incomplete")
	recovery["zscores"] = {"path": "synthetic diagnostic", "status": "passed", "comparison_eligible": False}
	return recovery


def _select_workload(workload: str, configs: list[dict[str, Any]]) -> dict[str, Any]:
	by_backend = {backend: sorted((item for item in configs if item["backend"] == backend), key=lambda item: item["warm_median_seconds"]) for backend in BACKENDS}
	for backend in BACKENDS:
		_require(by_backend[backend], f"{workload} lacks a {backend} candidate")
	python_best = by_backend["python"][0]
	candidates: list[dict[str, Any]] = []
	for backend in BACKENDS:
		best = by_backend[backend][0]
		improvement = 0.0 if backend == "python" else (python_best["warm_median_seconds"] - best["warm_median_seconds"]) / python_best["warm_median_seconds"]
		candidates.append({**best, "improvement_vs_best_python": improvement, "qualifies": backend == "python" or improvement >= THRESHOLD})
	eligible = [candidate for candidate in candidates if candidate["qualifies"]]
	tie_backend = "java" if workload == "raw_adoption_repair" else "sql"
	selected = sorted(eligible, key=lambda item: (item["warm_median_seconds"], 0 if item["backend"] == tie_backend else 1))[0]
	speedups: dict[str, Any] = {}
	for preparers in sorted({item["preparers"] for item in configs}):
		python = next((item for item in configs if item["backend"] == "python" and item["preparers"] == preparers), None)
		by_alternative: dict[str, Any] = {}
		if python is not None:
			for backend in ("java", "sql"):
				alternative = next((item for item in configs if item["backend"] == backend and item["preparers"] == preparers), None)
				if alternative is not None:
					by_alternative[backend] = {"configuration": alternative["name"], "warm_median_seconds": alternative["warm_median_seconds"], "improvement_vs_python": (python["warm_median_seconds"] - alternative["warm_median_seconds"]) / python["warm_median_seconds"], "qualifies": (python["warm_median_seconds"] - alternative["warm_median_seconds"]) / python["warm_median_seconds"] >= THRESHOLD}
			speedups[str(preparers)] = {"python_configuration": python["name"] if python else None, "python_warm_median_seconds": python["warm_median_seconds"] if python else None, "alternatives": by_alternative}
	return {"selected_backend": selected["backend"], "selected_configuration": selected["name"], "selected_warm_median_seconds": selected["warm_median_seconds"], "selected_throughput_rows_per_second": selected["throughput_rows_per_second"], "best_python_configuration": python_best["name"], "best_python_warm_median_seconds": python_best["warm_median_seconds"], "candidates": candidates, "speedups_by_preparer_count": speedups, "selection_reason": "lowest eligible startup-inclusive warm median; Python retained when no alternative meets the 10% threshold"}


def _capacity(manifest: dict[str, Any], audit: dict[str, Any], output_path: Path, study: dict[str, Any]) -> dict[str, Any]:
	ticker_count = len(manifest.get("mappings", {}).get("ticker_mappings", []))
	session_count = int(audit.get("session_count", 0))
	valid_sessions = int(audit.get("valid_archive_file_count", 0))
	valid_records = sum(int(item.get("archive_count", 0)) for item in audit.get("sessions", []) if item.get("file_valid") is True)
	dense_rows = ticker_count * session_count * 4
	usage = shutil.disk_usage(output_path.resolve().anchor or output_path.resolve().parent)
	server_disks: list[dict[str, int]] = []
	for report_name in ("raw", "derived", "zscores"):
		for config in study[report_name].get("configurations", {}).values():
			disk = config.get("disk")
			if isinstance(disk, dict) and isinstance(disk.get("capacity_bytes"), int) and isinstance(disk.get("free_bytes"), int):
				server_disks.append({"capacity_bytes": disk["capacity_bytes"], "free_bytes": disk["free_bytes"]})
	return {"counts": {"ticker_ids": ticker_count, "retained_sessions": session_count, "valid_archive_sessions": valid_sessions, "valid_archive_records": valid_records, "physical_database_rows": audit.get("database_row_count"), "dense_zscore_tables": 4, "dense_zscore_rows": dense_rows}, "disk_observation": {"local_volume_capacity_bytes": usage.total, "local_volume_free_bytes": usage.free, "benchmark_server_min_capacity_bytes": min((item["capacity_bytes"] for item in server_disks), default=None), "benchmark_server_min_free_bytes": min((item["free_bytes"] for item in server_disks), default=None)}, "runtime_estimates": {"basis": "linear extrapolation from selected startup-inclusive warm throughput; estimate only, compact Stage 9c dates are not full-history evidence", "raw_seconds": valid_records / _number(study["raw"]["configurations"]["python-p3"]["throughput_rows_per_second"], "raw throughput"), "derived_seconds": valid_records / _number(study["derived"]["configurations"]["java-p1"]["throughput_rows_per_second"], "derived throughput"), "zscores_seconds": dense_rows / _number(study["zscores"]["configurations"]["java-p1"]["throughput_rows_per_second"], "z-score throughput")}, "limits": {"max_preparer_workers": 3, "max_queued_sessions": 3, "max_writers": 1, "raw_transaction_unit": "one session", "derived_transaction_unit": "one session", "zscore_transaction_unit": "one series/session", "zscore_processing_chunk": "chronological 20-session chunk", "memory_per_preparer_bytes": 536870912, "java_heap": "-Xmx512m", "native_threads": 1}, "storage_headroom": {"status": "not measured by Stage 10", "ddl_table_copy": "Stage 12 must reserve space for a 44-million-row table copy and rollback before schema migration", "temporary_storage": "Stage 11/12 must reserve disposable staging, indexes and rebuild intermediates in addition to dense z-score rows", "recovery": "Stage 12 must retain an independently restorable backup and enough space for a second recovery target", "activation_gate": "Do not begin full z-score population until Stage 12 measures table, index, temporary and recovery bytes against the observed free space"}}


def build_stage10_decision(study_path: Path, manifest_path: Path, provisional_path: Path, audit_path: Path, diagnostic_path: Path, output_path: Path) -> dict[str, Any]:
	study = _read_json(study_path)
	manifest = _read_json(manifest_path)
	provisional = _read_json(provisional_path)
	audit = _read_json(audit_path)
	diagnostic = _read_json(diagnostic_path)
	_require(study.get("status") == "complete", "Stage 9 aggregate is not complete")
	_require(study.get("substage") == {"9a": "complete", "9b": "complete", "9c": "complete"}, "Stage 9 comparison substages are incomplete")
	_require(manifest.get("immutable_inputs") is True, "Stage 6 manifest is not immutable")
	manifest_hash = _hash_file(manifest_path)
	study_manifest = study.get("manifest", {})
	_require(study_manifest.get("id") == manifest.get("manifest_id"), "Stage 9 and manifest IDs differ")
	recorded_manifest_hashes = {study[report_name].get("manifest", {}).get("sha256") for report_name in ("raw", "derived", "zscores")}
	_require(recorded_manifest_hashes == {manifest_hash}, "Stage 9 manifest hash differs from disk")
	_require(provisional.get("provisional") is True, "The prior decision is not provisional")
	all_configs: dict[str, list[dict[str, Any]]] = {}
	for report_name, workload in (("raw", "raw_adoption_repair"), ("derived", "derived_computation_persistence"), ("zscores", "zscores_persistence")):
		report = study.get(report_name)
		_require(isinstance(report, dict) and report.get("status") == "complete", f"{workload} report is incomplete")
		production_access = report.get("production_access")
		_require(isinstance(production_access, dict) and all(value is False for value in production_access.values()), f"{workload} report records production access")
		all_configs[workload] = _report_configs(report, workload)
	recovery = _validate_recovery(study, diagnostic)
	selection = {workload: _select_workload(workload, configs) for workload, configs in all_configs.items()}
	capacity = _capacity(manifest, audit, output_path, study)
	code_hashes: dict[str, str] = {}
	for report_name in ("raw", "derived", "zscores"):
		for path, value in study[report_name].get("source_code_hashes", {}).items():
			code_hashes[path] = value
	return {"schema_version": 2, "stage": 10, "status": "complete", "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), "provisional": False, "production_ready": False, "production_backend_selected": True, "provisional_decision": {"path": str(provisional_path.resolve()), "sha256": _hash_file(provisional_path), "schema_version": provisional.get("schema_version"), "identity": provisional.get("study_sha256")}, "inputs": {"study": {"path": str(study_path.resolve()), "sha256": _hash_file(study_path)}, "manifest": {"path": str(manifest_path.resolve()), "sha256": manifest_hash, "id": manifest.get("manifest_id")}, "audit": {"path": str(audit_path.resolve()), "sha256": _hash_file(audit_path)}, "zscore_recovery_diagnostic": {"path": str(diagnostic_path.resolve()), "sha256": _hash_file(diagnostic_path)}}, "schema": {"corrected_schema_hash": manifest.get("corrected_schema_hash"), "authoritative_registry": manifest.get("authoritative_registry")}, "code_hashes": code_hashes, "reader": {"archive_reader": "python:stockguidance.performance.archive_read", "selection": "Python retained for raw adoption/repair; Java reader evidence remains historical/provisional only"}, "workloads": selection, "recovery_gates": recovery, "capacity": capacity, "rollout": {"mixed_pipeline": {"selected_per_workload": True, "activation": "blocked_until_stage11_transfer_measurement", "reason": "Stage 9 measured isolated end-to-end workloads and no transfer/staging/orchestration cost"}, "transactions": {"raw": "one session per transaction", "derived": "one session per transaction", "zscores": "one series/session per transaction"}, "batch_limits": {"preparers": "one by default; at most three only after Stage 11 memory/recovery proof", "queued_sessions": 3, "writers": 1, "zscore_chunk": "chronological 20-session chunk", "reduced_memory_mode": "one preparer and one queued session"}, "sql_loader_orchestrator": {"raw": "not selected; Stage 8a SQL candidate retained for audit only", "derived": "not selected; Stage 8b SQL routine retained for audit only", "zscores": "not selected; Stage 8c SQL routines retained for audit only"}, "production_entry_points": {"raw": "Python MySQL candidate path in stockguidance.performance.mysql_candidates", "derived": "Java stockguidance.migration.JavaDerivedBenchmarkMain / JavaWriterCandidate", "zscores": "Java stockguidance.migration.JavaZScoreBenchmarkMain / JavaZScoreCandidate"}, "gates": {"stage11": "wire selected paths and measure handoff cost in disposable storage", "stage12": "restore backup, migrate precision/collation, pilot and recheck capacity", "stage13": "populate valid raw/derived sessions chronologically", "stage14": "populate both z-score families at windows 20 and 60", "stage15": "independent full-scope audit and operating handoff"}}, "selection_rule": {"threshold": THRESHOLD, "formula": "(best Python warm median - alternative warm median) / best Python warm median", "tie_break": {"raw_adoption_repair": "Java", "derived_computation_persistence": "SQL", "zscores_persistence": "SQL"}, "timing_scope": "startup-inclusive end-to-end through durable commit/checkpoint; setup and correctness audit excluded"}, "no_production_mutations": True}


def write_stage10_decision(decision: dict[str, Any], output_path: Path) -> None:
	output_path.parent.mkdir(parents=True, exist_ok=True)
	temporary = output_path.with_name(output_path.name + ".tmp")
	temporary.write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	temporary.replace(output_path)
