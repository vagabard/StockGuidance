import json
from dataclasses import replace
from pathlib import Path

from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.reporting.documents import ReportKind, load_report
from stockguidance.reporting.inspection import (
	compare_saved_runs,
	configuration_differences,
	inspect_saved_run,
	list_saved_runs,
)
from stockguidance.reporting.storage import write_report
from stockguidance.reporting.simulation import simulation_payload
from stockguidance.runs import RunConfig, RunDirectoryService, RunOperation, RunStatus
from stockguidance.simulation.persistence import persist_simulation_result
from stockguidance.workflows.recommend import recommend


def _save_simulation(simulation, root: Path, result=None):
	service = RunDirectoryService(root / "runs", project=root)
	handle = service.create(RunConfig(RunOperation.SIMULATION, {"fixture": True}))
	service.start(handle.run_id)
	persist_simulation_result(service, handle, simulation.run() if result is None else result, artifact_ids=(simulation.reference.artifact_id,))
	return service, handle


def test_saved_run_inspection_exposes_full_scores_performance_and_incomplete_details(simulation, tmp_path: Path) -> None:
	service, handle = _save_simulation(simulation, tmp_path)
	inspection = inspect_saved_run(service, handle.run_id, validate=True)

	assert inspection.report is not None and inspection.report.kind is ReportKind.SIMULATION_V2
	assert inspection.performance is not None
	assert inspection.performance.equity
	assert inspection.performance.drawdown
	assert inspection.performance.subportfolios
	assert inspection.performance.open_positions
	view = inspection.to_mapping()["view"]
	assert view["full_scores"] and view["top_scores"]
	assert view["trades"] and view["equity"]
	assert view["feature_target_units"]["target"]


def test_paired_schedule_comparison_keeps_separate_chart_series_and_labels_config_changes(simulation, tmp_path: Path) -> None:
	first = simulation.run()
	second = simulation.run(replace(simulation.request, allocation=replace(simulation.request.allocation, policy=AllocationPolicy.STAGGERED_HALVES)))
	service = RunDirectoryService(tmp_path / "runs", project=tmp_path)
	handles = []
	for result in (first, second):
		handle = service.create(RunConfig(RunOperation.SIMULATION, {"allocation": result.request.allocation.policy.value}))
		service.start(handle.run_id)
		persist_simulation_result(service, handle, result, artifact_ids=(simulation.reference.artifact_id,))
		handles.append(handle)
	left, right = (inspect_saved_run(service, item.run_id) for item in handles)
	comparison = compare_saved_runs((left, right))
	assert comparison.compatible and comparison.aggregatable
	assert all(item.performance is not None for item in comparison.inspections)

	differences = configuration_differences(left, right)
	assert isinstance(differences, tuple)


def test_saved_run_listing_filters_children_and_corrupt_or_missing_reports_are_visible(simulation, tmp_path: Path) -> None:
	service, parent = _save_simulation(simulation, tmp_path)
	child = service.create(RunConfig(RunOperation.REPLAY, {"fixture": True}, parent_run_id=parent.run_id))
	service.fail(child.run_id, stage="replay", code="fixture_failure", message="fixture failure")
	failed = inspect_saved_run(service, child.run_id)
	assert failed.manifest.status is RunStatus.FAILED and failed.report is None
	assert failed.to_mapping()["view"]["failure"]["code"] == "fixture_failure"

	report_path = service.handle(parent.run_id).reports_directory / "simulation.json"
	report_path.write_text("{", encoding="utf-8")
	opened = inspect_saved_run(service, parent.run_id)
	assert opened.report is None and opened.report_error == "Saved report is invalid or incompatible"
	assert list_saved_runs(service, parent_run_id=parent.run_id, operation=RunOperation.REPLAY)[0].run_id == child.run_id

	manifest_path = service.handle(child.run_id).manifest_path
	manifest_path.write_text(json.dumps({"schema": "stockguidance-run-1"}), encoding="utf-8")
	assert any(not item.valid and item.run_id == child.run_id for item in service.list_runs())


def test_current_recommendation_v2_and_simulation_v2_reports_remain_strictly_loadable(simulation, tmp_path: Path) -> None:
	path = tmp_path / "simulation.json"
	write_report(path, simulation_payload(simulation.run(), schema="stockguidance-simulation-2"))
	document = load_report(path)
	assert document.kind is ReportKind.SIMULATION_V2


def test_saved_recommendation_report_version_is_strictly_loadable(recommender, tmp_path: Path) -> None:
	service = RunDirectoryService(tmp_path / "runs", project=tmp_path)
	recommendation = recommend(recommender.job, recommender.storage.boundary, recommender.calendar, recommender.services, recommender.storage.clock, runs=service)
	run_id = service.list_runs(include_invalid=False)[0].run_id
	path = service.handle(run_id).reports_directory / "recommendation.json"
	document = load_report(path)
	assert document.kind in (ReportKind.RECOMMENDATION, ReportKind.RECOMMENDATION_V2)
	assert recommendation.recommendation.score_units == document.payload["score_units"]
