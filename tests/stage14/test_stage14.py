from datetime import date, timedelta
import json

import pytest

from stockguidance.market_data.identity import content_id
from stockguidance.performance.stage14 import (
	Stage14Error,
	_audit_selection,
	_reference_status_score,
	build_zscore_manifest,
	validate_prerequisites,
)


def _base_manifest(count: int = 61) -> dict:
	start = date(2020, 1, 2)
	files = []
	targets = []
	for index in range(count):
		session = (start + timedelta(days=index)).isoformat()
		files.append({"session": session, "valid": True, "records_id": f"record-{index}"})
		targets.append({"session": session, "derived_dependencies": []})
	return {"schema_version": 1, "stage": 6, "files": files, "targets": targets}


def test_build_zscore_manifest_uses_60_observations_plus_raw_predecessor():
	manifest, scope = build_zscore_manifest(_base_manifest())

	assert scope["requested_sessions"] == 61
	assert manifest["targets"][0]["zscore_dependencies"] == ["2020-01-02"]
	assert len(manifest["targets"][59]["zscore_dependencies"]) == 60
	assert len(manifest["targets"][60]["zscore_dependencies"]) == 61
	assert manifest["targets"][60]["zscore_dependencies"][-1] == "2020-03-02"
	assert manifest["manifest_id"] == content_id({key: value for key, value in manifest.items() if key != "manifest_id"})


def test_reference_status_preserves_unverified_inputs_and_null_score():
	dates = ("2020-01-01", "2020-01-02", "2020-01-03")
	status, score = _reference_status_score("price", 3, 2, dates, 7, {("2020-01-01", 7): 1, ("2020-01-02", 7): 2, ("2020-01-03", 7): 3}, {}, {date_value: True for date_value in dates})
	assert status == 0
	assert score == pytest.approx(1.2247449, rel=1e-6)
	status, score = _reference_status_score("price", 3, 2, dates, 7, {("2020-01-01", 7): 1, ("2020-01-02", 7): 2, ("2020-01-03", 7): 3}, {}, {"2020-01-01": False, "2020-01-02": True, "2020-01-03": True})
	assert status == 3
	assert score is None


def test_audit_selection_is_deterministic_and_includes_chunk_boundaries():
	sessions = tuple(f"2020-01-{index:02d}" for index in range(1, 46))
	selected = _audit_selection(sessions, 20, 20261022, 3)

	assert selected == _audit_selection(sessions, 20, 20261022, 3)
	assert {"2020-01-01", "2020-01-20", "2020-01-21", "2020-01-40", "2020-01-45"} <= set(selected)


def test_validate_prerequisites_binds_stage13_manifest_and_snapshot(tmp_path):
	base = _base_manifest()
	base["manifest_id"] = content_id(base)
	manifest_path = tmp_path / "population-manifest.json"
	manifest_path.write_text(json.dumps(base), encoding="utf-8")
	stage13_path = tmp_path / "population.json"
	stage13_path.write_text(json.dumps({"schema_version": 1, "stage": 13, "status": "complete", "production_ready": True, "manifest_sha256": __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest(), "coverage": {"adopted_raw_sessions": 61, "valid_archive_sessions": 61}, "receipts": {"raw_receipts": 61}, "prerequisites": {"cutoff": "2026-10-21T23:59:00-06:00"}}), encoding="utf-8")
	decision_path = tmp_path / "decision.json"
	decision_path.write_text(json.dumps({"stage": 10, "status": "complete", "production_ready": False, "production_backend_selected": True, "no_production_mutations": True, "workloads": {"zscores_persistence": {"selected_backend": "java"}}}), encoding="utf-8")
	stage12_path = tmp_path / "stage12.json"
	stage12_path.write_text(json.dumps({"stage": 12, "status": "complete", "production_ready": True, "production_access": {"dml": True}}), encoding="utf-8")
	snapshot_path = tmp_path / "snapshot.json"
	snapshot_path.write_text(json.dumps({"schema_version": 1, "operation": "offline_archive_snapshot", "provenance_kind": "adopted_archive_snapshot", "cutoff": "2026-10-19T23:47:59+00:00"}), encoding="utf-8")

	context = validate_prerequisites(decision_path, stage12_path, stage13_path, manifest_path, snapshot_path)
	assert context.stage13_manifest["manifest_id"] == base["manifest_id"]

	decision_path.write_text(json.dumps({"stage": 10, "status": "complete", "production_ready": False, "production_backend_selected": True, "no_production_mutations": True, "workloads": {"zscores_persistence": {"selected_backend": "sql"}}}), encoding="utf-8")
	with pytest.raises(Stage14Error, match="Java"):
		validate_prerequisites(decision_path, stage12_path, stage13_path, manifest_path, snapshot_path)
