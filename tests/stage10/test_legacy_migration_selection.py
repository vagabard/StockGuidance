from __future__ import annotations

import json
from pathlib import Path

import pytest

from stockguidance.performance.stage10_selection import Stage10Error, _select_workload, build_stage10_decision


def _config(name: str, backend: str, preparers: int, median: float) -> dict[str, object]:
	return {"name": name, "backend": backend, "preparers": preparers, "status": "measured", "warm_median_seconds": median, "throughput_rows_per_second": 1000.0}


def test_selection_requires_ten_percent_improvement() -> None:
	configs = [_config("python-p3", "python", 3, 10.0), _config("java-p3", "java", 3, 9.1), _config("sql-p1", "sql", 1, 20.0)]
	assert _select_workload("raw_adoption_repair", configs)["selected_backend"] == "python"
	configs[1]["warm_median_seconds"] = 9.0
	result = _select_workload("raw_adoption_repair", configs)
	assert result["selected_backend"] == "java"
	assert result["candidates"][1]["qualifies"] is True


def test_stage10_rejects_incomplete_stage9(tmp_path: Path) -> None:
	study = {"status": "incomplete", "substage": {"9a": "complete", "9b": "complete", "9c": "pending"}}
	manifest = {"immutable_inputs": True}
	provisional = {"provisional": True}
	for name, value in (("study.json", study), ("manifest.json", manifest), ("decision.json", provisional), ("audit.json", {}), ("diagnostic.json", {})):
		(tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
	with pytest.raises(Stage10Error, match="aggregate is not complete"):
		build_stage10_decision(*(tmp_path / name for name in ("study.json", "manifest.json", "decision.json", "audit.json", "diagnostic.json")), tmp_path / "out.json")


def test_stage10_decision_preserves_provisional_identity(tmp_path: Path) -> None:
	root = Path(__file__).parents[2]
	study_path = root / "output/legacy-migration/benchmark/candidates/stage9c-compact-20261022-ready/study.json"
	manifest_path = root / "output/legacy-migration/benchmark/candidates/manifest.json"
	provisional_path = root / "output/legacy-migration/decision.json"
	audit_path = root / "output/legacy-migration/audit.json"
	diagnostic_path = root / "output/legacy-migration/benchmark/candidates/stage9c-independent-diagnostic-20261022-ready/diagnostic.json"
	if not all(path.exists() for path in (study_path, manifest_path, provisional_path, audit_path, diagnostic_path)):
		pytest.skip("Stage 9 evidence is not available")
	provisional_copy = tmp_path / "provisional.json"
	provisional = json.loads(provisional_path.read_text(encoding="utf-8"))
	provisional["provisional"] = True
	provisional["study_sha256"] = "517bf3d872e7149254493ae3177467653e4f7accf4c3daf798939119f035e0bd"
	provisional_copy.write_text(json.dumps(provisional), encoding="utf-8")
	decision = build_stage10_decision(study_path, manifest_path, provisional_copy, audit_path, diagnostic_path, tmp_path / "decision.json")
	assert decision["status"] == "complete"
	assert decision["workloads"]["raw_adoption_repair"]["selected_backend"] == "python"
	assert decision["workloads"]["derived_computation_persistence"]["selected_backend"] == "java"
	assert decision["workloads"]["zscores_persistence"]["selected_backend"] == "java"
	assert decision["provisional_decision"]["identity"] == "517bf3d872e7149254493ae3177467653e4f7accf4c3daf798939119f035e0bd"
