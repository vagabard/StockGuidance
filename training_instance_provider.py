"""
Training/Test instance provider

This module provides a small helper class that prepares date-indexed feature/label
windows for model training and testing. It relies on
`load_database_rows_into_tensors.fetch_grouped_by_date` to fetch and arrange raw
daily rows into per-date matrices.

Public API (as requested):
  - Set_How_many_days_in_past_to_use(int number_of_prior_dates)
      Controls how many prior trading days are concatenated with the selected
      date's features. If N is provided, the feature vector will contain the
      selected day d plus N prior days: [X[d], X[d-1], ..., X[d-N]].

  - get_maximum_number_of_test_train_instances()
      Returns the number of valid instances that can be produced, which equals
      the total number of dates fetched minus the configured number_of_prior_dates.

  - getfeatureAndLabelPair(selected_training_instance)
      Takes a 1-based instance index (1..max_instances) and returns:
        (features_tensor, labels_tensor)
      where features are the concatenation of the flattened per-date features for
      the selected date and its N prior dates, and labels are the ticker-wise
      labels for the selected date only.

Notes on shapes:
  - Let T be the maximum ticker_id observed (treated as dense slots 1..T).
  - Let F be the features per ticker (7 columns from the DB query).
  - Per date, X_flat has shape (T*F,). Y has shape (T,).
  - Returned features shape: ((N+1) * T * F,)
  - Returned labels shape: (T,)

By default, NumPy arrays are returned. If PyTorch is installed and you pass
use_torch=True, results are returned as torch.FloatTensors.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

from load_database_rows_into_tensors import (
    fetch_grouped_by_date,
    TORCH_AVAILABLE,
)
from feature_windowing import concat_features_current_then_prior

try:
    # reuse the torch instance created in the loader if available
    from load_database_rows_into_tensors import torch  # type: ignore
except Exception:  # pragma: no cover - torch is optional
    torch = None  # type: ignore


class TrainingInstanceProvider:
    def __init__(
        self,
        *,
        host: str = "localhost",
        database: str = "stockforecasting",
        user: Optional[str] = "Redbeard",
        password: Optional[str] = "3YZs#z5GOJKdea",
        port: int = 3306,
        driver: str = "auto",
        limit: Optional[int] = None,
        use_torch: bool = False,
        current_day_change: bool = True,
	    volume_ratio: bool = True,
	    vwap_ratio: bool = True,
	    tranaction_ratio: bool = True,
	    day_change_centered: str = "one",
    ) -> None:
        """Load per-date matrices from the database and cache them in memory.

        The underlying fetch returns:
          - dates: shape (D,)
          - X_flat: shape (D, T*F)
          - Y: shape (D, T)
        """
        dates, X_flat, Y = fetch_grouped_by_date(
            host=host,
            database=database,
            user=user,
            password=password,
            port=port,
            driver=driver,
            limit=limit,
            return_2d=False,
	        current_day_change=current_day_change,
	        volume_ratio=volume_ratio,
	        vwap_ratio=vwap_ratio,
	        tranaction_ratio=tranaction_ratio,
	        day_change_centered=day_change_centered
        )

        self._dates: np.ndarray = dates.astype(np.int32, copy=False)
        self._X_flat: np.ndarray = X_flat.astype(np.float64, copy=False)
        self._Y: np.ndarray = Y.astype(np.float64, copy=False)
        self._num_prior: int = 0
        self._use_torch: bool = bool(use_torch and TORCH_AVAILABLE)
        self._current_day_change: bool = current_day_change
        self._volume_ratio: bool = volume_ratio
        self._vwap_ratio: bool = vwap_ratio
        self._tranaction_ratio: bool = tranaction_ratio
        self._day_change_centered: str = day_change_centered


        # Derived dims
        self._D: int = int(self._X_flat.shape[0])
        self._TF: int = int(self._X_flat.shape[1]) if self._X_flat.ndim == 2 else 0
        self._T: int = int(self._Y.shape[1]) if self._Y.ndim == 2 else 0

    # Public API (names kept as requested)
    def set_how_many_days_in_past_to_use(self, number_of_prior_dates: int) -> None:
        """Configure how many prior dates to include.

        number_of_prior_dates >= 0, where 0 means use only the selected date.
        """
        if not isinstance(number_of_prior_dates, int):
            raise TypeError("number_of_prior_dates must be an int")
        if number_of_prior_dates < 0:
            raise ValueError("number_of_prior_dates must be >= 0")
        if number_of_prior_dates >= self._D:
            # Would leave no valid instances
            raise ValueError(
                f"number_of_prior_dates ({number_of_prior_dates}) must be < total dates ({self._D})"
            )
        self._num_prior = number_of_prior_dates

    def get_maximum_number_of_test_train_instances(self) -> int:
        """Total number of 1-based instances available with current window size."""
        return max(0, self._D - self._num_prior)

    def get_feature_and_label_pair(self, selected_training_instance: int):
        """Return (features, labels) for a 1-based instance index.

        The mapping is defined so that instance index 1 corresponds to the earliest
        date that has enough prior context, i.e., date index = num_prior.
        Instance k corresponds to absolute date index: num_prior + (k-1).
        """
        max_inst = self.get_maximum_number_of_test_train_instances()
        if max_inst <= 0:
            raise RuntimeError("No instances available. Call Set_How_many_days_in_past_to_use with a valid value.")
        if not isinstance(selected_training_instance, int):
            raise TypeError("selected_training_instance must be an int (1-based)")
        if selected_training_instance < 1 or selected_training_instance > max_inst:
            raise IndexError(
                f"selected_training_instance out of range: 1..{max_inst}, got {selected_training_instance}"
            )

        d = self._num_prior + (selected_training_instance - 1)

        # Concatenate features for [d, d-1, ..., d-num_prior] using shared utility
        feats = concat_features_current_then_prior(self._X_flat, d, self._num_prior)

        labels = self._Y[d]

        if self._use_torch:
            # Convert to float32 tensors (typical for NN inputs)
            feats_t = torch.from_numpy(feats.astype(np.float32, copy=False))
            labels_t = torch.from_numpy(labels.astype(np.float32, copy=False))
            return feats_t, labels_t
        else:
            return feats, labels


# Convenience factory function (optional)
def create_provider(**kwargs) -> TrainingInstanceProvider:
    """Shorthand to create a TrainingInstanceProvider.

    Example:
        provider = create_provider(use_torch=True, close_ratio_only=True)
        provider.Set_How_many_days_in_past_to_use(3)
        n = provider.get_maximum_number_of_test_train_instances()
        x, y = provider.getfeatureAndLabelPair(1)
    """
    return TrainingInstanceProvider(**kwargs)


__all__ = [
    "TrainingInstanceProvider",
    "create_provider",
]
