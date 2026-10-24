from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import torch

from stockguidance.artifacts.exporting import export_artifact
from stockguidance.artifacts.loading import load_artifact
from stockguidance.database.repositories.signals import SignalRepository
from stockguidance.features.specification import CloseFeature, SignalMode
from stockguidance.market_data.opening_refresh import OpeningRefresh
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.market_data.openings import OpeningObservation
from stockguidance.models.policy import ModelRefreshPolicy, ModelSelection
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.events import EventKind
from stockguidance.simulation.score_source import FixedModelScores, HistoricalSignals
from stockguidance.validation.findings import ValidationCheck
from stockguidance.validation.repair import RepairAction
from stockguidance.workflows.failures import StageExecutionError, WorkflowStage
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history
from stockguidance.workflows.validate_database import ValidationSettings, validate_database


def opening_model(simulation, opening_signals, tmp_path):
	features = replace(opening_signals.features, lookback_sessions=1)
	samples = simulation.samples
	cutoff = samples.decision(5).previous_session.closes_at + timedelta(minutes=16)
	training = train_from_history(HistoryTrainingRequest((samples.decision(2),), cutoff, features, samples.target, simulation.configuration, tmp_path / "trained-open.sgmodel"), samples.boundary, samples.calendar, samples.publication, opening_signals.repository)
	loaded = load_artifact(training.artifact)
	with torch.no_grad():
		for parameter in loaded.model.parameters():
			parameter.zero_()
		linear = tuple(module for module in loaded.model.modules() if isinstance(module, torch.nn.Linear))[-1]
		linear.weight[0, 1] = 1
		linear.weight[1, 3] = 1
	return features, export_artifact(tmp_path / "opening-ranker.sgmodel", loaded.model, loaded.metadata)


@pytest.mark.parametrize("mode", list(SignalMode))
@pytest.mark.parametrize("allocation", list(AllocationPolicy))
def test_both_signals_and_schedules_keep_close_fills_and_independent_cash_rest(simulation, opening_signals, tmp_path, mode, allocation):
	features, reference = (simulation.request.features, simulation.reference) if mode is SignalMode.PREVIOUS_CLOSE else opening_model(simulation, opening_signals, tmp_path)
	signals = HistoricalSignals(simulation.samples.boundary, simulation.calendar, simulation.samples.publication, opening_signals.repository, simulation.liquidity)
	request = replace(simulation.request, features=features, allocation=replace(simulation.request.allocation, policy=allocation))
	result = simulate(request, simulation.samples.boundary, simulation.calendar, FixedModelScores(reference, signals), simulation.prices)
	assert result.successful and result.complete
	assert all(event.at == event.session.closes_at for event in result.events if event.kind in {EventKind.BUY, EventKind.SALE})
	assert all(trade.position.entry_price == simulation.prices.prices[trade.position.entry_session, trade.position.ticker_id] for trade in result.trades)
	assert all(trade.position.exit_session.sequence == trade.position.entry_session.sequence + 1 for trade in result.trades)
	assert len(result.final.allocations) == (1 if allocation is AllocationPolicy.ALTERNATE_SESSIONS else 2)
	for plan in result.plans:
		if plan.orders:
			balance = next(item for item in plan.portfolio.allocations if item.name == plan.allocation)
			assert sum(order.maximum_debit for order in plan.orders) <= balance.available_cash
	if allocation is AllocationPolicy.STAGGERED_HALVES:
		assert any(value.allocations[0].equity != value.allocations[1].equity for value in result.valuations)


def test_openings_change_selection_not_execution_prices_or_horizon(simulation, opening_signals, tmp_path):
	features, reference = opening_model(simulation, opening_signals, tmp_path)
	request = replace(simulation.request, features=features)
	signals = HistoricalSignals(simulation.samples.boundary, simulation.calendar, simulation.samples.publication, opening_signals.repository, simulation.liquidity)
	first = simulate(request, simulation.samples.boundary, simulation.calendar, FixedModelScores(reference, signals), simulation.prices)
	session = simulation.samples.decision().entry_session
	for ticker_id, price in ((17, 60.5), (3, 90.0)):
		opening_signals.store.save(simulation.request.source, OpeningObservation(ticker_id, f"T{ticker_id}", session, price, session.opens_at + timedelta(minutes=2), f"fixture-changed-open-{ticker_id}"))
	second = simulate(request, simulation.samples.boundary, simulation.calendar, FixedModelScores(reference, signals), simulation.prices)
	assert first.forecasts[0].prediction.ranking[0].ticker_id == 17
	assert second.forecasts[0].prediction.ranking[0].ticker_id == 3
	assert first.trades[0].position.entry_price == 100 and second.trades[0].position.entry_price == 50
	assert first.trades[0].position.entry_session == second.trades[0].position.entry_session == session
	assert first.trades[0].position.exit_session == second.trades[0].position.exit_session == simulation.samples.decision().exit_session


