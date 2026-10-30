"""Saved simulation attempts and explicit offline replay/rerun services."""

from dataclasses import asdict, replace
import json
from pathlib import Path
from typing import Mapping

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.identity import encode_json, mapping
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.reporting.simulation import simulation_payload
from stockguidance.runs.contracts import ReportState, RunConfig, RunOperation
from stockguidance.runs.service import RunDirectoryService, RunHandle
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.orchestration import PairedSimulation, simulate_paired
from stockguidance.simulation.prices import ExecutionPriceSource
from stockguidance.simulation.replay import SavedExecutionPrices, SavedScoreSource
from stockguidance.simulation.result import SimulationResult
from stockguidance.simulation.score_source import ScoreSource


def simulation_settings(request: SimulationRequest, policy: str) -> dict[str, object]:
	return {
		"policy": policy,
		"source": asdict(request.source),
		"dates": {"start": request.start.isoformat(), "end": request.end.isoformat(), "anchor": request.allocation.anchor_session.session_date.isoformat()},
		"features": asdict(request.features),
		"target": asdict(request.target),
		"allocation": request.allocation.policy.value,
		"initial_capital": str(request.initial_capital),
		"decision_lead_seconds": request.decision_lead.total_seconds(),
		"eligibility": asdict(request.eligibility),
		"simulation_top_k": request.top_k,
		"execution": asdict(request.execution),
		"availability_policy": request.availability_policy.value,
		"data_snapshot": None if request.data_snapshot is None else request.data_snapshot.isoformat(),
	}


def _universe(result: SimulationResult) -> dict[str, object]:
	if not result.forecasts:
		return {"ticker_ids": list(result.request.features.ticker_ids), "ticker_labels": []}
	labels = [item.symbol for item in result.forecasts[0].prediction.scores]
	return {"ticker_ids": list(result.request.features.ticker_ids), "ticker_labels": labels}


