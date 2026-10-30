from pathlib import Path
from typing import Any, cast

from tools.training_recommendation_simulation_stage12 import run


def test_stage12_offline_workflow_rehearsal(tmp_path: Path) -> None:
	summary = cast(dict[str, Any], run(tmp_path / "stage12"))

	fixture = summary["fixture"]
	assert fixture["warmup_sessions"] >= 60
	assert fixture["simulation_sessions"] == 10
	assert fixture["selected_ticker_ids"] == [101, 202]
	assert {item["label"] for item in fixture["excluded"]} == {"GAP", "LOWVOL"}
	assert any("missing_observation" in item["reasons"] for item in fixture["excluded"] if item["label"] == "GAP")
	assert any("average_volume_below_lower_bound" in item["reasons"] for item in fixture["excluded"] if item["label"] == "LOWVOL")

	assert summary["preparation"] == {"validated": 1, "prepared": 1}
	assert summary["training"]["raw_artifact_reloaded"] is True
	assert summary["training"]["raw_target"] == "raw_return-v2"
	assert summary["training"]["ranking_target"] == "return_zscore_cross_sectional-v2"
	assert summary["training"]["ranking_objective"] == "pairwise_ranking"

	recommendation = summary["recommendation"]
	assert recommendation["validate_only_calls"] == 1
	assert recommendation["provider_calls"] == 0
	assert recommendation["symbols"] == ["AAA", "BBB"]

	simulation = summary["simulation"]
	assert simulation["alternate_successful"] is True
	assert simulation["staggered_successful"] is True
	assert simulation["replay_matches"] is True
	assert simulation["rerun_successful"] is True