def test_artifact_signal_cannot_be_toggled_even_with_equal_input_dimensions(simulation, opening_signals, tmp_path):
	features, reference = opening_model(simulation, opening_signals, tmp_path)
	other = replace(features, signal_mode=SignalMode.PREVIOUS_CLOSE, feature_order=(CloseFeature.RELATIVE_CLOSE, CloseFeature.LOG_CLOSE))
	assert features.input_size == other.input_size
	with pytest.raises(ValueError, match="specifications differ"):
		FixedModelScores(reference, HistoricalSignals(simulation.samples.boundary, simulation.calendar, simulation.samples.publication, opening_signals.repository, simulation.liquidity)).validate(replace(simulation.request, features=other), simulation.samples.decision(), simulation.calendar)
	metadata = load_artifact(reference).metadata
	assert metadata.semantics.features == features
	assert metadata.semantics.target == simulation.request.target
	assert "password" not in repr(metadata).lower() and "api_key" not in repr(metadata).lower()


def recommendation_openings(recommender):
	features = replace(recommender.job.features, signal_mode=SignalMode.ENTRY_OPEN, feature_order=(CloseFeature.RELATIVE_CLOSE, CloseFeature.ENTRY_OPEN_RETURN))
	store = OpeningStore(recommender.job.source.data_root)
	for timing in recommender.plan.decisions:
		for ticker_id in features.ticker_ids:
			store.save(recommender.job.source.identity, OpeningObservation(ticker_id, "AAA", timing.entry_session, 100.0, timing.entry_session.opens_at + timedelta(minutes=2), f"historical-fixture-{timing.entry_session.session_date}"))
	calls = []

	def fetch(source, session, ticker_id, symbol, clock):
		calls.append((session, ticker_id, symbol))
		return OpeningObservation(ticker_id, symbol, session, 123.0, clock.now(), "live-fixture-opening")

	provider = SimpleNamespace(fetch=fetch)
	refresh = OpeningRefresh(recommender.job.source, store, provider, recommender.closes)
	services = replace(recommender.services, closes=SignalRepository(recommender.closes, store), openings=refresh)
	return features, store, services, calls


@pytest.mark.parametrize("mode", list(ModelRefreshPolicy))
@pytest.mark.parametrize("allocation", list(AllocationPolicy))
def test_both_recommendation_policies_capture_full_openings_before_shared_prediction(recommender, mode, allocation, tmp_path):
	features, store, services, calls = recommendation_openings(recommender)
	training_request = HistoryTrainingRequest(recommender.plan.decisions, recommender.storage.clock.now(), features, recommender.job.target, recommender.plan.configuration, tmp_path / "current-open.sgmodel")
	training = train_from_history(training_request, recommender.storage.boundary, recommender.calendar, recommender.job.publication, services.closes)
	job = replace(recommender.job, features=features, allocation=replace(recommender.job.allocation, policy=allocation), model=ModelSelection(mode, training.artifact.artifact_id if mode is ModelRefreshPolicy.CURRENT else None), current_artifact=training.artifact if mode is ModelRefreshPolicy.CURRENT else None, retraining=None if mode is ModelRefreshPolicy.CURRENT else recommender.plan)
	result = recommender.run(job, services_override=services)
	assert result.recommendation.request.features == features
	assert result.sample.features.values is not None
	assert [ticker_id for session, ticker_id, symbol in calls] == list(features.ticker_ids)
	assert result.sample.timing.entry_session.session_date == date(2024, 7, 9)
	assert all(session.session_date < date(2024, 7, 9) for source, session in recommender.storage.provider.calls)
	assert (result.training is not None) == (mode is ModelRefreshPolicy.RETRAIN)
	assert not result.order_plan


def test_missing_full_universe_openings_stop_before_retraining(recommender):
	features, store, services, calls = recommendation_openings(recommender)
	services.openings.provider.fetch = lambda *values: None
	job = replace(recommender.job, features=features, model=ModelSelection(ModelRefreshPolicy.RETRAIN), current_artifact=None, retraining=recommender.plan)
	with pytest.raises(StageExecutionError) as error:
		recommender.run(job, services_override=services, trainer=lambda *args: pytest.fail("Missing opening reached training"))
	assert error.value.failure.stage is WorkflowStage.REFRESH


def test_opening_validation_is_read_only_and_repair_does_not_fetch_daily_data(recommender):
	features, store, services, calls = recommendation_openings(recommender)
	settings = ValidationSettings(date(2024, 7, 3), date(2024, 7, 8), recommender.job.publication, recommender.storage.clock.now(), features)
	commits = recommender.storage.database.commits
	result = validate_database(recommender.job.source, settings, recommender.storage.boundary, recommender.calendar, recommender.services.validation, recommender.storage.clock, openings=store)
	assert result.complete
	session = recommender.plan.decisions[0].entry_session
	store.path_for(recommender.job.source.identity, session.session_date.isoformat(), features.ticker_ids[0]).unlink()
	result = validate_database(recommender.job.source, settings, recommender.storage.boundary, recommender.calendar, recommender.services.validation, recommender.storage.clock, openings=store)
	assert not result.complete and any(item.check is ValidationCheck.OPENING for item in result.findings)
	assert any(item.action is RepairAction.CAPTURE_OPENING for item in result.repair_plan.items)
	with pytest.raises(ValueError, match="manual"):
		result.repair_plan.validate_for_update(recommender.job.source.identity, settings.start, settings.end, recommender.storage.clock.now())
	assert not calls and recommender.storage.database.commits == commits
