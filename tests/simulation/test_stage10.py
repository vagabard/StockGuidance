from dataclasses import replace

import pytest

from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.runs import RunConfig, RunDirectoryService, RunOperation
from stockguidance.simulation.orchestration import simulate_paired
from stockguidance.simulation.persistence import persist_simulation_result, replay_saved_simulation
from stockguidance.simulation.score_source import FixedModelScores, PreEvaluationTraining, TrainThenSimulateScores
from stockguidance.simulation.engine import simulate


def _saved_simulation(simulation, tmp_path):
    service = RunDirectoryService(tmp_path / "runs")
    handle = service.create(RunConfig(RunOperation.SIMULATION, {"fixture": True}))
    service.start(handle.run_id)
    result = simulation.run()
    persist_simulation_result(service, handle, result, artifact_ids=(simulation.reference.artifact_id,))
    return service, handle, result


def test_train_then_simulate_fits_once_before_first_evaluation(simulation, tmp_path):
    plan = PreEvaluationTraining((simulation.samples.decision(2),), simulation.configuration, tmp_path / "pre-period")
    scores = TrainThenSimulateScores(plan, simulation.signals)
    result = simulate(simulation.request, simulation.samples.boundary, simulation.calendar, scores, simulation.prices)
    assert result.successful
    assert len(result.training_runs) == 1
    assert len(result.forecasts) == 3
    timing, training = result.training_runs[0]
    assert training.artifact.path.parent == tmp_path / "pre-period"
    assert training.artifact.path.exists()
    assert training.artifact.path != tmp_path / "pre-period" / "decision-2024-07-05.sgmodel"
    assert training.artifact.path.name == "pre-evaluation.sgmodel"
    assert training.artifact.artifact_id


def test_paired_schedules_share_frozen_forecasts_and_have_independent_ledgers(simulation):
    paired = simulate_paired(simulation.request, simulation.samples.boundary, simulation.calendar, FixedModelScores(simulation.reference, simulation.signals), simulation.prices)
    alternate, staggered = paired.results
    assert alternate.successful and staggered.successful
    assert alternate.request.allocation.policy is AllocationPolicy.ALTERNATE_SESSIONS
    assert staggered.request.allocation.policy is AllocationPolicy.STAGGERED_HALVES
    assert alternate.forecasts[0].prediction == staggered.forecasts[0].prediction
    assert alternate.forecasts[0].sample == staggered.forecasts[0].sample
    assert len(staggered.forecasts) >= len(alternate.forecasts)
    assert alternate.final is not staggered.final


def test_saved_score_replay_matches_original_and_changed_top_k_uses_full_coverage(simulation, tmp_path):
    service, handle, original = _saved_simulation(simulation, tmp_path)
    parent_before = service.read(handle.run_id).to_mapping()
    replay = replay_saved_simulation(service, handle.run_id, simulation.request, simulation.samples.boundary, simulation.calendar)
    assert replay.final == original.final
    assert tuple(item.total_equity for item in replay.valuations) == tuple(item.total_equity for item in original.valuations)
    changed = replay_saved_simulation(service, handle.run_id, replace(simulation.request, top_k=2), simulation.samples.boundary, simulation.calendar, top_k=2)
    assert changed.successful
    assert all(len(item.prediction.ranking) <= 2 for item in changed.forecasts)
    children = service.children(handle.run_id)
    assert len(children) == 2
    assert service.read(handle.run_id).to_mapping() == parent_before


def test_saved_score_replay_rejects_schedule_without_saved_score_coverage(simulation, tmp_path):
    service, handle, _ = _saved_simulation(simulation, tmp_path)
    with pytest.raises(ValueError, match="scores|coverage"):
        replay_saved_simulation(service, handle.run_id, replace(simulation.request, allocation=replace(simulation.request.allocation, policy=AllocationPolicy.STAGGERED_HALVES)), simulation.samples.boundary, simulation.calendar, allocation_policy=AllocationPolicy.STAGGERED_HALVES)
