from __future__ import annotations

import numpy as np
import pytest

from simulate_trading import _average_scores, _build_open_close_matrices, _concat_features


def test_build_open_close_matrices_maps_dates_and_tickers() -> None:
	keys = np.array([[100, 1], [101, 3], [100, 2]], dtype=np.int64)
	feats = np.array([[10.0, 11.0], [20.0, 21.0], [30.0, 31.0]], dtype=np.float64)
	dates = np.array([100, 101], dtype=np.int64)

	open_mat, close_mat = _build_open_close_matrices(keys, feats, dates, T=3)

	assert open_mat.shape == (2, 3)
	assert close_mat.shape == (2, 3)
	assert open_mat[0, 0] == pytest.approx(10.0)
	assert close_mat[0, 0] == pytest.approx(11.0)
	assert open_mat[1, 2] == pytest.approx(20.0)
	assert close_mat[1, 2] == pytest.approx(21.0)
	assert np.isnan(open_mat[0, 2])
	assert np.isnan(close_mat[0, 2])


def test_concat_features_prefers_prior_only_when_size_matches() -> None:
	X_flat = np.array(
		[
			[1.0, 2.0, 3.0, 4.0],
			[5.0, 6.0, 7.0, 8.0],
			[9.0, 10.0, 11.0, 12.0],
		],
		dtype=np.float64,
	)
	dates = np.array([1, 2, 3], dtype=np.int32)

	vec, feature_date_id = _concat_features(
		X_flat,
		date_array_index=2,
		num_prior=1,
		prefer_prior_only=True,
		expected_size=4,
		dates=dates,
	)

	np.testing.assert_array_equal(vec, X_flat[1])
	assert feature_date_id == 2


def test_concat_features_falls_back_to_include_current_when_sizes_differ() -> None:
	X_flat = np.array(
		[
			[1.0, 2.0, 3.0, 4.0],
			[5.0, 6.0, 7.0, 8.0],
			[9.0, 10.0, 11.0, 12.0],
		],
		dtype=np.float64,
	)
	dates = np.array([1, 2, 3], dtype=np.int32)

	vec, feature_date_id = _concat_features(
		X_flat,
		date_array_index=2,
		num_prior=1,
		prefer_prior_only=True,
		expected_size=8,
		dates=dates,
	)

	expected = np.concatenate([X_flat[2], X_flat[1]], axis=0)
	np.testing.assert_array_equal(vec, expected)
	assert feature_date_id == 3


def test_average_scores_validations_and_mean() -> None:
	with pytest.raises(ValueError):
		_average_scores([])
	with pytest.raises(ValueError):
		_average_scores([np.ones((2, 2))])

	result = _average_scores([np.array([1.0, 2.0]), np.array([3.0, 4.0])])
	np.testing.assert_array_equal(result, np.array([2.0, 3.0]))
