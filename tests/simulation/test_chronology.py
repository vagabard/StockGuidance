from dataclasses import replace
from datetime import date, timedelta

import pytest

from stockguidance.artifacts.exporting import export_artifact
from stockguidance.artifacts.loading import load_artifact
from stockguidance.datasets.training import build_training_dataset
from stockguidance.features.specification import CloseFeature
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.recommendations import scoring
from stockguidance.simulation.events import EventKind
from stockguidance.simulation.score_source import ChronologicalModelScores, FixedModelScores, WalkForwardTraining
from stockguidance.workflows.train_model import train_from_history


def chronological_source(simulation, *, cadence=2, trainer=None):
	plan = WalkForwardTraining(tuple(simulation.samples.decision(day) for day in (2, 3, 5, 8, 9, 10, 11)), simulation.configuration, simulation.tmp_path / "walk-forward", cadence)
	return ChronologicalModelScores(plan, simulation.signals, trainer=trainer)


@pytest.mark.parametrize("policy,training_days", [(AllocationPolicy.ALTERNATE_SESSIONS, (5, 11)), (AllocationPolicy.STAGGERED_HALVES, (5, 9, 11))])
def test_retraining_cadence_counts_scheduled_buys_and_excludes_immature_labels(simulation, policy, training_days):
	captured = []

	def trainer(request, boundary, calendar, publication, repository):
		dataset = build_training_dataset(request.decisions, request.training_cutoff, request.features, request.target, boundary, calendar, publication, repository)
		assert all(sample.timing.exit_session.closes_at < request.training_cutoff for sample in dataset.eligible)
		assert all(sample.timing.entry_session.sequence < calendar.session(request.training_cutoff.astimezone(calendar.timezone).date()).sequence for sample in dataset.samples)
		previous = tuple(sample for sample in dataset.samples if sample.timing.exit_session.session_date == request.training_cutoff.astimezone(calendar.timezone).date())
		assert previous and all(sample.target_state.value == "not_yet_available" for sample in previous)
		captured.append((request, dataset))
		return train_from_history(replace(request, artifact_path=request.artifact_path.with_name("actual-" + request.artifact_path.name)), boundary, calendar, publication, repository)

	request = replace(simulation.request, allocation=replace(simulation.request.allocation, policy=policy))
	source = chronological_source(simulation, trainer=trainer)
	result = simulation.run(request, source=source)
	assert result.successful
	assert tuple(item[0].training_cutoff.day for item in captured) == training_days
	assert len(result.training_runs) == len(training_days)
	assert tuple(timing.entry_session.session_date.day for timing, _ in result.training_runs) == training_days
	assert all(training.artifact.path.name.startswith("actual-") for _, training in result.training_runs)
	assert not any(path.name.startswith("decision-") for path in source.plan.artifact_directory.glob("*.sgmodel"))
	assert result.score_policy.retrain_every_buy_decisions == 2
	assert captured[0][1].eligible[0].timing.entry_session.session_date == date(2024, 7, 2)
	assert not any(item.timing.entry_session.session_date.day == 3 for item in captured[0][1].eligible)
	with pytest.raises(ValueError, match="fresh"):
		simulation.run(request, source=source)


def test_skipped_required_input_still_consumes_the_anchored_retraining_cadence(simulation):
	simulation.liquidity.missing.add((simulation.calendar.session(date(2024, 7, 3)), 17))
	source = chronological_source(simulation)
	result = simulation.run(source=source)
	assert result.successful
	assert tuple(timing.entry_session.session_date.day for timing, _ in result.training_runs) == (5, 11)
	assert tuple(item.sample.timing.entry_session.session_date.day for item in result.forecasts) == (9, 11)
	assert result.forecasts[0].artifact_id == result.training_runs[0][1].artifact.artifact_id


def test_future_fixed_model_is_rejected_before_any_signal_or_execution_reads(simulation):
	loaded = load_artifact(simulation.reference)
	cutoff = simulation.samples.decision(5).decision_cutoff + timedelta(seconds=1)
	metadata = replace(loaded.metadata, training=replace(loaded.metadata.training, training_cutoff=cutoff, information_cutoff=cutoff))
	reference = export_artifact(simulation.tmp_path / "future.sgmodel", loaded.model, metadata)
	simulation.samples.repository.calls.clear()
	with pytest.raises(ValueError, match="information after"):
		simulation.run(source=FixedModelScores(reference, simulation.signals))
	assert not simulation.samples.repository.calls and not simulation.prices.calls


def test_fixed_model_fitted_on_the_evaluation_entry_is_rejected_even_before_decision(simulation):
	loaded = load_artifact(simulation.reference)
	session = simulation.calendar.session(date(2024, 7, 5))
	cutoff = session.opens_at + timedelta(minutes=1)
	metadata = replace(loaded.metadata, training=replace(loaded.metadata.training, training_cutoff=cutoff, information_cutoff=cutoff))
	reference = export_artifact(simulation.tmp_path / "same-day.sgmodel", loaded.model, metadata)
	with pytest.raises(ValueError, match="before the evaluation"):
		simulation.run(source=FixedModelScores(reference, simulation.signals))


def test_equal_width_feature_mismatch_is_rejected_before_market_reads(simulation):
	request = replace(simulation.request, features=replace(simulation.request.features, feature_order=(CloseFeature.LOG_CLOSE,)))
	simulation.samples.repository.calls.clear()
	with pytest.raises(ValueError, match="specifications differ"):
		simulation.run(request)
	assert not simulation.prices.calls and not simulation.samples.repository.calls


def test_fixed_predictions_read_only_prior_sessions_at_each_decision(simulation, monkeypatch):
	simulation.samples.repository.calls.clear()
	predictions = []
	original = scoring.score_sample

	def score(artifact, sample):
		assert sample.label_cutoff is None and sample.target_state.value == "not_requested"
		assert all(item.sequence < sample.timing.entry_session.sequence for item in sample.features.sessions)
		predictions.append((artifact.artifact_id, sample.feature_cache_key))
		return original(artifact, sample)

	monkeypatch.setattr(scoring, "score_sample", score)
	first = simulation.run()
	assert all(session.closes_at < query.scope.as_of for query in simulation.samples.repository.calls for session in query.scope.sessions)
	assert all(len(query.scope.sessions) == 1 for query in simulation.liquidity.calls)
	simulation.samples.revise(3, 17, 500.0)
	second = simulation.run()
	assert len(predictions) == 6
	assert first.forecasts[0].sample.feature_cache_key != second.forecasts[0].sample.feature_cache_key
	assert first.forecasts[0].prediction.data_revision != second.forecasts[0].prediction.data_revision


def test_retraining_failure_cannot_silently_continue_using_an_old_model(simulation):
	calls = []

	def trainer(*arguments):
		calls.append(arguments[0])
		if len(calls) == 2:
			raise RuntimeError("fixture-password-never-log")
		return train_from_history(*arguments)

	result = simulation.run(source=chronological_source(simulation, trainer=trainer))
	assert not result.successful and not result.complete
	assert len(result.training_runs) == 1 and len(result.forecasts) == 2
	assert any(item.kind is EventKind.FAILURE and item.session.session_date.day == 11 for item in result.events)
	assert "password" not in repr(result.events)
