from dataclasses import replace
from datetime import date, timedelta
import math

import pytest

from stockguidance.config.source import PriceAdjustment
from stockguidance.database.repositories.closes import CloseData
from stockguidance.datasets.building import build_sample
from stockguidance.datasets.sample import TargetState
from stockguidance.features.construction import FeatureValues, construct_features
from stockguidance.features.history_window import WindowState
from stockguidance.features.specification import CloseFeature, FeatureSpec, SignalMode
from stockguidance.market_data.closes import CloseObservation, PriceState
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.construction import close_to_close_return
from stockguidance.targets.specification import TargetSpec


def test_hand_calculated_features_use_only_prior_closes_in_exact_order(samples):
	sample = samples.build()
	assert sample.features.values == pytest.approx((0, 0, .1, -.2, .21, .2))
	assert tuple(session.session_date.day for session in sample.features.sessions) == (1, 2, 3)
	assert sample.features.specification.ticker_ids == (17, 3)
	assert sample.features.specification.shape == (3, 2, 1)
	assert sample.features.specification.layout == "session_oldest_first,ticker,feature:C"
	assert sample.target_state is TargetState.NOT_REQUESTED
	assert sample.targets.raw == (None, None)
	assert not sample.training_eligible
	query = samples.repository.calls[0]
	assert query.scope.sessions == sample.features.sessions
	assert all(session.sequence < sample.timing.entry_session.sequence for session in query.scope.sessions)


def test_training_and_inference_features_match_with_mature_close_targets(samples):
	inference = samples.build()
	training = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	assert training.features == inference.features
	assert training.feature_cache_key == inference.feature_cache_key
	assert training.targets.raw == pytest.approx((.05, -.1))
	assert training.targets.transformed == training.targets.raw
	assert training.training_eligible
	assert training.timing.entry_session.session_date == date(2024, 7, 5)
	assert training.timing.exit_session.session_date == date(2024, 7, 8)
	assert training.cache_key != inference.cache_key
	assert not math.isclose(training.targets.raw[0], 210 / 100 - 1)


def test_future_entry_and_exit_prices_cannot_change_inference_features(samples):
	before = samples.build()
	samples.revise(5, 17, 1_000_000.)
	samples.revise(8, 17, 1.)
	after = samples.build()
	assert before == after
	assert all(session.session_date.day in (1, 2, 3) for query in samples.repository.calls for session in query.scope.sessions)


def test_sparse_reversed_ticker_ids_align_by_key_not_row_position(samples):
	specification = replace(samples.specification, ticker_ids=(3, 17))
	sample = samples.build(specification=specification, label_cutoff=samples.decision(9).decision_cutoff)
	assert sample.features.values == pytest.approx((0, 0, -.2, .1, .2, .21))
	assert sample.targets.raw == pytest.approx((-.1, .05))
	assert samples.repository.calls[-1].ticker_ids == (3, 17)


def test_first_history_boundary_is_unavailable_without_earlier_queries(samples):
	for day in (1, 2, 3):
		sample = samples.build(day)
		assert sample.feature_state is WindowState.INSUFFICIENT_HISTORY
		assert sample.features.values is None and sample.features.sessions == ()
	assert samples.repository.calls == []
	assert samples.build(5).features.values is not None


def test_last_label_remains_unavailable_before_next_close_despite_future_storage(samples):
	cutoff = samples.decision(9).decision_cutoff
	sample = samples.build(8, label_cutoff=cutoff)
	assert sample.features.values is not None
	assert sample.target_state is TargetState.NOT_YET_AVAILABLE
	assert sample.targets.raw == (None, None)
	assert not sample.training_eligible
	assert all(session.sequence < samples.decision(8).entry_session.sequence for session in samples.repository.calls[-1].scope.sessions)


def test_exit_publication_not_only_exit_close_controls_maturity(samples):
	exit_session = samples.decision().exit_session
	before = samples.build(label_cutoff=exit_session.closes_at + timedelta(minutes=14))
	after = samples.build(label_cutoff=exit_session.closes_at + timedelta(minutes=15))
	assert before.targets.raw == (None, None)
	assert after.targets.raw == pytest.approx((.05, -.1))


def test_late_actual_outcome_receipt_is_not_a_zero_return(samples):
	key = samples.decision().exit_session, 3
	observation = samples.repository.observations[key]
	samples.repository.observations[key] = replace(observation, timing=replace(observation.timing, available_at=samples.decision(10).decision_cutoff))
	sample = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	assert sample.targets.raw == pytest.approx((.05, None))
	assert sample.target_state is TargetState.NOT_YET_AVAILABLE
	assert not sample.training_eligible


def test_training_cannot_use_a_historical_feature_received_after_its_decision(samples):
	key = samples.calendar.session(date(2024, 7, 3)), 17
	observation = samples.repository.observations[key]
	samples.repository.observations[key] = replace(observation, timing=replace(observation.timing, available_at=samples.decision(8).decision_cutoff))
	sample = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	assert sample.features.values is None
	assert sample.targets.ready
	assert sample.features.issues[0].state is PriceState.NOT_YET_AVAILABLE
	assert not sample.training_eligible


