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

import random
from typing import Optional, Tuple, Any, List
from pathlib import Path

import line_profiler
import numpy as np
from numpy import dtype, ndarray
from torch import Tensor
from torch.utils.data import DataLoader

from load_database_rows_into_tensors import (
	fetch_grouped_by_date,
	TORCH_AVAILABLE,
)
from feature_windowing import concat_features_current_then_prior
from trainer.util.utils import tensorize_and_sanitize
from trainer.datasets import IndexDataset
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
		ticker_limit: Optional[int] = None,
		use_torch: bool = False,
		current_day_change: bool = True,
		volume_ratio: bool = False,
		vwap_ratio: bool = False,
		tranaction_ratio: bool = False,
		day_change_centered: str = "zero",
		start_date_id: Optional[int] = None,
		end_date_id: Optional[int] = None,
		device: Optional[str] = None,
		cache_dir: Optional[str] = None,
	) -> None:
		print(f"TrainingInstanceProvider initialized with dbinfo_file={dbinfo_file}, limit={limit}, ticker_limit={ticker_limit}, use_torch={use_torch}, current_day_change={current_day_change}, volume_ratio={volume_ratio}, vwap_ratio={vwap_ratio}, tranaction_ratio={tranaction_ratio}, day_change_centered={day_change_centered}, start_date_id={start_date_id}, end_date_id={end_date_id}, device={device}, cache_dir={cache_dir}")
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
			ticker_limit=ticker_limit,
			return_2d=False,
			current_day_change=current_day_change,
			volume_ratio=volume_ratio,
			vwap_ratio=vwap_ratio,
			tranaction_ratio=tranaction_ratio,
			day_change_centered=day_change_centered,
			start_date_id=start_date_id,
			end_date_id=end_date_id,
			cache_dir=cache_dir,
		)

		if device is None:
			device = "cuda" if torch is not None and torch.cuda.is_available() else "cpu"
		self._device = torch.device(device) if torch is not None else None

		self.cumulative_dates_for_train_validation_and_test_sets: Tensor = torch.from_numpy(
			dates.astype(np.int32, copy=False)
		).to(device=self._device) if torch is not None else dates

		self.ticker_data_list_flat, self.labels_for_training = tensorize_and_sanitize(
			ticker_data_list_flat,
			labels_for_training,
			device=self._device,
		)

		# Log data size
		self.ticker_ids: List[int] = ticker_ids
		self.number_of_prior_days_to_include_in_feature: int = 0
		self._use_torch: bool = True # bool(use_torch and TORCH_AVAILABLE)
		self._current_day_change: bool = current_day_change
		self._volume_ratio: bool = volume_ratio
		self._vwap_ratio: bool = vwap_ratio
		self._tranaction_ratio: bool = tranaction_ratio
		self._day_change_centered: str = day_change_centered


		# Derived dims
		self.number_of_dates_or_dates_times_number_of_features_times_number_tickers: int = int(self.ticker_data_list_flat.shape[0])
		self.ticker_feature_quantity: int = int(self.ticker_data_list_flat.shape[1]) if self.ticker_data_list_flat.ndim == 2 else 0
		self.ticker_label_for_training_quantity: int = int(self.labels_for_training.shape[1]) if self.labels_for_training.ndim == 2 else 0
		self._reusable_feat_tensor: Optional[Tensor] = None

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


	def get_feature_and_label_pair(self, selected_training_instance: int, out_feat: Optional[Tensor] = None) -> tuple[Tensor, Tensor, int]:
		"""Return (features, labels) for a 1-based instance index.

		The mapping is defined so that instance index 1 corresponds to the earliest
		date that has enough prior context, i.e., date index = num_prior.
		Instance k corresponds to absolute date index: num_prior + (k-1).

		If out_feat is provided, it must have the correct shape and device.
		If NOT provided, internal reusable tensors may be used and returned.
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

			# Determine which tensor to use for output
			if out_feat is not None:
				target_out = out_feat
			else:
				if self._reusable_feat_tensor is None or self._reusable_feat_tensor.shape[0] != (num_prior + 1) * self.ticker_feature_quantity:
					# Allocation once
					self._reusable_feat_tensor = torch.empty((num_prior + 1) * self.ticker_feature_quantity, device=self._device, dtype=self.ticker_data_list_flat.dtype)
				target_out = self._reusable_feat_tensor

			indices = torch.arange(d, d - num_prior - 1, -1, device=self._device)
			# index_select doesn't support 1D output directly with 'out', it follows the input's dimensionality.
			# ticker_data_list_flat is (D, T*F). index_select with indices (N,) will produce (N, T*F).
			# We then need to view it as (-1).
			# However, torch.index_select(input, dim, index, *, out=None) -> Tensor
			# If target_out is already flattened, we can't pass it to index_select if it expects (num_prior+1, T*F).

			tmp_view = target_out.view(num_prior + 1, self.ticker_feature_quantity)
			torch.index_select(self.ticker_data_list_flat, 0, indices, out=tmp_view)
			feats = target_out

		# Optional debug dump of one random ticker's feature window
		# if env_feature_debug_enabled() and np.random.rand() < 0.001:
		# 	feature_names: list[str] = ["change_in_price_over_day"]
		# 	if self._current_day_change:
		# 		feature_names.append("change_in_price_over_day")  #["close_ratio_to_prior"]
		# 	if self._volume_ratio:
		# 		feature_names.append("volume_normalized")
		# 	if self._vwap_ratio:
		# 		feature_names.append("vwap_ratio_to_prior")
		# 	if self._tranaction_ratio:
		# 		feature_names.append("transactions_normalized")
		#
		# 	if env_feature_debug_enabled():
		#
		# 		feats_np = feats.cpu().numpy() if hasattr(feats, "numpy") else feats
		# 		feats_src_np = self.ticker_data_list_flat.cpu().numpy()
		# 		labels_src_np = self.labels_for_training.cpu().numpy()
		# 		dates_src_np = self.cumulative_dates_for_train_validation_and_test_sets.cpu().numpy()
		#
		# 		dump_feature_window(
		# 			X_flat=feats_src_np,
		# 			dates=dates_src_np,
		# 			labels=labels_src_np,
		# 			d=d,
		# 			num_prior=self.number_of_prior_days_to_include_in_feature,
		# 			current_day_change_used=self._current_day_change,
		# 			header_note="training",
		# 			feature_names=feature_names,
		# 			vec_size=int(feats_np.size),
		# 		)

		labels = self.labels_for_training[d]
		date_to_return = self.cumulative_dates_for_train_validation_and_test_sets[d].item()
		if self._use_torch:
			return feats, labels, date_to_return
		else:
			return feats.detach().cpu().numpy(), labels.detach().cpu().numpy(), int(self.cumulative_dates_for_train_validation_and_test_sets[d])


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
		device: Optional[str] = None,
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

		if device is None:
			device = "cuda" if torch is not None and torch.cuda.is_available() else "cpu"
		self._device = torch.device(device) if torch is not None else None

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
				self.cumulative_dates_for_train_validation_and_test_sets = torch.from_numpy(self.cumulative_dates_for_train_validation_and_test_sets).to(device=self._device)
				self.ticker_data_list_flat = torch.from_numpy(self.ticker_data_list_flat).to(device=self._device)
				self.labels_for_training = torch.from_numpy(self.labels_for_training).to(device=self._device)

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

	def get_feature_and_label_pair(self, selected_training_instance: int, out_feat: Optional[Tensor] = None) -> tuple[Tensor, Tensor, int] | tuple[
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
			if self._use_torch and torch is not None and isinstance(feats_src, torch.Tensor):
				num_prior = self.number_of_prior_days_to_include_in_feature
				if num_prior <= 0:
					feats = feats_src[d]
				else:
					if out_feat is not None:
						target_out = out_feat
					else:
						if not hasattr(self, "_reusable_feat_tensor") or self._reusable_feat_tensor is None or self._reusable_feat_tensor.shape[0] != (num_prior + 1) * self.ticker_feature_quantity:
							self._reusable_feat_tensor = torch.empty((num_prior + 1) * self.ticker_feature_quantity, device=self._device, dtype=feats_src.dtype)
						target_out = self._reusable_feat_tensor

					indices = torch.arange(d, d - num_prior - 1, -1, device=self._device)
					tmp_view = target_out.view(num_prior + 1, self.ticker_feature_quantity)
					torch.index_select(feats_src, 0, indices, out=tmp_view)
					feats = target_out

				labels = self.labels_for_training[d]
				date_for_instance = int(self.cumulative_dates_for_train_validation_and_test_sets[d])

				return feats, labels, date_for_instance
			else:
				# NumPy path
				if hasattr(feats_src, "cpu"):
					feats_src = feats_src.cpu().numpy()
				elif hasattr(feats_src, "numpy"):
					feats_src = feats_src.numpy()

				feats = concat_features_current_then_prior(feats_src, d, self.number_of_prior_days_to_include_in_feature)

				labels = self.labels_for_training[d]
				if hasattr(labels, "cpu"):
					labels = labels.cpu().numpy()
				elif hasattr(labels, "numpy"):
					labels = labels.numpy()
				date_for_instance = int(self.cumulative_dates_for_train_validation_and_test_sets[d])

		if self._use_torch:
			feats_t = torch.as_tensor(feats, dtype=torch.float32, device=self._device)
			labels_t = torch.as_tensor(labels, dtype=torch.float32, device=self._device)
			return feats_t, labels_t, int(date_for_instance)
		else:
			return feats, labels, int(date_for_instance)


# Convenience factory function (optional)
def create_provider(device: Optional[str] = None, **kwargs) -> TrainingInstanceProvider:
	"""Shorthand to create a TrainingInstanceProvider.

	Example:
		provider = create_provider(use_torch=True, close_ratio_only=True)
		provider.Set_How_many_days_in_past_to_use(3)
		n = provider.get_maximum_number_of_test_train_instances()
		x, y = provider.getfeatureAndLabelPair(1)
	"""
	# Filter out kwargs that are not accepted by TrainingInstanceProvider.__init__
	import inspect
	sig = inspect.signature(TrainingInstanceProvider.__init__)
	valid_params = sig.parameters.keys()
	provider_kwargs = {k: v for k, v in kwargs.items() if k in valid_params}
	return TrainingInstanceProvider(device=device, **provider_kwargs)


__all__ = [
	"TrainingInstanceProvider",
	"TrainingDatasetTrivial",
	"create_provider",
	"main",
]


def main(argv: List[str] | None = None) -> int:
	import argparse
	import time

	parser = argparse.ArgumentParser(description="Profile TrainingInstanceProvider")
	parser.add_argument("--dbinfo", type=str, default="dbinfo.id")
	parser.add_argument("--limit", type=int, default=None)
	parser.add_argument("--prior-days", type=int, default=5)
	parser.add_argument("--use-torch", action="store_true")
	parser.add_argument("--device", type=str, default="cpu")
	parser.add_argument("--day-centered", type=str, default="proportional_normalized_zero_centered")
	parser.add_argument("--volume-ratio", action="store_true")
	parser.add_argument("--vwap-ratio", action="store_true")
	parser.add_argument("--transaction-ratio", action="store_true")
	#parser.add_argument("--trivial", action="store_true", help="Use TrainingDatasetTrivial instead of TrainingInstanceProvider")
	parser.add_argument("--iterations", type=int, default=5, help="Number of times to loop over all instances")

	args = parser.parse_args(argv)

	start_time = time.time()
	# if args.trivial:
	# 	print("Initializing TrainingDatasetTrivial...")
	# 	provider: Any = TrainingDatasetTrivial(
	# 		use_torch=args.use_torch,
	# 		day_change_centered=args.day_centered,
	# 		volume_ratio=args.volume_ratio,
	# 		vwap_ratio=args.vwap_ratio,
	# 		tranaction_ratio=args.transaction_ratio,
	# 		device=args.device,
	# 		cache_format="msgspec",
	# 	)
	# else:
	print(f"Initializing TrainingInstanceProvider with dbinfo={args.dbinfo}, limit={args.limit}...")
	provider = TrainingInstanceProvider(
		dbinfo_file=args.dbinfo,
		limit=None,
		use_torch=True,
		current_day_change=False,
		day_change_centered="zero",
		volume_ratio=False,
		vwap_ratio=False,
		tranaction_ratio=False,
		device="cpu",
		start_date_id=15242,
		end_date_id = 16856
	)

	init_time = time.time() - start_time
	print(f"Initialization took {init_time:.4f}s")

	provider.set_how_many_days_in_past_to_use(args.prior_days)
	max_inst = provider.get_maximum_number_of_test_train_instances()
	print(f"Total instances: {max_inst}")

	if max_inst <= 0:
		print("No instances available.")
		return 1

	# Create IndexDataset to iterate over
	indices = list(range(1, max_inst + 1))
	dataset = IndexDataset(provider=provider, indices=indices)

	# print(f"Looping {args.iterations} time(s) over {max_inst} instances using IndexDataset...")
	# loop_start = time.time()
	# count = 0
	# print(f"running with batch size 256, num_workers=1...")
	# test_profiler_loader = DataLoader(dataset, batch_size=256, num_workers=1)
	# for i in range(args.iterations):
	#     for feat, label in test_profiler_loader:
	#         count += 1
	#         hold_count = feat.shape+ label.shape
	#         if count % 1000 == 0:
	#             print(f"Processed {count} instances  shape {hold_count}...")
	#
	# loop_end = time.time()
	# loop_time = loop_end - loop_start
	# print(f"Looping took {loop_time:.4f}s ({loop_time/count:.6f}s per instance)")
	#
	# print(f"Looping {args.iterations} time(s) over {max_inst} instances using IndexDataset...")
	# loop_start = time.time()
	# count = 0
	# print(f"running with batch size 64, num_workers=1...")
	# test_profiler_loader = DataLoader(dataset, batch_size=64, num_workers=1)
	# for i in range(args.iterations):
	#     for feat, label in test_profiler_loader:
	#         count += 1
	#         hold_count = feat.shape+ label.shape
	#         if count % 1000 == 0:
	#             print(f"Processed {count} instances  shape {hold_count}...")
	#
	# loop_end = time.time()
	# loop_time = loop_end - loop_start
	# print(f"Looping took {loop_time:.4f}s ({loop_time/count:.6f}s per instance)")
	#
	# print(f"Looping {args.iterations} time(s) over {max_inst} instances using IndexDataset...")
	# loop_start = time.time()
	# count = 0
	# print(f"running with batch size 256, num_workers=0...")
	# test_profiler_loader = DataLoader(dataset, batch_size=256, num_workers=0)
	# for i in range(args.iterations):
	#     for feat, label in test_profiler_loader:
	#         count += 1
	#         hold_count = feat.shape+ label.shape
	#         if count % 1000 == 0:
	#             print(f"Processed {count} instances  shape {hold_count}...")
	#
	# loop_end = time.time()
	# loop_time = loop_end - loop_start
	# print(f"Looping took {loop_time:.4f}s ({loop_time/count:.6f}s per instance)")

	print(f"Looping {args.iterations} time(s) over {max_inst} instances using IndexDataset...")
	loop_start = time.time()
	count = 0
	print(f"running with batch size 256, num_workers=0...")
	test_profiler_loader = DataLoader(dataset,batch_size=32,
		shuffle=True,
		num_workers=0,
		#drop_last=drop_last,
		persistent_workers=False,
	)
	hold_feat = None
	hold_label = None
	for i in range(args.iterations):
		for feat, label in test_profiler_loader:
			count += 1
			if random.random() < 0.01:
				if hold_feat is None:
					hold_feat = feat
					hold_label = label
				else:
					hold_feat += feat
					hold_label += label
			if count % 10 == 0:
				print(f"Processed {count} instances  shape {feat.shape}  {label.shape}...")

	loop_end = time.time()
	loop_time = loop_end - loop_start
	print(f"Looping took {loop_time:.4f}s ({loop_time/count:.6f}s per instance)")


	print(f"Total time: {time.time() - start_time:.4f}s")

	return 0


if __name__ == "__main__":
	import sys
	sys.exit(main(sys.argv[1:]))