def _sections(result: SimulationResult, report: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
	decision_value = report.get("decisions")
	if not isinstance(decision_value, list):
		raise ValueError("Simulation report decisions must be a list")
	decisions = tuple(mapping(item) for item in decision_value)
	eligibility: list[Mapping[str, object]] = []
	for decision in decisions:
		rows = decision.get("eligibility")
		if isinstance(rows, list):
			eligibility.extend(mapping(item) for item in rows)
	return {
		"identity": {"source": asdict(result.request.source), "calendar": {"id": result.request.allocation.anchor_session.calendar_id, "version": result.calendar_version}, "execution_revision": report["execution_revision"]},
		"resolution": {"start_session": result.request.start.isoformat(), "end_session": result.request.end.isoformat(), "anchor_session": result.request.allocation.anchor_session.session_date.isoformat(), "decision_cutoff": result.execution_audit_cutoff.isoformat()},
		"universe": _universe(result),
		"specifications": {"features": asdict(result.request.features), "target": asdict(result.request.target), "allocation": result.request.allocation.policy.value, "score_policy": result.score_policy.policy.value, "top_k": result.request.top_k},
		"provenance": {"execution_audit_cutoff": result.execution_audit_cutoff.isoformat(), "execution_revision": report["execution_revision"], "point_in_time_replay": report["point_in_time_replay"]},
		"results": {
			"predictions": {"schema": "simulation-decision-scores-1", "rows": decisions, "scope": "ordered_model_universe_before_top_k"},
			"eligibility": tuple(eligibility),
			"exclusions": [item for item in eligibility if item.get("state") != "eligible"],
			"metrics": {"successful": result.successful, "complete": result.complete, "final_equity": None if not result.valuations else str(result.valuations[-1].total_equity), "training_runs": len(result.training_runs)},
			"reports": {"path": "reports/simulation.json", "schema": report["schema"]},
			"trades": report["realized_trades"], "equity": report["equity"], "benchmarks": report["benchmarks"],
		},
	}


def persist_simulation_result(service: RunDirectoryService, handle: RunHandle, result: SimulationResult, *, operation: RunOperation = RunOperation.SIMULATION, artifact_ids: tuple[str, ...] = ()) -> dict[str, object]:
	"""Write the complete report and manifest sections before finalizing a run."""
	report = simulation_payload(result, schema="stockguidance-simulation-2")
	atomic_write(handle.reports_directory / "simulation.json", encode_json(report))
	for index, artifact_id in enumerate(dict.fromkeys((*artifact_ids, *(training.artifact.artifact_id for _, training in result.training_runs)))):
		service.register_external(handle.run_id, name=f"model-{index}", kind="artifact", sha256=artifact_id, external_reference="saved-or-run-model")
	service.update_sections(handle.run_id, resolved_configuration=simulation_settings(result.request, result.score_policy.policy.value), sections=_sections(result, report))
	if result.successful:
		if operation is RunOperation.SIMULATION and not result.complete:
			service.complete(handle.run_id, report_state=ReportState.INCOMPLETE, incomplete_report={"reason": "simulation contains skipped orders, missing fills or stale marks", "complete": False})
		else:
			service.complete(handle.run_id)
	else:
		service.fail(handle.run_id, stage="simulation", code="required_stage_failure", message="Historical simulation produced a required-stage failure")
	return report


def _start_child(service: RunDirectoryService, operation: RunOperation, settings: Mapping[str, object], parent_run_id: str) -> RunHandle:
	handle = service.create(RunConfig(operation, settings, parent_run_id=parent_run_id))
	service.start(handle.run_id)
	return handle


def replay_saved_simulation(
	service: RunDirectoryService,
	parent_run_id: str,
	request: SimulationRequest,
	boundary: HistoryBoundary,
	calendar: SessionCalendar,
	*,
	top_k: int | None = None,
	allocation_policy: AllocationPolicy | None = None,
) -> SimulationResult:
	"""Replay saved scores and quotes without a model, database, or provider."""
	parent = service.validate(parent_run_id)
	if parent.operation is not RunOperation.SIMULATION:
		raise ValueError("Saved-score replay requires a simulation parent run")
	report_path = service.handle(parent_run_id).reports_directory / "simulation.json"
	report = mapping(json.loads(report_path.read_text(encoding="utf-8")))
	updated = request
	if top_k is not None or allocation_policy is not None:
		updated = replace(updated, top_k=updated.top_k if top_k is None else top_k, allocation=replace(updated.allocation, policy=updated.allocation.policy if allocation_policy is None else allocation_policy))
	settings = simulation_settings(updated, "saved_score_replay") | {"parent_run_id": parent_run_id, "changed_top_k": top_k, "changed_allocation": None if allocation_policy is None else allocation_policy.value}
	handle = _start_child(service, RunOperation.REPLAY, settings, parent_run_id)
	try:
		scores = SavedScoreSource(report, updated, calendar)
		prices = SavedExecutionPrices.from_report(report, updated.source, calendar, updated.features.ticker_ids)
		result = simulate(updated, boundary, calendar, scores, prices)
		persist_simulation_result(service, handle, result, operation=RunOperation.REPLAY)
		return result
	except Exception as error:
		try:
			service.fail(handle.run_id, stage="replay", code=type(error).__name__, message=str(error))
		except Exception:
			pass
		raise


def rerun_pinned_simulation(
	service: RunDirectoryService,
	parent_run_id: str,
	request: SimulationRequest,
	boundary: HistoryBoundary,
	calendar: SessionCalendar,
	scores: ScoreSource,
	prices: ExecutionPriceSource,
	*,
	data_revision: str | None = None,
	re_resolved: bool = False,
) -> SimulationResult:
	"""Run a new child with the parent's resolved dates and explicit data choice."""
	parent = service.validate(parent_run_id)
	if parent.operation is not RunOperation.SIMULATION:
		raise ValueError("Pinned simulation rerun requires a simulation parent run")
	settings = simulation_settings(request, "pinned_rerun") | {"parent_run_id": parent_run_id, "re_resolved": re_resolved, "data_revision": data_revision}
	handle = _start_child(service, RunOperation.RERUN, settings, parent_run_id)
	try:
		result = simulate(request, boundary, calendar, scores, prices)
		persist_simulation_result(service, handle, result, operation=RunOperation.RERUN)
		return result
	except Exception as error:
		try:
			service.fail(handle.run_id, stage="rerun", code=type(error).__name__, message=str(error))
		except Exception:
			pass
		raise


def persist_paired_simulation(
	service: RunDirectoryService,
	request: SimulationRequest,
	boundary: HistoryBoundary,
	calendar: SessionCalendar,
	scores: ScoreSource,
	prices: ExecutionPriceSource,
) -> PairedSimulation:
	"""Persist one immutable comparison parent and two independent children."""
	parent = service.create(RunConfig(RunOperation.SIMULATION, simulation_settings(request, "paired_schedules")))
	service.start(parent.run_id)
	try:
		paired = simulate_paired(request, boundary, calendar, scores, prices)
		child_ids: list[str] = []
		for name, result in (("alternate_sessions", paired.alternate_sessions), ("staggered_halves", paired.staggered_halves)):
			child = _start_child(service, RunOperation.SIMULATION, simulation_settings(result.request, "paired_schedule") | {"paired_parent": parent.run_id, "schedule": name}, parent.run_id)
			persist_simulation_result(service, child, result)
			child_ids.append(child.run_id)
		service.update_sections(parent.run_id, sections={"results": {"paired_children": child_ids, "schedules": ["alternate_sessions", "staggered_halves"]}})
		service.complete(parent.run_id)
		return paired
	except Exception as error:
		try:
			service.fail(parent.run_id, stage="paired_simulation", code=type(error).__name__, message=str(error))
		except Exception:
			pass
		raise
