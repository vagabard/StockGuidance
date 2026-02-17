from __future__ import annotations

import numpy as np
import pytest
import sys
from unittest.mock import MagicMock

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

	def fake_fetch_grouped_by_date(*args, **kwargs):
		# Return copies to ensure tests cannot mutate shared state.
		return dates.copy(), x_flat.copy(), labels.copy(), [1, 2, 3]

	monkeypatch.setattr(tip, "fetch_grouped_by_date", fake_fetch_grouped_by_date)
	return tip.TrainingInstanceProvider(use_torch=False)


def test_provider_returns_concatenated_features_and_labels(provider, synthetic_data):
	dates, x_flat, labels = synthetic_data
	provider.set_how_many_days_in_past_to_use(1)

	max_inst = provider.get_maximum_number_of_test_train_instances()
	assert max_inst == 2

	# Instance 1: date index 1 (date 11), uses dates [1, 0]
	features, lbls, date_id_extra = provider.get_feature_and_label_pair(1)

	expected_features = np.concatenate([x_flat[1], x_flat[0]], axis=0).astype(np.float32)
	np.testing.assert_array_equal(features, expected_features)
	np.testing.assert_array_equal(lbls, labels[1].astype(np.float32))
	assert date_id_extra == dates[1]

	# Instance 2: date index 2 (date 12), uses dates [2, 1]
	features, lbls, date_id_extra = provider.get_feature_and_label_pair(2)
	assert date_id_extra == dates[2]


def test_provider_zero_prior_days(provider, synthetic_data):
	dates, x_flat, labels = synthetic_data
	provider.set_how_many_days_in_past_to_use(0)

	max_inst = provider.get_maximum_number_of_test_train_instances()
	assert max_inst == 3

	for i in range(1, 4):
		features, lbls, date_id_extra = provider.get_feature_and_label_pair(i)
		np.testing.assert_array_equal(features, x_flat[i - 1].astype(np.float32))
		np.testing.assert_array_equal(lbls, labels[i - 1].astype(np.float32))
		assert date_id_extra == dates[i - 1]


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


def test_provider_initialization_parameters(monkeypatch):
	captured_kwargs = {}

	def fake_fetch_grouped_by_date(**kwargs):
		nonlocal captured_kwargs
		captured_kwargs = kwargs
		return np.array([1]), np.array([[1]]), np.array([[1]]), [1]

	monkeypatch.setattr(tip, "fetch_grouped_by_date", fake_fetch_grouped_by_date)

	_ = tip.TrainingInstanceProvider(
		dbinfo_file="test.id",
		limit=100,
		start_date_id=20230101,
		end_date_id=20231231,
		current_day_change=False,
		volume_ratio=False,
		vwap_ratio=False,
		tranaction_ratio=False,
		day_change_centered="zero"
	)

	assert captured_kwargs["dbinfo_file"] == "test.id"
	assert captured_kwargs["limit"] == 100
	assert captured_kwargs["start_date_id"] == 20230101
	assert captured_kwargs["end_date_id"] == 20231231
	assert captured_kwargs["current_day_change"] is False
	assert captured_kwargs["volume_ratio"] is False
	assert captured_kwargs["vwap_ratio"] is False
	assert captured_kwargs["tranaction_ratio"] is False
	assert captured_kwargs["day_change_centered"] == "zero"


def test_provider_with_torch(monkeypatch, synthetic_data):
	dates, x_flat, labels = synthetic_data

	class MockTensor(np.ndarray):
		def numel(self):
			return self.size
		def element_size(self):
			return self.itemsize
		def cpu(self):
			return self
		def numpy(self):
			return self.view(np.ndarray)
		@property
		def shape(self):
			return super().shape
		def view(self, *args):
			if len(args) == 1 and isinstance(args[0], type) and issubclass(args[0], np.ndarray):
				return super().view(args[0])
			if len(args) == 1 and isinstance(args[0], (int, tuple)):
				return self.reshape(args[0]).view(MockTensor)
			return self.reshape(args).view(MockTensor)
		def to(self, *args, **kwargs):
			return self

	class MockTorch:
		def from_numpy(self, arr):
			return arr.view(MockTensor)
		def as_tensor(self, arr, dtype=None, device=None):
			return np.array(arr, dtype=np.float32).view(MockTensor) if dtype is not None else np.array(arr).view(MockTensor)
		def device(self, name):
			return name
		def arange(self, start, end, step):
			return np.arange(start, end, step).view(MockTensor)
		def index_select(self, tensor, dim, indices):
			return tensor[indices].view(MockTensor)
		def nan_to_num(self, tensor, nan=0.0, posinf=0.0, neginf=0.0):
			return np.nan_to_num(tensor, nan=nan, posinf=posinf, neginf=neginf).view(MockTensor)
		float32 = np.float32
		Tensor = MockTensor

	mock_torch = MockTorch()
	# Replace torch in sys.modules to catch all imports
	monkeypatch.setitem(sys.modules, "torch", mock_torch)
	# Also need to monkeypatch tip specifically as it might have already imported it
	monkeypatch.setattr(tip, "torch", mock_torch)
	monkeypatch.setattr(tip, "TORCH_AVAILABLE", True)

	# Since we monkeypatched sys.modules, we might also need to patch trainer.util.utils
	import trainer.util.utils
	monkeypatch.setattr(trainer.util.utils, "torch", mock_torch)
	monkeypatch.setattr(trainer.util.utils, "TORCH_AVAILABLE", True)

	def fake_fetch_grouped_by_date(*args, **kwargs):
		return dates.copy(), x_flat.copy(), labels.copy(), [1, 2, 3]

	monkeypatch.setattr(tip, "fetch_grouped_by_date", fake_fetch_grouped_by_date)

	provider = tip.TrainingInstanceProvider(use_torch=True)
	provider.set_how_many_days_in_past_to_use(1)

	features, lbls, date_id_extra = provider.get_feature_and_label_pair(1)

	# Verify types (though our mock returns numpy, in reality it would be torch)
	# The important part is that it goes through the torch path
	assert isinstance(features, np.ndarray)
	assert features.dtype == np.float32
	assert lbls.dtype == np.float32
	assert date_id_extra == dates[1]


