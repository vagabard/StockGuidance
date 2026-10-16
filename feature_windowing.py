"""
feature_windowing
------------------

Utilities for constructing concatenated per-date feature windows that are
shared between legacy training (training_instance_provider) and inference/simulation
(simulate_trading).

All functions operate on X_flat with shape (D, T*F), where each row is the
flattened per-ticker feature vector for a given date index.

These legacy windows are incompatible with previous-close samples. New consumers
use stockguidance.datasets.building and its exact calendar/specification window.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def concat_features_current_then_prior(X_flat: np.ndarray, d: int, num_prior: int) -> np.ndarray:
    """Concatenate features for indices [d, d-1, ..., d-num_prior].

    If num_prior == 0, returns X_flat[d].
    """
    if num_prior <= 0:
        return X_flat[d]
    slices = [X_flat[d - off] for off in range(0, num_prior + 1)]
    return np.concatenate(slices, axis=0)


def concat_features_prior_only(X_flat: np.ndarray, d: int, num_prior: int) -> np.ndarray:
    """Concatenate features for indices [d-1, d-2, ..., d-num_prior].

    If num_prior <= 0, returns an empty vector.
    """
    if num_prior <= 0:
        return np.empty((0,), dtype=X_flat.dtype)
    slices = [X_flat[d - off] for off in range(1, num_prior + 1)]
    return np.concatenate(slices, axis=0)


def best_fit_feature_vector(
    X_flat: np.ndarray,
    d: int,
    num_prior: int,
    expected_size: int,
    *,
    prefer_prior_only: bool = True,
) -> np.ndarray:
    """Return a feature vector whose size matches expected_size if possible.

    Tries strictly prior-days window [d-1..d-num_prior] if prefer_prior_only.
    If that doesn't match expected_size, falls back to including current day
    [d..d-num_prior]. If neither matches, raises ValueError with sizes.
    """
    # Handle zero prior as a simple fast-path
    if num_prior <= 0:
        vec = X_flat[d]
        if int(vec.size) == int(expected_size):
            return vec
        # else fall through to include_current to raise a descriptive error

    prior_vec: Optional[np.ndarray] = None
    if prefer_prior_only and num_prior > 0:
        prior_vec = concat_features_prior_only(X_flat, d, num_prior)
        if int(prior_vec.size) == int(expected_size):
            return prior_vec

    include_vec = concat_features_current_then_prior(X_flat, d, num_prior)
    if int(include_vec.size) != int(expected_size):
        prior_size = prior_vec.size if prior_vec is not None else 'n/a'
        raise ValueError(
            f"Could not construct feature vector of expected size {expected_size}. "
            f"Got {include_vec.size} (include-current) and {prior_size} (prior-only)."
        )
    return include_vec


__all__ = [
    "concat_features_current_then_prior",
    "concat_features_prior_only",
    "best_fit_feature_vector",
]
