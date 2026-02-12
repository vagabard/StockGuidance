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

from typing import Optional, Tuple, Any, List
from pathlib import Path

import numpy as np
from numpy import dtype, ndarray
from torch import Tensor

from load_database_rows_into_tensors import (
    fetch_grouped_by_date,
    TORCH_AVAILABLE,
)
from feature_windowing import concat_features_current_then_prior
from trainer.util.utils import tensorize_and_sanitize
from debug_feature_logging import (
    dump_feature_window,
    env_feature_debug_enabled,
)

try:
    # reuse the torch_interface instance created in the loader if available
    from load_database_rows_into_tensors import torch  # type: ignore
except Exception:  # pragma: no cover - torch_interface is optional
    torch = None  # type: ignore

try:
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover - optional dependency for caching
    pd = None  # type: ignore


class TrainingInstanceProvider:
    def __init__(
        self,
        *,
        dbinfo_file: str = "dbinfo.id",
        limit: Optional[int] = None,
        use_torch: bool = False,
        current_day_change: bool = True,
        volume_ratio: bool = False,
        vwap_ratio: bool = False,
        tranaction_ratio: bool = False,
        day_change_centered: str = "zero",
        start_date_id: Optional[int] = None,
        end_date_id: Optional[int] = None,
    ) -> None:
        """Load per-date matrices from the database and cache them in memory.

        The underlying fetch returns:
          - dates: shape (D,)
          - ticker_data_list_flat: shape (D, T*F)
          - labels_for_training: shape (D, T)
          - ticker_ids: list of ticker_id values
        """
        dates, ticker_data_list_flat, labels_for_training, ticker_ids = fetch_grouped_by_date(
            dbinfo_file=dbinfo_file,
            limit=limit,
            return_2d=False,
            current_day_change=current_day_change,
            volume_ratio=volume_ratio,
            vwap_ratio=vwap_ratio,
            tranaction_ratio=tranaction_ratio,
            day_change_centered=day_change_centered,
            start_date_id=start_date_id,
            end_date_id=end_date_id,
        )

        self.cumulative_dates_for_train_validation_and_test_sets: Tensor = torch.from_numpy(
            dates.astype(np.int32, copy=False)
        )
        self.ticker_data_list_flat, self.labels_for_training = tensorize_and_sanitize(
            ticker_data_list_flat,
            labels_for_training,
            device=torch.device("cpu"),
        )

        # Log data size
        nbytes = self.cumulative_dates_for_train_validation_and_test_sets.numel() * self.cumulative_dates_for_train_validation_and_test_sets.element_size() + \
                 self.ticker_data_list_flat.numel() * self.ticker_data_list_flat.element_size() + \
                 self.labels_for_training.numel() * self.labels_for_training.element_size()
        print(f"[DATA LOAD] Dates shape: {self.cumulative_dates_for_train_validation_and_test_sets.shape}")
        print(f"[DATA LOAD] Ticker data shape: {self.ticker_data_list_flat.shape}")
        print(f"[DATA LOAD] Labels shape: {self.labels_for_training.shape}")
        print(f"[DATA LOAD] Total memory for raw arrays: {nbytes / (1024**3):.2f} GB")

        self.ticker_ids: List[int] = ticker_ids
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
                f"number_of_prior_dates ({number_of_prior_dates}) must be < total dates ({self.number_of_dates_or_dates_times_number_of_features_times_number_tickers}). "
                f"Try reducing --prior-days or ensure the date range contains enough data."
            )
        self.number_of_prior_days_to_include_in_feature = number_of_prior_dates

    def get_maximum_number_of_test_train_instances(self) -> int:
        """Total number of 1-based instances available with current window size."""
        return max(0, self.number_of_dates_or_dates_times_number_of_features_times_number_tickers - self.number_of_prior_days_to_include_in_feature)

    def get_feature_and_label_pair(self, selected_training_instance: int) -> tuple[Tensor, Tensor, int]:
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

        # Concatenate features for [d, d-1, ..., d-num_prior]
        num_prior = self.number_of_prior_days_to_include_in_feature
        if num_prior <= 0:
            feats = self.ticker_data_list_flat[d]
        else:
            # We want [d, d-1, ..., d-num_prior]
            # Slicing is faster and uses views if possible, but here we want non-contiguous
            # or reversed rows, so we use torch.cat.
            # However, to support the "megatensor" view idea, if the model can accept
            # [d-num_prior, ..., d] we could just slice.
            # But the existing logic is d, d-1, ...
            indices = torch.arange(d, d - num_prior - 1, -1)
            feats = torch.index_select(self.ticker_data_list_flat, 0, indices).view(-1)

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

            if env_feature_debug_enabled():

                feats_np = feats.cpu().numpy() if hasattr(feats, "numpy") else feats
                feats_src_np = self.ticker_data_list_flat.cpu().numpy()
                labels_src_np = self.labels_for_training.cpu().numpy()
                dates_src_np = self.cumulative_dates_for_train_validation_and_test_sets.cpu().numpy()

                dump_feature_window(
                    X_flat=feats_src_np,
	                dates=dates_src_np,
	                labels=labels_src_np,
	                d=d,
	                num_prior=self.number_of_prior_days_to_include_in_feature,
	                current_day_change_used=self._current_day_change,
	                header_note="training",
	                feature_names=feature_names,
	                vec_size=int(feats_np.size),
	            )

        labels = self.labels_for_training[d]

        if self._use_torch:
            return feats, labels, int(self.cumulative_dates_for_train_validation_and_test_sets[d])
        else:
            return feats.numpy(), labels.numpy(), int(self.cumulative_dates_for_train_validation_and_test_sets[d])


