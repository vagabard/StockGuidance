from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from stockguidance.datasets.numpy_arrays import training_arrays, validate_arrays
from stockguidance.datasets.training import TrainingDataset, build_training_dataset
from stockguidance.features.specification import CloseFeature


def dataset(samples, days=(3, 5, 8), cutoff=None):
	return build_training_dataset(tuple(samples.decision(day) for day in days), cutoff or samples.decision(9).decision_cutoff, samples.specification, samples.target, samples.boundary, samples.calendar, samples.publication, samples.repository)


def test_training_excludes_boundary_and_immature_samples_without_shifting_dates(samples):
	training = dataset(samples)
	assert tuple(sample.timing.entry_session.session_date.day for sample in training.eligible) == (5,)
	assert tuple(sample.timing.entry_session.session_date.day for sample in training.excluded) == (3, 8)
	features, targets = training_arrays(training)
	assert features.shape == (1, 6) and targets.shape == (1, 2)
	assert features[0] == pytest.approx((0, 0, .1, -.2, .21, .2))
	assert targets[0] == pytest.approx((.05, -.1))
	assert features.dtype == targets.dtype == np.float64
	assert features.flags.c_contiguous and targets.flags.c_contiguous


def test_missing_labels_are_excluded_as_a_full_sample_not_sanitized(samples):
	samples.repository.missing.add((samples.decision(5).exit_session, 3))
	training = dataset(samples)
	assert not training.eligible
	assert training.samples[1].targets.raw[1] is None
	with pytest.raises(ValueError, match="No training samples"):
		training_arrays(training)


@pytest.mark.parametrize("days", [(8, 5), (5, 5)])
def test_training_decisions_must_be_unique_and_chronological(samples, days):
	with pytest.raises(ValueError, match="chronological"):
		dataset(samples, days)
	assert not samples.repository.calls


def test_dataset_semantics_are_checked_even_for_equal_input_dimensions(samples):
	first = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	second = samples.build(8, label_cutoff=samples.decision(9).decision_cutoff, specification=replace(samples.specification, ticker_ids=(3, 17)))
	with pytest.raises(ValueError, match="incompatible"):
		TrainingDataset((first, second), samples.decision(9).decision_cutoff)


@pytest.mark.parametrize("problem", ["shape", "float32", "nan", "infinity", "noncontiguous", "list"])
def test_numpy_boundary_rejects_invalid_arrays(samples, problem):
	features, targets = training_arrays(dataset(samples))
	if problem == "shape":
		features = features.reshape(1, 2, 3)
	elif problem == "float32":
		targets = targets.astype(np.float32)
	elif problem == "nan":
		targets[0, 0] = np.nan
	elif problem == "infinity":
		features[0, 0] = np.inf
	elif problem == "noncontiguous":
		features = np.zeros((1, 12))[:, ::2]
	else:
		features = features.tolist()
	with pytest.raises(ValueError):
		validate_arrays(features, targets, samples.specification)


def test_cache_identity_is_stable_for_reordered_database_rows(samples):
	first = samples.build()
	samples.repository.observations = dict(reversed(tuple(samples.repository.observations.items())))
	assert samples.build().cache_key == first.cache_key


def test_corrected_feature_close_invalidates_feature_and_sample_identities(samples):
	first = samples.build()
	samples.revise(2, 17, 120.)
	second = samples.build()
	assert first.feature_revision != second.feature_revision
	assert first.feature_cache_key != second.feature_cache_key
	assert first.cache_key != second.cache_key
	assert first.features.values != second.features.values


def test_correction_to_label_does_not_change_feature_identity(samples):
	first = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	samples.revise(8, 17, 400.)
	second = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	assert first.feature_cache_key == second.feature_cache_key
	assert first.cache_key != second.cache_key
	assert second.targets.raw[0] == 1.


def test_backfill_invalidates_an_unavailable_feature_identity(samples):
	key = samples.calendar.session(date(2024, 7, 2)), 3
	samples.repository.missing.add(key)
	unavailable = samples.build()
	samples.repository.missing.clear()
	ready = samples.build()
	assert unavailable.features.values is None
	assert unavailable.cache_key != ready.cache_key


@pytest.mark.parametrize("change", ["ticker_order", "feature_transform", "lookback", "cutoff", "label_cutoff", "source", "adjustment", "calendar", "revision"])
def test_cache_identity_includes_explicit_semantics_and_revision(samples, change):
	first = samples.build(label_cutoff=samples.decision(9).decision_cutoff)
	if change == "ticker_order":
		second = samples.build(specification=replace(samples.specification, ticker_ids=(3, 17)), label_cutoff=first.label_cutoff)
	elif change == "feature_transform":
		second = samples.build(specification=replace(samples.specification, feature_order=(CloseFeature.LOG_CLOSE,)), label_cutoff=first.label_cutoff)
	elif change == "lookback":
		second = samples.build(specification=replace(samples.specification, lookback_sessions=2), label_cutoff=first.label_cutoff)
	elif change == "cutoff":
		second = replace(first, timing=replace(first.timing, decision_cutoff=first.timing.decision_cutoff - timedelta(minutes=1)))
	elif change == "label_cutoff":
		second = replace(first, label_cutoff=first.label_cutoff + timedelta(minutes=1))
	elif change == "source":
		second = replace(first, source=replace(first.source, universe="other-declared-universe"))
	elif change == "adjustment":
		from stockguidance.config.source import PriceAdjustment
		second = replace(first, source=replace(first.source, adjustment=PriceAdjustment.UNADJUSTED), features=replace(first.features, specification=replace(first.features.specification, adjustment=PriceAdjustment.UNADJUSTED)))
	elif change == "calendar":
		second = replace(first, calendar_version="fixture-other-calendar-version")
	else:
		second = replace(first, data_revision="corrected-snapshot-revision")
	assert first.cache_key != second.cache_key
