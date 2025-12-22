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

DB access is standardized via a single `dbinfo_file` parameter that is passed
through to the database loader.

By default, NumPy arrays are returned. If PyTorch is installed and you pass
use_torch=True, results are returned as torch_interface.FloatTensors.
"""
from __future__ import annotations

from typing import Optional, Tuple, Any

import numpy as np
from numpy import dtype, ndarray
from torch import Tensor

from load_database_rows_into_tensors import (
    fetch_grouped_by_date,
    TORCH_AVAILABLE,
)
from feature_windowing import concat_features_current_then_prior
from debug_feature_logging import (
    dump_feature_window,
    env_feature_debug_enabled,
)

try:
    # reuse the torch_interface instance created in the loader if available
    from load_database_rows_into_tensors import torch  # type: ignore
except Exception:  # pragma: no cover - torch_interface is optional
    torch = None  # type: ignore


class TrainingInstanceProvider:
    def __init__(
        self,
        *,
        dbinfo_file: str = "dbinfo.id",
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
          - ticker_data_list_flat: shape (D, T*F)
          - labels_for_training: shape (D, T)
        """
        dates, ticker_data_list_flat, labels_for_training = fetch_grouped_by_date(
            dbinfo_file=dbinfo_file,
            limit=limit,
            return_2d=False,
	        current_day_change=current_day_change,
	        volume_ratio=volume_ratio,
	        vwap_ratio=vwap_ratio,
	        tranaction_ratio=tranaction_ratio,
	        day_change_centered=day_change_centered
        )

        self.cumulative_dates_for_train_validation_and_test_sets: np.ndarray = dates.astype(np.int32, copy=False)
        self.ticker_data_list_flat: np.ndarray = ticker_data_list_flat.astype(np.float64, copy=False)
        self.labels_for_training: np.ndarray = labels_for_training.astype(np.float64, copy=False)
        self.number_of_prior_days_to_include_in_feature: int = 0
        self._use_torch: bool = bool(use_torch and TORCH_AVAILABLE)
        self._current_day_change: bool = current_day_change
        self._volume_ratio: bool = volume_ratio
        self._vwap_ratio: bool = vwap_ratio
        self._tranaction_ratio: bool = tranaction_ratio
        self._day_change_centered: str = day_change_centered


        # Derived dims
        self.number_of_dates_or_dates_times_number_of_features_times_number_tickers: int = int(self.ticker_data_list_flat.shape[0])
        self.ticker_feature_quantity: int = int(self.ticker_data_list_flat.shape[1]) if self.ticker_data_list_flat.ndim == 2 else 0
        self.ticker_label_for_training_quantity: int = int(self.labels_for_training.shape[1]) if self.labels_for_training.ndim == 2 else 0

    # Public API (names kept as requested)
    def set_how_many_days_in_past_to_use(self, number_of_prior_dates: int) -> None:
        """Configure how many prior dates to include.

        number_of_prior_dates >= 0, where 0 means use only the selected date.
        """
        if not isinstance(number_of_prior_dates, int):
            raise TypeError("number_of_prior_dates must be an int")
        if number_of_prior_dates < 0:
            raise ValueError("number_of_prior_dates must be >= 0")
        if number_of_prior_dates >= self.number_of_dates_or_dates_times_number_of_features_times_number_tickers:
            # Would leave no valid instances
            raise ValueError(
                f"number_of_prior_dates ({number_of_prior_dates}) must be < total dates ({self.number_of_dates_or_dates_times_number_of_features_times_number_tickers})"
            )
        self.number_of_prior_days_to_include_in_feature = number_of_prior_dates

    def get_maximum_number_of_test_train_instances(self) -> int:
        """Total number of 1-based instances available with current window size."""
        return max(0, self.number_of_dates_or_dates_times_number_of_features_times_number_tickers - self.number_of_prior_days_to_include_in_feature)

    def get_feature_and_label_pair(self, selected_training_instance: int) -> tuple[Tensor, Tensor] | tuple[
	    ndarray[tuple[Any, ...], dtype[Any]], Any]:
        """Return (features, labels) for a 1-based instance index.

        The mapping is defined so that instance index 1 corresponds to the earliest
        date that has enough prior context, i.e., date index = num_prior.
        Instance k corresponds to absolute date index: num_prior + (k-1).
        """
        max_inst = self.get_maximum_number_of_test_train_instances()
        if max_inst <= 0:
            raise RuntimeError("No instances available. Call Set_How_many_days_in_past_to_use with a valid value.")
        if not isinstance(selected_training_instance, int):
            raise TypeError(f"selected_training_instance must be an int (1-based), got {type(selected_training_instance)}")
        if selected_training_instance < 1 or selected_training_instance > max_inst:
            raise IndexError(
                f"selected_training_instance out of range: 1..{max_inst}, got {selected_training_instance}. "
                f"num_prior={self.number_of_prior_days_to_include_in_feature}, num_dates={self.number_of_dates_or_dates_times_number_of_features_times_number_tickers}"
            )

        d = self.number_of_prior_days_to_include_in_feature + (selected_training_instance - 1)

        # Concatenate features for [d, d-1, ..., d-num_prior] using shared utility
        feats = concat_features_current_then_prior(self.ticker_data_list_flat, d, self.number_of_prior_days_to_include_in_feature)

        # Optional debug dump of one random ticker's feature window
        if env_feature_debug_enabled() and np.random.rand() < 0.001:
            feature_names: list[str] = ["close_ratio_to_prior"]
            if self._current_day_change:
                feature_names.append("change_in_price_over_day")
            if self._volume_ratio:
                feature_names.append("volume_normalized")
            if self._vwap_ratio:
                feature_names.append("vwap_ratio_to_prior")
            if self._tranaction_ratio:
                feature_names.append("transactions_normalized")

            dump_feature_window(
                X_flat=self.ticker_data_list_flat,
                dates=self.cumulative_dates_for_train_validation_and_test_sets,
                labels=self.labels_for_training,
                d=d,
                num_prior=self.number_of_prior_days_to_include_in_feature,
                current_day_change_used=self._current_day_change,
                header_note="training",
                feature_names=feature_names,
                vec_size=int(feats.size),
            )

        labels = self.labels_for_training[d]

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