class TrainingDatasetTrivial:
    def __init__(
        self,
        *,
        number_of_dates: int = 128,
        number_of_tickers: int = 16,
        number_of_features: int = 4,
        use_torch: bool = False,
        store_as_torch: bool = False,
        cache_dataset: bool = False,
        cache_path: Optional[str] = None,
        cache_format: str = "parquet",
        seed: Optional[int] = None,
        current_day_change: bool = True,
        volume_ratio: bool = False,
        vwap_ratio: bool = False,
        tranaction_ratio: bool = False,
        day_change_centered: str = "zero",
    ) -> None:
        if not isinstance(number_of_dates, int) or number_of_dates <= 0:
            raise ValueError("number_of_dates must be a positive int")
        if not isinstance(number_of_tickers, int) or number_of_tickers <= 0:
            raise ValueError("number_of_tickers must be a positive int")
        if not isinstance(number_of_features, int) or number_of_features <= 0:
            raise ValueError("number_of_features must be a positive int")

        cache_format = cache_format.lower().strip()
        if cache_format not in {"msgspec"}:
            raise ValueError("cache_format must be 'msgspec'")

        rng = np.random.default_rng(seed)
        dates = np.arange(1, number_of_dates + 1, dtype=np.int32)
        ticker_data_list_flat = rng.standard_normal(
            size=(number_of_dates, number_of_tickers * number_of_features)
        ).astype(np.float64, copy=False)
        labels_for_training = rng.standard_normal(
            size=(number_of_dates, number_of_tickers)
        ).astype(np.float64, copy=False)
        ticker_ids = list(range(number_of_tickers))

        self._cache_enabled: bool = bool(cache_dataset)
        self._cache_format: str = cache_format
        cache_suffix = "msgpack"
        self._cache_path: Path = Path(cache_path) if cache_path else Path("cache") / f"training_dataset_trivial.{cache_suffix}"
        self._dates_start: int = int(dates[0])

        if self._cache_enabled:
            import msgspec
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            data_to_cache = {
                "date_id": dates.tolist(),
                "features": [f.tolist() for f in ticker_data_list_flat],
                "labels": [l.tolist() for l in labels_for_training],
            }
            with open(self._cache_path, "wb") as f:
                f.write(msgspec.msgpack.encode(data_to_cache))
            self.cumulative_dates_for_train_validation_and_test_sets = None
            self.ticker_data_list_flat = None
            self.labels_for_training = None
        else:
            self.cumulative_dates_for_train_validation_and_test_sets: np.ndarray = dates.astype(np.int32, copy=False)
            self.ticker_data_list_flat: np.ndarray = ticker_data_list_flat.astype(np.float64, copy=False)
            self.labels_for_training: np.ndarray = labels_for_training.astype(np.float64, copy=False)

            if store_as_torch and TORCH_AVAILABLE:
                self.cumulative_dates_for_train_validation_and_test_sets = torch.from_numpy(self.cumulative_dates_for_train_validation_and_test_sets)
                self.ticker_data_list_flat = torch.from_numpy(self.ticker_data_list_flat)
                self.labels_for_training = torch.from_numpy(self.labels_for_training)

        self.ticker_ids: List[int] = ticker_ids
        self.number_of_prior_days_to_include_in_feature: int = 0
        self._use_torch: bool = bool(use_torch and TORCH_AVAILABLE)
        self._current_day_change: bool = current_day_change
        self._volume_ratio: bool = volume_ratio
        self._vwap_ratio: bool = vwap_ratio
        self._tranaction_ratio: bool = tranaction_ratio
        self._day_change_centered: str = day_change_centered

        # Derived dims
        if self._cache_enabled:
            self.number_of_dates_or_dates_times_number_of_features_times_number_tickers = int(number_of_dates)
            self.ticker_feature_quantity = int(number_of_tickers * number_of_features)
            self.ticker_label_for_training_quantity = int(number_of_tickers)
        else:
            self.number_of_dates_or_dates_times_number_of_features_times_number_tickers = int(self.ticker_data_list_flat.shape[0])
            self.ticker_feature_quantity = int(self.ticker_data_list_flat.shape[1]) if self.ticker_data_list_flat.ndim == 2 else 0
            self.ticker_label_for_training_quantity = int(self.labels_for_training.shape[1]) if self.labels_for_training.ndim == 2 else 0

    def set_how_many_days_in_past_to_use(self, number_of_prior_dates: int) -> None:
        if not isinstance(number_of_prior_dates, int):
            raise TypeError("number_of_prior_dates must be an int")
        if number_of_prior_dates < 0:
            raise ValueError("number_of_prior_dates must be >= 0")
        if number_of_prior_dates >= self.number_of_dates_or_dates_times_number_of_features_times_number_tickers:
            raise ValueError(
                f"number_of_prior_dates ({number_of_prior_dates}) must be < total dates ({self.number_of_dates_or_dates_times_number_of_features_times_number_tickers}). "
                f"Try reducing --prior-days or ensure the date range contains enough data."
            )
        self.number_of_prior_days_to_include_in_feature = number_of_prior_dates

    def get_maximum_number_of_test_train_instances(self) -> int:
        return max(0, self.number_of_dates_or_dates_times_number_of_features_times_number_tickers - self.number_of_prior_days_to_include_in_feature)

    def get_feature_and_label_pair(self, selected_training_instance: int) -> tuple[Tensor, Tensor, int] | tuple[
	    ndarray, Any, int ]:
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

        if self._cache_enabled:
            import msgspec
            with open(self._cache_path, "rb") as f:
                data = msgspec.msgpack.decode(f.read())
            start_idx = d - self.number_of_prior_days_to_include_in_feature
            end_idx = d + 1
            window_features = np.asarray(data["features"][start_idx:end_idx], dtype=np.float64)
            window_labels = np.asarray(data["labels"][start_idx:end_idx], dtype=np.float64)
            if self.number_of_prior_days_to_include_in_feature <= 0:
                feats = window_features[-1]
            else:
                slices = [window_features[-1 - off] for off in range(0, self.number_of_prior_days_to_include_in_feature + 1)]
                feats = np.concatenate(slices, axis=0)
            labels = window_labels[-1]
            date_for_instance = data["date_id"][d]
        else:
            feats_src = self.ticker_data_list_flat
            if hasattr(feats_src, "numpy"):
                feats_src = feats_src.numpy()
            feats = concat_features_current_then_prior(feats_src, d, self.number_of_prior_days_to_include_in_feature)

            labels = self.labels_for_training[d]
            if hasattr(labels, "numpy"):
                labels = labels.numpy()
            date_for_instance = int(self.cumulative_dates_for_train_validation_and_test_sets[d])

        if self._use_torch:
            feats_t = torch.as_tensor(feats, dtype=torch.float32)
            labels_t = torch.as_tensor(labels, dtype=torch.float32)
            return feats_t, labels_t, int(date_for_instance)
        else:
            return feats, labels, int(date_for_instance)


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
    "TrainingDatasetTrivial",
    "create_provider",
]
