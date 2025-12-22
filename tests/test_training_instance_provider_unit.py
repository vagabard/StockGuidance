from __future__ import annotations

import numpy as np
import pytest

import training_instance_provider as tip


@pytest.fixture()
def synthetic_data():
	dates = np.array([10, 11, 12], dtype=np.int32)
	x_flat = np.array(
		[
			[1.0, 2.0, 3.0, 4.0],
			[5.0, 6.0, 7.0, 8.0],
			[9.0, 10.0, 11.0, 12.0],
		],
		dtype=np.float64,
	)
	labels = np.array(
		[
			[0.1, 0.2],
			[0.3, 0.4],
			[0.5, 0.6],
		],
		dtype=np.float64,
	)
	return dates, x_flat, labels


@pytest.fixture()
def provider(monkeypatch, synthetic_data):
	dates, x_flat, labels = synthetic_data

	def fake_fetch_grouped_by_date(*, dbinfo_file=None, limit=None, return_2d=False, current_day_change=True, volume_ratio=True, vwap_ratio=True, tranaction_ratio=True, day_change_centered="one"):
		# Return copies to ensure tests cannot mutate shared state.
		return dates.copy(), x_flat.copy(), labels.copy()

	monkeypatch.setattr(tip, "fetch_grouped_by_date", fake_fetch_grouped_by_date)
	return tip.TrainingInstanceProvider(use_torch=False)


def test_provider_returns_concatenated_features_and_labels(provider, synthetic_data):
	_, x_flat, labels = synthetic_data
	provider.set_how_many_days_in_past_to_use(1)

	max_inst = provider.get_maximum_number_of_test_train_instances()
	assert max_inst == 2

	features, lbls = provider.get_feature_and_label_pair(1)

	expected_features = np.concatenate([x_flat[1], x_flat[0]], axis=0)
	np.testing.assert_array_equal(features, expected_features)
	np.testing.assert_array_equal(lbls, labels[1])


def test_provider_validates_prior_days(provider):
	with pytest.raises(TypeError):
		provider.set_how_many_days_in_past_to_use(1.5)
	with pytest.raises(ValueError):
		provider.set_how_many_days_in_past_to_use(-1)
	# number_of_dates_or_dates_times_number_of_features_times_number_tickers is 3
	with pytest.raises(ValueError):
		provider.set_how_many_days_in_past_to_use(3)


def test_provider_validates_instance_indices(provider):
	provider.set_how_many_days_in_past_to_use(0)
	with pytest.raises(TypeError):
		provider.get_feature_and_label_pair("1")
	with pytest.raises(IndexError):
		provider.get_feature_and_label_pair(0)
	with pytest.raises(IndexError):
		provider.get_feature_and_label_pair(provider.get_maximum_number_of_test_train_instances() + 1)
