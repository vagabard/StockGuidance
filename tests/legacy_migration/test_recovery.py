from __future__ import annotations

import json
from pathlib import Path

import pytest

from stockguidance.performance.stage11_recovery import recovery_evidence, validate_stage11a_report
from stockguidance.performance.stage11_integration import Stage11Error

from legacy_migration.test_raw_adoption import _fixture


def _handoff_report(context, *, production: bool = False) -> dict:
	return {
		"schema_version": 1,
		"stage": "11a",
		"status": "complete",
		"production_access": {"ddl": production, "dml": production},
		"prerequisites": {
			"decision_sha256": context.decision_sha256,
			"manifest_sha256": context.manifest_sha256,
			"audit_sha256": context.audit_sha256,
			"manifest_id": context.manifest_id,
		},
		"selected_backends": {"raw": "python", "derived": "java", "zscores": "java"},
		"execution": {"workloads": ["raw", "derived"]},
	}


def test_stage11a_handoff_is_bound_to_current_prerequisites(tmp_path: Path):
	context, _ = _fixture(tmp_path)
	report_path = tmp_path / "stage11a.json"
	report_path.write_text(json.dumps(_handoff_report(context)), encoding="utf-8")
	assert validate_stage11a_report(report_path, context)["status"] == "complete"

	report_path.write_text(json.dumps(_handoff_report(context, production=True)), encoding="utf-8")
	with pytest.raises(Stage11Error, match="disposable-only"):
		validate_stage11a_report(report_path, context)


def test_recovery_evidence_requires_all_failure_classes(tmp_path: Path):
	context, _ = _fixture(tmp_path)
	checks = [
		"raw_before_commit_rollback",
		"raw_after_commit_durable_without_checkpoint",
		"raw_restart_idempotent",
		"stale_checkpoint_rejected",
		"changed_input_rejected",
		"stored_dependency_corruption_rejected",
	]
	artifacts = {}
	for workload in ("raw", "derived", "zscores"):
		path = tmp_path / f"{workload}.json"
		path.write_text(json.dumps({"status": "complete", "recovery_checks": checks, "schema": {"production_target": False, "schema_hash": "schema"}, "code_hashes": {"candidate": "code"}}), encoding="utf-8")
		artifacts[workload] = {"path": str(path), "sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(), "status": "passed"}
	context.decision["recovery_gates"] = {"raw": {"java": artifacts["raw"]}, "derived": {"java": artifacts["derived"]}, "zscores": {"java": artifacts["zscores"]}}
	result = recovery_evidence(context)
	assert result["status"] == "passed"
	assert all(result["checks"].values())