@pytest.mark.parametrize("day,ticker_id", [(2, 3), (3, 17)])
def test_missing_history_invalidates_whole_fixed_universe(samples, day, ticker_id):
	samples.repository.missing.add((samples.calendar.session(date(2024, 7, day)), ticker_id))
	sample = samples.build()
	assert sample.features.values is None
	assert sample.features.issues[0].ticker_id == ticker_id
	assert sample.features.issues[0].state is PriceState.MISSING


def test_missing_mature_label_preserves_partial_unavailability(samples):
	samples.repository.missing.add((samples.decision().exit_session, 3))
	sample = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	assert sample.targets.raw[0] == pytest.approx(.05)
	assert sample.targets.raw[1] is None
	assert sample.target_state is TargetState.UNAVAILABLE
	assert not sample.training_eligible


def test_unknown_publication_authorizes_no_reads(samples):
	sample = samples.build(publication_override=DailyPublicationPolicy(None, "Unknown fixture policy"), label_cutoff=samples.decision(9).decision_cutoff)
	assert sample.features.values is None and sample.feature_state is WindowState.UNVERIFIED
	assert sample.target_state is TargetState.UNVERIFIED
	assert samples.repository.calls == []


def test_feature_order_is_explicit_and_stateless(samples):
	specification = replace(samples.specification, feature_order=(CloseFeature.LOG_CLOSE, CloseFeature.RELATIVE_CLOSE))
	sample = samples.build(specification=specification)
	assert specification.shape == (3, 2, 2)
	assert sample.features.values[:4] == pytest.approx((math.log(100), 0, math.log(50), 0))
	assert sample.features.values[-4:] == pytest.approx((math.log(121), .21, math.log(60), .2))


@pytest.mark.parametrize("value", [0., -1., float("nan"), float("inf"), float("-inf"), True, "10"])
def test_external_invalid_close_prices_are_rejected(samples, value):
	observation = next(iter(samples.repository.observations.values()))
	with pytest.raises(ValueError):
		replace(observation, close=value)
	with pytest.raises(ValueError):
		close_to_close_return(value, 10., TargetSpec())


@pytest.mark.parametrize("changes", [{"feature_order": ()}, {"feature_order": ("legacy_intraday",)}, {"feature_order": [CloseFeature.LOG_CLOSE]}, {"feature_order": (CloseFeature.LOG_CLOSE, CloseFeature.LOG_CLOSE)}, {"adjustment": "adjusted"}])
def test_feature_transform_contract_rejects_invalid_layouts(samples, changes):
	with pytest.raises(ValueError):
		replace(samples.specification, **changes)


def test_adjustment_mismatch_fails_before_read(samples):
	with pytest.raises(ValueError, match="adjustment"):
		samples.build(specification=replace(samples.specification, adjustment=PriceAdjustment.UNADJUSTED))
	assert not samples.repository.calls


def test_entry_open_requires_an_explicit_opening_feature():
	with pytest.raises(ValueError, match="entry_open requires"):
		FeatureSpec((17, 3), 3, signal_mode=SignalMode.ENTRY_OPEN)


def test_invalid_window_and_numerical_layouts_are_rejected(samples):
	sample = samples.build()
	data = samples.repository.read(samples.repository.calls[0])
	with pytest.raises(ValueError, match="exact prior"):
		construct_features(samples.specification, samples.decision(), sample.features.sessions[1:], data)
	with pytest.raises(ValueError, match="layout"):
		replace(sample.features, values=(0.,))
	with pytest.raises(ValueError, match="layout"):
		replace(sample.features, values=(float("nan"),) * 6)
	with pytest.raises(ValueError, match="account for"):
		CloseData(data.query, data.observations[:-1], data.issues)
	with pytest.raises(ValueError, match="once"):
		CloseData(data.query, data.observations + data.observations[:1], data.issues)


def test_wrong_repository_query_or_future_observations_are_rejected(samples):
	class WrongRepository:
		def read(self, query):
			return samples.repository.read(replace(query, ticker_ids=(3, 17)))
	with pytest.raises(ValueError, match="changed the requested"):
		build_sample(samples.specification, samples.target, samples.decision(), samples.boundary, samples.calendar, samples.publication, WrongRepository())


def test_training_cutoff_cannot_precede_sample_decision(samples):
	with pytest.raises(ValueError, match="precedes"):
		samples.build(label_cutoff=samples.decision().decision_cutoff - timedelta(seconds=1))
	assert not samples.repository.calls


def test_declared_layout_cannot_be_satisfied_by_entry_session_features(samples):
	sample = samples.build()
	future_window = tuple(samples.calendar.shift(session, 1) for session in sample.features.sessions)
	with pytest.raises(ValueError, match="exact prior-session"):
		replace(sample, features=replace(sample.features, sessions=future_window))