def test_provider_with_skipped_dates_and_multiple_tickers(monkeypatch):
	# User requirement: dates with skips: 11, 12, 15, 18, 20, 21, 22
	dates = np.array([11, 12, 15, 18, 20, 21, 22], dtype=np.int32)
	num_dates = len(dates)

	# User requirement: each date has at least 3 tickers
	# For simplicity, let's assume max ticker ID is 9.
	# ticker_id slots are 0 to 8 (ticker_id 1 to 9)
	# slots = (ticker_id - 1) * num_features
	# Let's use 2 features per ticker.
	num_tickers = 9
	num_features = 2
	f_per_date = num_tickers * num_features

	x_flat = np.zeros((num_dates, f_per_date), dtype=np.float64)
	labels = np.zeros((num_dates, num_tickers), dtype=np.float64)

	# User requirement: Each ticker per date should have a single number for it close to 1.0 (1.1, 0.9, etc.)
	# We'll fill features and labels with values around 1.0 for the specified tickers.

	# Date 11 (index 0): tickers 3, 6, 7
	# Ticker 3: index (3-1)*2 = 4, 5
	# Ticker 6: index (6-1)*2 = 10, 11
	# Ticker 7: index (7-1)*2 = 12, 13
	for d_idx, tickers in enumerate([
		[3, 6, 7], # 11
		[5, 6, 9], # 12
		[1, 2, 3], # 15
		[4, 5, 8], # 18
		[2, 7, 9], # 20
		[1, 4, 6], # 21
		[3, 5, 8], # 22
	]):
		for t_id in tickers:
			t_idx = t_id - 1
			# Fill features with values around 1.0
			x_flat[d_idx, t_idx * num_features] = 1.0 + (t_id * 0.01)
			x_flat[d_idx, t_idx * num_features + 1] = 1.0 - (t_id * 0.01)
			# Fill label with a value around 1.0
			labels[d_idx, t_idx] = 0.9 + (t_id * 0.02)

	def fake_fetch_grouped_by_date(*args, **kwargs):
		return dates.copy(), x_flat.copy(), labels.copy(), [1, 2, 3]

	monkeypatch.setattr(tip, "fetch_grouped_by_date", fake_fetch_grouped_by_date)

	provider = tip.TrainingInstanceProvider(use_torch=False)
	provider.set_how_many_days_in_past_to_use(1)

	# With 7 dates and 1 prior day, we should have 6 instances.
	max_inst = provider.get_maximum_number_of_test_train_instances()
	assert max_inst == 6

	# Test first instance (date 12, using 12 and 11)
	features, lbls, date_id_extra = provider.get_feature_and_label_pair(1)
	assert date_id_extra == 12
	# features should be concatenation of x_flat[1] and x_flat[0]
	expected_features = np.concatenate([x_flat[1], x_flat[0]], axis=0).astype(np.float32)
	np.testing.assert_array_equal(features, expected_features)
	np.testing.assert_array_equal(lbls, labels[1].astype(np.float32))

	# Verify ticker values in features for instance 1 (date 12)
	# Tickers for date 12 were 5, 6, 9.
	# Ticker 5 features: 1.05, 0.95
	t5_idx = (5 - 1) * num_features
	assert features[t5_idx] == 1.05
	assert features[t5_idx + 1] == 0.95

	# Verify ticker values from prior day (date 11) in concatenated features
	# Prior day features start at index f_per_date
	# Tickers for date 11 were 3, 6, 7.
	# Ticker 3 features: 1.03, 0.97
	t3_prior_idx = f_per_date + (3 - 1) * num_features
	assert features[t3_prior_idx] == 1.03
	assert features[t3_prior_idx + 1] == 0.97

def test_provider_stores_ticker_ids(monkeypatch, synthetic_data):
	dates, x_flat, labels = synthetic_data
	ticker_ids = [1, 5, 9]

	def fake_fetch_grouped_by_date(*args, **kwargs):
		return dates.copy(), x_flat.copy(), labels.copy(), ticker_ids

	monkeypatch.setattr(tip, "fetch_grouped_by_date", fake_fetch_grouped_by_date)
	provider = tip.TrainingInstanceProvider(use_torch=False)
	assert provider.ticker_ids == ticker_ids
