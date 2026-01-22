from __future__ import annotations

import numpy as np
import pytest

from feature_windowing import (
	best_fit_feature_vector,
	concat_features_current_then_prior,
	concat_features_prior_only,
)


def test_concat_features_current_then_prior() -> None:
	X_flat = np.array(
		[
			[1.0, 2.0],
			[3.0, 4.0],
			[5.0, 6.0],
		],
		dtype=np.float64,
	)
	# d=2, num_prior=1 -> indices [2, 1]
	vec = concat_features_current_then_prior(X_flat, d=2, num_prior=1)
	expected = np.array([5.0, 6.0, 3.0, 4.0], dtype=np.float64)
	np.testing.assert_array_equal(vec, expected)


def test_concat_features_prior_only() -> None:
	X_flat = np.array(
		[
			[1.0, 2.0],
			[3.0, 4.0],
			[5.0, 6.0],
		],
		dtype=np.float64,
	)
	# d=2, num_prior=1 -> index [1]
	vec = concat_features_prior_only(X_flat, d=2, num_prior=1)
	expected = np.array([3.0, 4.0], dtype=np.float64)
	np.testing.assert_array_equal(vec, expected)


def test_best_fit_feature_vector_prefers_prior_only_when_size_matches() -> None:
	X_flat = np.array(
		[
			[1.0, 2.0, 3.0, 4.0],
			[5.0, 6.0, 7.0, 8.0],
			[9.0, 10.0, 11.0, 12.0],
		],
		dtype=np.float64,
	)

	vec = best_fit_feature_vector(
		X_flat,
		d=2,
		num_prior=1,
		expected_size=4,
		prefer_prior_only=True,
	)

	# Should return X_flat[1] (d-1)
	np.testing.assert_array_equal(vec, X_flat[1])


def test_best_fit_feature_vector_falls_back_to_include_current_when_sizes_differ() -> None:
	X_flat = np.array(
		[
			[1.0, 2.0, 3.0, 4.0],
			[5.0, 6.0, 7.0, 8.0],
			[9.0, 10.0, 11.0, 12.0],
		],
		dtype=np.float64,
	)

	vec = best_fit_feature_vector(
		X_flat,
		d=2,
		num_prior=1,
		expected_size=8,
		prefer_prior_only=True,
	)

	# Should return [X_flat[2], X_flat[1]]
	expected = np.concatenate([X_flat[2], X_flat[1]], axis=0)
	np.testing.assert_array_equal(vec, expected)


