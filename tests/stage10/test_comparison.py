from dataclasses import replace
from decimal import Decimal
import json

import pytest

from stockguidance.artifacts.loading import load_artifact
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.simulation.events import EventKind
from stockguidance.simulation.score_source import HistoricalSignals, WalkForwardTraining
from stockguidance.simulation.signal_comparison import compare_signal_modes


@pytest.mark.parametrize("allocation", list(AllocationPolicy))
@pytest.mark.parametrize("missing", [False, True])
def test_small_chronological_comparison_uses_matched_cohorts_budget_and_seed(simulation, opening_signals, tmp_path, allocation, missing):
	samples = simulation.samples
	request = replace(simulation.request, allocation=replace(simulation.request.allocation, policy=allocation))
	simulation.prices.prices = {(item.timing.session, item.ticker_id): Decimal(str(item.close)) for item in samples.repository.observations.values() if request.start <= item.timing.session.session_date <= request.end}
	if missing:
		for day in (2, 5):
			session = samples.decision(day).entry_session
			opening_signals.store.path_for(request.source, session.session_date.isoformat(), 3).unlink()
	signals = HistoricalSignals(samples.boundary, samples.calendar, samples.publication, opening_signals.repository, simulation.liquidity)
	plan = WalkForwardTraining(tuple(samples.decision(day) for day in (2, 3, 5, 8, 9, 10, 11)), simulation.configuration, tmp_path / "paired", 2)
	comparison = compare_signal_modes(request, samples.boundary, samples.calendar, signals, simulation.prices, plan)
	previous, opened = comparison.previous_close, comparison.entry_open
	assert previous.successful and opened.successful
	assert previous.request.allocation == opened.request.allocation
	assert previous.request.initial_capital == opened.request.initial_capital == request.initial_capital
	assert previous.request.features.ticker_ids == opened.request.features.ticker_ids
	assert previous.request.features.lookback_sessions == opened.request.features.lookback_sessions
	assert previous.request.target == opened.request.target
	assert [item.sample.timing for item in previous.forecasts] == [item.sample.timing for item in opened.forecasts]
	assert [timing for timing, result in previous.training_runs] == [timing for timing, result in opened.training_runs]
	assert [result.training_samples for timing, result in previous.training_runs] == [result.training_samples for timing, result in opened.training_runs]
	for result in (previous, opened):
		for timing, training in result.training_runs:
			metadata = load_artifact(training.artifact).metadata
			assert metadata.training.last_exit < timing.entry_session.session_date
			assert metadata.training.training_cutoff == timing.decision_cutoff
			assert metadata.training.seed == comparison.seed == 42
			assert metadata.training.completed_epochs == comparison.epochs == 1
		assert all(event.at == event.session.closes_at for event in result.events if event.kind in {EventKind.BUY, EventKind.SALE})
	if missing:
		assert comparison.excluded_decisions[0].entry == request.start
		assert any(item.entry == samples.decision(2).entry_session.session_date for item in comparison.excluded_training)
		assert not previous.complete and not opened.complete
	else:
		assert previous.complete and opened.complete
		assert not comparison.excluded_decisions
	payload = comparison.payload()
	assert payload["cohort_policy"] == "intersection_at_each_training_and_decision_cutoff"
	assert payload["every_buy_decisions"] == 2
	assert "password" not in json.dumps(payload) and "api_key" not in json.dumps(payload)
	path = tmp_path / f"comparison-{allocation.value}-{missing}.json"
	path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
	print(json.dumps({"allocation": allocation.value, "missing_openings": missing, "previous_close_equity": str(previous.valuations[-1].total_equity), "entry_open_equity": str(opened.valuations[-1].total_equity), "excluded_decisions": len(comparison.excluded_decisions), "seed": comparison.seed, "epochs": comparison.epochs}, sort_keys=True))
