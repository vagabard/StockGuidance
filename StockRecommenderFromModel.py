"""
StockRecommenderFromModel
-------------------------

Utility to load a previously trained model and produce buy recommendations.

Usage example:
	from StockRecommenderFromModel import StockRecommenderFromModel
	rec = StockRecommenderFromModel("path/to/run_dir_or_manifest.json")
	days_needed = rec.get_required_previous_days()
	# Build feature vector for the selected date using that many prior days
	scores, top_ids = rec.getBuyRecommendations(feature_vector, top_k=10)

Notes
 - This loader expects artifacts written by train_pytorch_network.py, namely a
   directory containing a model_manifest.json and model_state.pt.
 - Ticker ids in the output default to 1..T (dense slots). If a sidecar file
   named ticker_id_map.json (list of length T with actual ticker_id integers)
   exists next to the manifest, those values are used instead.
"""
from __future__ import annotations

import os
import json
import traceback
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, List, Tuple, Union, Optional, Dict

import numpy as np
import torch  # type: ignore
from torch import nn

from trainer import torch_interface

TORCH_AVAILABLE = True

try:
	import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
	psutil = None  # type: ignore

from models import load_model_from_manifest  # type: ignore
from models.utils import resolve_manifest_path  # type: ignore
from trainer.model_loader import load_model_from_checkpoint
from trainer.state.family_run import FamilyRun
from database.db_connect import get_connection  # type: ignore


ArrayLike = Union[np.ndarray, "torch_interface.Tensor", Iterable[float]]

print_recommendation_type = False

# Minimum number of trades (transactions) required for a ticker to be
# considered in the recommendations. Tickers with fewer trades are skipped.
# Adjust this constant to tune the threshold.
MIN_TRADES_FOR_RECOMMENDATION = 200


class StockRecommender:
	"""Base class for stock recommenders."""
	def __init__(self, single_run: "SingleRun"):
		self._single_run = single_run
		self.input_size = single_run.input_size
		self.output_size = single_run.output_size
		self.ticker_ids = single_run.ticker_ids
		self._ticker_id_map = self.ticker_ids

	def get_output_directory(self) -> Optional[str]:
		return None

	def getBuyRecommendations(
		self,
		feature_vector: Optional[ArrayLike] = None,
		top_k: int = 10,
		index_base: int = 1,
		*,
		current_date_id: Optional[int] = None,
		dbinfo_file: Optional[str] = None,
		transactions_per_ticker: Optional[Union[np.ndarray, Iterable[float]]] = None,
		clear_gpu_cache: bool = False,
		save_scores: bool = False,
		epoch_name: str = "",
		precalculated_scores: Optional[torch.Tensor] = None,
		precalculated_transactions: Optional[Dict[int, float]] = None,
		provider: Optional["TrainingInstanceProvider"] = None,
	) -> Tuple[np.ndarray, List[int]]:
		raise NotImplementedError

	def get_required_previous_days(self) -> int:
		return int(self._single_run.prior_days)

	def get_day_change_centered(self) -> str:
		return str(self._single_run.day_change_centered or "zero")

	def get_current_day_change_used(self) -> bool:
		return bool(self._single_run.family_run.current_day_change)

	def get_volume_ratio_used(self) -> bool:
		return bool(self._single_run.family_run.volume_ratio)

	def get_vwap_ratio_used(self) -> bool:
		return bool(self._single_run.family_run.vwap_ratio)

	def get_tranaction_ratio_used(self) -> bool:
		return bool(self._single_run.family_run.transaction_ratio)


class StockRecommenderFromSavedScores(StockRecommender):
	"""Recommender that only uses precalculated scores and transactions."""
	def __init__(self, single_run: "SingleRun", output_directory: Optional[str] = None):
		super().__init__(single_run)
		self.output_directory = output_directory

	def get_output_directory(self) -> Optional[str]:
		return self.output_directory

	def getBuyRecommendations(
		self,
		feature_vector: Optional[ArrayLike] = None,
		top_k: int = 10,
		index_base: int = 1,
		*,
		current_date_id: Optional[int] = None,
		dbinfo_file: Optional[str] = None,
		transactions_per_ticker: Optional[Union[np.ndarray, Iterable[float]]] = None,
		clear_gpu_cache: bool = False,
		save_scores: bool = False,
		epoch_name: str = "",
		precalculated_scores: Optional[torch.Tensor] = None,
		precalculated_transactions: Optional[Dict[int, float]] = None,
		provider: Optional["TrainingInstanceProvider"] = None,
	) -> Tuple[np.ndarray, List[int]]:
		"""Return recommendations using precalculated scores and transactions."""
		if precalculated_scores is None:
			raise ValueError("precalculated_scores must be provided to StockRecommenderFromSavedScores.")

		if top_k <= 0:
			top_k = 1

		if self.output_size is None and precalculated_scores is not None:
			self.output_size = precalculated_scores.numel()

		if self.output_size is not None:
			top_k = int(min(top_k, self.output_size))

		out_from_model_flat_cpu = precalculated_scores.detach().cpu()
		T = int(self.output_size)

		# Build transactions vector
		transactions_vec_tensor = None
		if precalculated_transactions is not None:
			arr = np.zeros((T,), dtype=np.float32)
			if self.ticker_ids is not None and len(self.ticker_ids) > 0:
				for i, tid in enumerate(self.ticker_ids):
					val = precalculated_transactions.get(int(tid))
					if val is not None:
						arr[i] = float(val)
			else:
				for tid, val in precalculated_transactions.items():
					if 1 <= int(tid) <= T:
						arr[int(tid) - 1] = float(val)
			transactions_vec_tensor = torch.from_numpy(arr)
		elif transactions_per_ticker is not None:
			if isinstance(transactions_per_ticker, np.ndarray):
				arr = transactions_per_ticker.astype(np.float32, copy=False)
			else:
				arr = np.asarray(list(transactions_per_ticker), dtype=np.float32)
			transactions_vec_tensor = torch.from_numpy(arr)

		if transactions_vec_tensor is None:
			raise ValueError("Transactions must be provided to StockRecommenderFromSavedScores.")

		# Build mask
		mask_allowed = torch.isfinite(transactions_vec_tensor) & (transactions_vec_tensor >= float(MIN_TRADES_FOR_RECOMMENDATION))

		# Apply mask
		masked_scores = out_from_model_flat_cpu.clone()
		masked_scores[~mask_allowed] = -float("inf")

		# Get top-k
		topk_vals, topk_idx = torch.topk(masked_scores, k=top_k, largest=True, sorted=True)
		idx_list = topk_idx.tolist()

		# Map to ticker ids
		top_scoring_ticker_ids: List[int] = []
		if self.ticker_ids is not None and len(self.ticker_ids) > 0 and len(self.ticker_ids) == T:
			for i in idx_list:
				if masked_scores[i] > -float("inf"):
					top_scoring_ticker_ids.append(int(self.ticker_ids[i]))
		else:
			offset = 1 if int(index_base) == 1 else 0
			for i in idx_list:
				if masked_scores[i] > -float("inf"):
					top_scoring_ticker_ids.append(int(i + offset))

		return masked_scores.numpy(), top_scoring_ticker_ids


class StockRecommenderFromModel(StockRecommender):
	def __init__(
		self,
		single_run: "SingleRun",
		model_or_path: "List[torch_interface.nn.Module | str] | str",  # type: ignore[name-defined]
		output_directory: Optional[str] = None,
		model_dict: Optional[dict] = None,
		device: str = "auto",
		*,
		ensemble_normalize: bool = False,
	) -> None:
		"""Create a recommender from either a saved model path or existing models.

		Simplified control flow:
		- If a string is passed, models are loaded from the path and then the class
		  is constructed from those models.
		- If models are passed (list preferred), the class is constructed directly
		  from that list via a common method.

		Args:
			single_run: A SingleRun object containing the configuration and family run.
			model_or_path: Path/dir to load models from, or a list of models. A single
				model instance is accepted for backward compatibility and wrapped into a list.
			device: "auto" (cuda if available else cpu), "cpu", or "cuda".
			load_models: If False, models will not be loaded from disk if model_or_path is a string.
			             Only metadata (input/output size) will be initialized.
		"""
		super().__init__(single_run)
		if not TORCH_AVAILABLE:
			raise RuntimeError("PyTorch is required to use StockRecommenderFromModel.")
		global print_recommendation_type
		print_recommendation_type = False
		# Resolve device once; used in both construction modes
		if device == "auto":
			dev = "cuda" if torch.cuda.is_available() else "cpu"  # type: ignore[attr-defined]
		else:
			dev = device
		self._device = torch.device(dev)  # type: ignore[call-arg]
		self._ensemble_normalize: bool = bool(ensemble_normalize)
		self._models: List["torch_interface.nn.Module"] = []  # type: ignore[name-defined]
		self._is_ensemble: bool = False
		self._restricted_memory_mode: bool = False
		# Disk-caching for memory management
		self._cached_to_disk: bool = False
		self._ensemble_cache_paths: list[str] = []
		self.output_directory = output_directory

		# Unified path: either load from string source, or build from provided models
		if isinstance(model_or_path, str):
			models, _ = self._load_models_from_source(model_or_path)
			self.output_directory = model_path = model_or_path
			self._construct_from_models(models)
		elif isinstance(model_or_path, List) and len(model_or_path) > 0 and (isinstance(model_or_path[0], nn.Module) or isinstance(model_or_path[0], str)):
			self._construct_from_models(model_or_path)
		else:
			raise ValueError("Must provide either a model path or a list of models.")

	def _construct_from_models(
		self,
		models_or_paths: List[Union["torch_interface.nn.Module", str]],  # type: ignore[name-defined]
	) -> None:
		"""Finalize construction from a list of models or paths, ensuring unified manifest.
		Also decides whether to offload ensemble models to disk to reduce RAM usage.
		"""
		in_memory_models = [m for m in models_or_paths if not isinstance(m, str)]
		provided_paths = [m for m in models_or_paths if isinstance(m, str)]

		if in_memory_models:
			try:
				self._models = [m.to(self._device).eval() for m in in_memory_models]
			except torch.OutOfMemoryError:
				if str(self._device).startswith("cuda"):
					self._restricted_memory_mode = True
					self._models = [m.to("cpu").eval() for m in in_memory_models]
					if torch.cuda.is_available():
						torch.cuda.empty_cache()
				else:
					raise
		else:
			self._models = []

		self._ensemble_cache_paths = provided_paths
		self._cached_to_disk = len(self._ensemble_cache_paths) > 0
		self._is_ensemble = (len(self._models) + len(self._ensemble_cache_paths)) > 1

		# Use SingleRun instead of _manifest
		self.input_size = self._single_run.input_size
		self.output_size = self._single_run.output_size

		# Cache common dims
		if self._models:
			# Prefer attributes from the first model instance if not set in single_run
			if not self.input_size:
				self.input_size = getattr(self._models[0], "input_size", None)
			if not self.output_size:
				self.output_size = getattr(self._models[0], "output_size", None)
		elif self._ensemble_cache_paths:
			# Load first model to get dims if not set in single_run
			if not self.input_size or not self.output_size:
				first_path = self._ensemble_cache_paths[0]
				try:
					# Try to load just the manifest if it's a directory, or load model on CPU
					if os.path.isdir(first_path) or first_path.endswith(".json"):
						from models.utils import resolve_manifest_path
						m_path = resolve_manifest_path(first_path)
						with open(m_path, "r") as f:
							m_data = json.load(f)
						if not self.input_size:
							self.input_size = int(m_data.get("input_size", 0))
						if not self.output_size:
							self.output_size = int(m_data.get("output_size", 0))
					else:
						loaded = torch.load(first_path, map_location="cpu", weights_only=False)
						if isinstance(loaded, dict) and "model_state_dict" in loaded:
							# Dimensions must come from SingleRun/FamilyRun if not in checkpoint
							pass
						else:
							if not self.input_size:
								self.input_size = getattr(loaded, "input_size", None)
							if not self.output_size:
								self.output_size = getattr(loaded, "output_size", None)
						del loaded
				except Exception as e:
					print(f"Warning: could not extract dimensions from cached model {first_path}: {e}")

		if not self.output_size and self.ticker_ids is not None and len(self.ticker_ids) > 0:
			self.output_size = len(self.ticker_ids)

		# If still not found, we might need to rely on what was passed in _load_models_from_source
		# but _load_models_from_source currently returns a manifest.

		self._input_size = self.input_size
		self._output_size = self.output_size

		self.ticker_ids = self._single_run.ticker_ids
		self._ticker_id_map = self.ticker_ids  # Alias for backward compatibility

		# We need input_size and output_size to be set.
		# If they weren't found in models, we'll have to use them from the manifest we loaded.


	def _load_models_from_source(self, source: str) -> Tuple[List["torch_interface.nn.Module"], dict]:  # type: ignore[name-defined]
		"""Load models from a path or directory and return (models, manifest)."""
		path_str = os.path.abspath(source)
		models: List["torch_interface.nn.Module"] = []  # type: ignore[name-defined]
		manifests: List[dict] = []

		if os.path.isdir(path_str):
			rep_dirs: List[str] = []
			try:
				def _collect_reps(d_path: str) -> List[str]:
					found: List[str] = []
					for name in sorted(os.listdir(d_path)):
						full = os.path.join(d_path, name)
						if not os.path.isdir(full):
							continue
						if name.lower().startswith("rep"):
							rest = name[3:]
							if rest and rest.isdigit():
								found.append(full)
					return found

				# 1) Check if current dir is already a rep dir
				direct_rep_to_check = Path(path_str.lower()).name
				if direct_rep_to_check.startswith("rep") and direct_rep_to_check[3:].isdigit():
					rep_dirs = [path_str]
				else:
					# 2) Check for immediate rep subdirectories
					rep_dirs = _collect_reps(path_str)

					# 3) If no reps found, check if there is a single timestamp-like subdirectory
					#    that contains rep subdirectories (common for FamilyRun output).
					if not rep_dirs:
						subdirs = [
							os.path.join(path_str, n)
							for n in os.listdir(path_str)
							if os.path.isdir(os.path.join(path_str, n))
						]
						for sd in subdirs:
							# Basic heuristic: if it looks like YYYYMMDD_HHMMSS or similar
							# but we can just check if it contains reps.
							found_in_sub = _collect_reps(sd)
							if found_in_sub:
								rep_dirs.extend(found_in_sub)
			except Exception as e:
				print(f"Error processing directory {path_str}:")
				traceback.print_exception(e)
				rep_dirs = []

			if rep_dirs:
				for d in rep_dirs:
					manifest_path = resolve_manifest_path(d)
					if not os.path.exists(manifest_path):
						continue
					m, man, _ = load_model_from_manifest(manifest_path, map_location=str(self._device))
					models.append(m)
					manifests.append(man)
				if not models:
					raise RuntimeError(f"No models could be loaded from directory: {path_str}")
			else:
				# Single model in directory
				manifest_path = resolve_manifest_path(path_str)
				m, man, _ = load_model_from_manifest(manifest_path, map_location=str(self._device))
				models.append(m)
				manifests.append(man)
		else:
			# Treat as a file path (manifest or weights)
			manifest_path = resolve_manifest_path(path_str)
			m, man, _ = load_model_from_manifest(manifest_path, map_location=str(self._device))
			models.append(m)
			manifests.append(man)

		# Validate manifests are consistent across ensemble members
		base_manifest = manifests[0]
		base_in = int(base_manifest.get("input_size", 0))
		base_out = int(base_manifest.get("output_size", 0))
		for man in manifests[1:]:
			if int(man.get("input_size", 0)) != base_in or int(man.get("output_size", 0)) != base_out:
				raise ValueError("All ensemble members must share input_size and output_size")

		return models, dict(base_manifest)

	def _get_rss(self) -> int | None:
		if psutil is None:
			return None
		try:
			return psutil.Process(os.getpid()).memory_info().rss
		except Exception:
			return None

	def _maybe_cache_ensemble_to_disk(self) -> None:
		"""If process RSS exceeds 50% of physical memory, offload ensemble to disk.
		We serialize each model to a temporary file and drop in-memory references.
		"""
		try:
			if not self._models:
				return
			if psutil is None:
				return
			total_mem = psutil.virtual_memory().total
			current_rss = self._get_rss() or 0
			if current_rss <= int(total_mem * 0.5):
				return
			# Ensure cache dir
			base_dir = self.output_directory or os.getcwd()
			cache_dir = os.path.join(base_dir, "cache", "recommender_models")
			os.makedirs(cache_dir, exist_ok=True)
			# Save and clear
			self._ensemble_cache_paths = []
			for i, m in enumerate(self._models, start=1):
				path = os.path.join(cache_dir, f"ensemble_{i}.pt")
				try:
					torch.save(m, path)
					self._ensemble_cache_paths.append(path)
				except Exception as e:
					print(f"Warning: failed to cache ensemble member {i} to disk: {e}")
			# Drop in-memory models
			self._models = []
			self._cached_to_disk = len(self._ensemble_cache_paths) > 0
			if torch.cuda.is_available():
				torch.cuda.empty_cache()
		except Exception as e:
			print(f"Warning: _maybe_cache_ensemble_to_disk failed: {e}")

	def get_manifest(self) -> dict:
		"""Return a shallow copy of the underlying manifest metadata.

		This is primarily intended for tooling/CLIs that need to inspect
		training-time configuration such as ``prior_days`` or feature flags.
		Callers should treat the returned dict as read-only.
		"""
		# We don't have _manifest anymore. We can reconstruct it from _single_run if needed,
		# or just return the single_run dict.
		from trainer.io_run import create_manifest_dictionary
		return create_manifest_dictionary(
			self._single_run,
			None, None, "",
			int(self.input_size or 0),
			int(self.output_size or 0)
		)

	def get_model_number(self):
		return len(self._models)

	def to_cpu(self) -> None:
		"""Move all models in the ensemble to CPU and clear GPU cache."""
		for m in self._models:
			m.to("cpu")
		if torch.cuda.is_available():
			torch.cuda.empty_cache()

	def get_output_directory(self):
		return self.output_directory

	# resolve_manifest_path moved to models.utils

	def get_input_size(self) -> int:
		"""Return the input size (number of features) expected by the model(s)."""
		return self.input_size

	def get_required_previous_days(self) -> int:
		"""Return how many prior days the model expects as context."""
		return super().get_required_previous_days()

	def get_day_change_centered(self) -> str:
		"""Return the day-change centering mode used when training the model.

		If not present in the manifest, this falls back to "zero", which matches
		the historical default used in training scripts.
		"""
		return super().get_day_change_centered()

	def get_current_day_change_used(self) -> bool:
		"""Return whether current-day price change features were used.
		"""
		return super().get_current_day_change_used()

	def get_volume_ratio_used(self) -> bool:
		"""Return the type of volume ratio used in the model."""
		return super().get_volume_ratio_used()

	def get_vwap_ratio_used(self) -> bool:
		"""Return the type of vwap ratio used in the model."""
		return super().get_vwap_ratio_used()

	def get_tranaction_ratio_used(self) -> bool:
		"""Return whether transaction-ratio features were used.
		"""
		return super().get_tranaction_ratio_used()

	def get_num_features_per_ticker(self) -> int:
		"""Return the number of features per ticker for a single day."""
		T = self._output_size
		D = self.get_required_previous_days() + 1
		if T and D:
			return int(self.input_size // D // T)
		return 0


	def _to_tensor(self, x: ArrayLike) -> "torch_interface.Tensor":  # type: ignore[name-defined]
		if torch is None:
			raise RuntimeError("PyTorch is required for inference.")

		if isinstance(x, torch.Tensor):  # type: ignore[attr-defined]
			t = x
		elif isinstance(x, np.ndarray):
			t = torch.from_numpy(x.astype(np.float32, copy=False))  # type: ignore[attr-defined]
		else:
			# Generic iterable of floats
			t = torch.tensor(list(x), dtype=torch.float32)  # type: ignore[attr-defined]

		# Flatten to 1D and validate size
		t = t.to(self._device).reshape(-1)
		if t.numel() != self.input_size:  # type: ignore[attr-defined]
			raise ValueError(
				f"Feature vector size mismatch: expected {self._input_size}, got {int(t.numel())}"
			)
		return t

	def generateStockScoresForNetwork(
		self,
		network: nn.Module,
		inputs: "torch_interface.Tensor",
		save_to_file: bool = False,
		epoch_name: str = "",
		date_id: Optional[int] = None,
		network_index: Optional[int] = None,
	) -> "torch_interface.Tensor":
		"""Run a single network and return the scores.

		If save_to_file is True, the scores are saved to a file.
		Prefer saving to the individual rep directory if network_index is provided.
		The filename includes the epoch_name, date_id and network_index if provided.
		"""
		with torch.no_grad():
			if self._restricted_memory_mode and str(self._device).startswith("cuda"):
				network = network.to(self._device)

			raw_output = network(inputs)
			raw_output = torch.nan_to_num(raw_output, nan=0.0, posinf=0.0, neginf=0.0)  # type: ignore[attr-defined]
			out_flat = raw_output.reshape(-1).to(torch.float32)  # type: ignore[attr-defined]

			if self._ensemble_normalize:
				global print_recommendation_type
				if not print_recommendation_type:
					print(
						"StockRecommenderFromModel: applying per-model normalization before ensemble aggregation.",
					)
					print_recommendation_type = True
				total = float(out_flat.sum().item())
				if total > 0.0:
					out_flat = out_flat / total

			if save_to_file:
				save_dir = None
				if network_index is not None:
					# Try to get the specific rep directory (network_index is 0-based, rep_index is 1-based)
					save_dir = self._single_run.file_structure.get_directory_for_rep(network_index + 1)

				if not save_dir:
					save_dir = self._single_run.file_structure.run_group_dir

				if save_dir:
					os.makedirs(save_dir, exist_ok=True)
					# Use a fixed net_0 suffix when saving from a specific recommender's context.
					# This ensures that even in an ensemble where we have multiple rep directories,
					# each rep's own score file is named net_0 within its own directory.
					filename = f"stock_scores_epoch_{epoch_name}"
					if date_id is not None:
						filename += f"_date_{date_id}"
					filename += "_net_0.csv"
					save_path = os.path.join(save_dir, filename)
					# Save as CSV instead of .pt
					scores_np = out_flat.detach().cpu().numpy()
					np.savetxt(save_path, scores_np, delimiter=",")
					# print(f"Saved stock scores to {save_path}")
				else:
					print("DEBUG: save_dir is None or empty!")

			if self._restricted_memory_mode and str(self._device).startswith("cuda"):
				network.to("cpu")
				if torch.cuda.is_available():
					torch.cuda.empty_cache()

			res = out_flat.detach()
			# Explicitly delete temporary tensors to help GC
			del raw_output, out_flat
			return res

	def getBuyRecommendations(
		self,
		feature_vector: Optional[ArrayLike] = None,
		top_k: int = 10,
		index_base: int = 1,
		*,
		current_date_id: Optional[int] = None,
		dbinfo_file: Optional[str] = None,
		transactions_per_ticker: Optional[Union[np.ndarray, Iterable[float]]] = None,
		clear_gpu_cache: bool = False,
		save_scores: bool = False,
		epoch_name: str = "",
		precalculated_scores: Optional[torch.Tensor] = None,
		precalculated_transactions: Optional[Dict[int, float]] = None,
		provider: Optional["TrainingInstanceProvider"] = None,
	) -> Tuple[np.ndarray, List[int]]:
		"""Run the model and produce recommendations.

		feature_vector: Flattened input matching the model's expected input_size.
		top_k: How many top outputs to return as ticker ids.
		return_scores: If True, return the full output scores as a NumPy array as first item.
		index_base: 1 to return ticker ids as 1..T, 0 to return 0..T-1.
		precalculated_scores: If provided, use these scores instead of running the model.
		                      feature_vector can be None if precalculated_scores is provided.
		provider: If provided and feature_vector is None, use this provider to load the feature vector
		          for current_date_id.
		precalculated_transactions: If provided, use these transaction counts instead of fetching from DB.
		                             Mapping: ticker_id -> transactions
		...
		"""

		global print_recommendation_type

		if top_k <= 0:
			top_k = 1
		top_k = int(min(top_k, self._output_size))

		if precalculated_scores is not None:
			out_from_model_flat_cpu = precalculated_scores.detach().cpu()
		else:
			if feature_vector is None:
				if provider is not None and current_date_id is not None:
					# Find the instance index for this date_id
					try:
						# IndexDataset or similar can help, but we might have to search in provider
						# cumulative_dates_for_train_validation_and_test_sets.
						# Note: TrainingInstanceProvider.get_feature_and_label_pair(instance_index)
						# where instance_index is 1-based.
						dates = provider.cumulative_dates_for_train_validation_and_test_sets
						if hasattr(dates, "numpy"):
							dates = dates.numpy()

						# Find index in dates
						matching_indices = np.where(dates == int(current_date_id))[0]
						if matching_indices.size > 0:
							abs_idx = int(matching_indices[0])
							num_prior = provider.number_of_prior_days_to_include_in_feature
							instance_idx = abs_idx - num_prior + 1
							if instance_idx >= 1:
								feature_vector, _, _ = provider.get_feature_and_label_pair(instance_idx)
							else:
								raise ValueError(f"date_id {current_date_id} does not have enough prior context.")
						else:
							raise ValueError(f"date_id {current_date_id} not found in provider.")
					except Exception as e:
						raise ValueError(f"Failed to load feature_vector for date_id {current_date_id} from provider: {e}")

				if feature_vector is None:
					raise ValueError("feature_vector must be provided if precalculated_scores is None.")
			feature_tensor = self._to_tensor(feature_vector)
			# print(f"DEBUG: feature_tensor mean={feature_tensor.mean().item():.6f}, std={feature_tensor.std().item():.6f}")
			with torch.no_grad():  # type: ignore[attr-defined]
				feature_tensor = torch.nan_to_num(feature_tensor, nan=0.0, posinf=0.0, neginf=0.0)  # type: ignore[attr-defined]
				input_to_model = feature_tensor.reshape(1, -1)

				# Aggregate ensemble outputs, supporting both in-memory and disk-cached ensembles
				agg: Optional["torch_interface.Tensor"] = None  # type: ignore[name-defined]

				def _accumulate_from_model(one_model: nn.Module, net_idx: Optional[int] = None) -> None:
					nonlocal agg
					out_from_model_flat = self.generateStockScoresForNetwork(
						one_model,
						input_to_model,
						save_to_file=save_scores,
						epoch_name=epoch_name,
						date_id=current_date_id,
						network_index=net_idx,
					)
					if agg is None:
						agg = out_from_model_flat
					else:
						agg = agg + out_from_model_flat
					# cleanup
					del out_from_model_flat

				if self._cached_to_disk and self._ensemble_cache_paths:
					# Load each model from disk, run, then release
					for idx, path in enumerate(self._ensemble_cache_paths):
						try:
							# Use unified loader from trainer.model_loader
							# Use the family run from the single run directly
							fr = self._single_run.family_run

							m, _, _ = load_model_from_checkpoint(
								path,
								fr,
								int(self.input_size),
								int(self.output_size),
								int(self._single_run.mlp_layers),
								str(self._single_run.activation),
								int(self._single_run.prior_days),
								device=self._device,
								map_location="cpu"
							)
							_accumulate_from_model(m, net_idx=idx)
							del m
						finally:
							if torch.cuda.is_available():
								torch.cuda.empty_cache()
				else:
					for idx, one_model in enumerate(self._models):  # type: ignore[attr-defined]
						_accumulate_from_model(one_model, net_idx=idx)

				if agg is None:
					raise RuntimeError("Model aggregation failed (could be aggregation of a single model).")
				out_from_model_flat_cpu = agg.detach().cpu()
				# Explicitly clear agg from GPU
				del agg
				if 'input_to_model' in locals():
					del input_to_model

				# Explicitly clear feature_tensor from GPU
				del feature_tensor

		out_from_model_flat = out_from_model_flat_cpu
		# This filter is ALWAYS enforced; we obtain transactions from, in priority order:
		# 1) explicit precalculated_transactions argument
		# 2) explicit transactions_per_ticker argument
		# 3) database query using (current_date_id, dbinfo_file)
		# 4) transactions column present in the feature vector (legacy fallback)
		T = int(self._output_size)
		mask_allowed: Optional["torch_interface.Tensor"] = None  # type: ignore[name-defined]
		transactions_vec_tensor: Optional["torch_interface.Tensor"] = None  # type: ignore[name-defined]

		# 1) Precalculated map provided (e.g. from bulk pre-fetch)
		if precalculated_transactions is not None:
			# Initialize zeros; missing tickers will be treated as 0 transactions
			arr = np.zeros((T,), dtype=np.float32)
			if self.ticker_ids is not None and len(self.ticker_ids) > 0:
				for i, tid in enumerate(self.ticker_ids):
					val = precalculated_transactions.get(int(tid))
					if val is not None:
						arr[i] = float(val)
			else:
				# Dense slots
				for tid, val in precalculated_transactions.items():
					if 1 <= int(tid) <= T:
						arr[int(tid) - 1] = float(val)
			transactions_vec_tensor = torch.from_numpy(arr).to(self._device)

		# 2) Explicit vector provided by caller
		if transactions_vec_tensor is None and transactions_per_ticker is not None:
			try:
				if isinstance(transactions_per_ticker, np.ndarray):
					arr = transactions_per_ticker.astype(np.float32, copy=False)
				else:
					arr = np.asarray(list(transactions_per_ticker), dtype=np.float32)
				if arr.size != T:
					raise ValueError(f"transactions_per_ticker length mismatch: expected {T}, got {arr.size}")
				transactions_vec_tensor = torch.from_numpy(arr).to(self._device)  # type: ignore[attr-defined]
			except Exception as e:
				raise ValueError(f"Invalid transactions_per_ticker: {e}")

		# 2) DB fetch by date_id
		if transactions_vec_tensor is None and current_date_id is not None and dbinfo_file:
			try:
				conn = get_connection(dbinfo_file=dbinfo_file)
				cur = conn.cursor()
				cur.execute(
					"""
					SELECT ticker_id, transactions
					FROM raw_stock_data
					WHERE date_id = %s
					""",
					(int(current_date_id),),
				)
				rows = cur.fetchall() or []
				# Initialize zeros; missing tickers will be treated as 0 transactions
				arr = np.zeros((T,), dtype=np.float32)
				for r in rows:
					# Access by position (driver-independent); expect (ticker_id, transactions)
					tid = int(r[0])
					val = r[1]

					slot_idx = -1
					if self.ticker_ids is not None and len(self.ticker_ids) > 0:
						try:
							if isinstance(self.ticker_ids, list):
								slot_idx = self.ticker_ids.index(tid)
							else:
								# Assume numpy array or similar
								matching_indices = np.where(self.ticker_ids == tid)[0]
								if matching_indices.size > 0:
									slot_idx = int(matching_indices[0])
						except (ValueError, IndexError):
							pass
					else:
						# Fallback to 1-based indexing if no ticker_ids list
						if 1 <= tid <= T:
							slot_idx = tid - 1

					if slot_idx != -1 and val is not None:
						try:
							arr[slot_idx] = float(val)
						except Exception:
							pass
				transactions_vec_tensor = torch.from_numpy(arr).to(self._device)  # type: ignore[attr-defined]
			finally:
				try:
					conn.close()
				except Exception:
					pass

		# 3) Fallback to transactions in the feature vector (if present)
		if transactions_vec_tensor is None and feature_vector is not None:
			try:
				F = (
					1
					+ int(bool(self.get_current_day_change_used()))
					+ int(bool(self.get_volume_ratio_used()))
					+ int(bool(self.get_vwap_ratio_used()))
					+ int(bool(self.get_tranaction_ratio_used()))
				)
				if self.get_tranaction_ratio_used() and F > 0 and T > 0:
					# Column index of transactions within the per-ticker slice for current day
					transactions_col = 1
					if self.get_current_day_change_used():
						transactions_col += 1
					if self.get_volume_ratio_used():
						transactions_col += 1
					if self.get_vwap_ratio_used():
						transactions_col += 1
					current_start = 0
					current_end = current_start + (T * F)
					# Re-convert to tensor if we don't have it (might have been deleted)
					feature_tensor = self._to_tensor(feature_vector)
					fv_current = feature_tensor[current_start:current_end]
					indices = torch.arange(T, device=fv_current.device, dtype=torch.int64)  # type: ignore[attr-defined]
					col_idx = indices * F + int(transactions_col)
					transactions_vec_tensor = fv_current[col_idx].to(torch.float32)  # type: ignore[attr-defined]
					del feature_tensor
			except Exception:
				transactions_vec_tensor = None

		# Enforce availability
		if transactions_vec_tensor is None:
			raise ValueError(
				"Transactions filter requires counts for the current date. Provide 'transactions_per_ticker' or both 'current_date_id' and 'dbinfo_file', or include the transactions feature in the vector."
			)

		# Build mask
		mask_allowed = torch.isfinite(transactions_vec_tensor) & (transactions_vec_tensor >= float(MIN_TRADES_FOR_RECOMMENDATION))  # type: ignore[attr-defined]

		# Clone scores and apply the allow-mask by setting disallowed scores to -inf.
		# This ensures we don't recommend or return valid scores for stocks that
		# failed the transactions filter.
		masked_scores = out_from_model_flat.clone()
		if mask_allowed is not None and mask_allowed.numel() == self._output_size:  # type: ignore[attr-defined]
			masked_scores[~mask_allowed.cpu()] = -float("inf")

		# Convert to numpy for returning. We return the MASKED scores so that
		# display logic doesn't inadvertently show valid scores for filtered tickers.
		scores_np = masked_scores.numpy()

		# Argpartition/argsort for top-k indices (largest first)
		topk_vals, topk_idx = torch.topk(masked_scores, k=top_k, largest=True, sorted=True)  # type: ignore[attr-defined]
		idx_list = topk_idx.tolist()

		# Map to ticker ids if mapping present; else convert to index_base offset
		top_scoring_ticker_ids: List[int] = []
		if self.ticker_ids is not None and len(self.ticker_ids) > 0 and len(self.ticker_ids) == self._output_size:
			for i in idx_list:
				if masked_scores[i] > -float("inf"):
					top_scoring_ticker_ids.append(int(self.ticker_ids[i]))
		else:
			# Dense slots -> ticker ids 1..T or 0..T-1
			offset = 1 if int(index_base) == 1 else 0
			for i in idx_list:
				if masked_scores[i] > -float("inf"):
					top_scoring_ticker_ids.append(int(i + offset))

		# Explicitly clear remaining GPU tensors
		del transactions_vec_tensor, mask_allowed, topk_vals, topk_idx

		if clear_gpu_cache and torch.cuda.is_available():
			torch.cuda.empty_cache()

		return scores_np, top_scoring_ticker_ids

	@property
	def output_size(self):
		return self._output_size

	@output_size.setter
	def output_size(self, value):
		self._output_size = value

	def email_recommendations(self, recommendations_text: str, recipient_email: str = "vagabard@gmail.com"):
		"""Send recommendations via email.

		Args:
			recommendations_text: The text content of the recommendations.
			recipient_email: The email address to send the recommendations to.
		"""
		smtp_host = os.getenv("SMTP_HOST", "localhost")
		smtp_port = int(os.getenv("SMTP_PORT", "25"))
		smtp_user = os.getenv("SMTP_USER")
		smtp_pass = os.getenv("SMTP_PASS")

		msg = MIMEText(recommendations_text)
		msg["Subject"] = "Stock Recommendations"
		msg["From"] = smtp_user or "recommender@stockguidance.com"
		msg["To"] = recipient_email

		try:
			with smtplib.SMTP(smtp_host, smtp_port) as server:
				if smtp_user and smtp_pass:
					server.starttls()
					server.login(smtp_user, smtp_pass)
				server.send_message(msg)
			print(f"Recommendations emailed to {recipient_email}")
		except Exception as e:
			print(f"Failed to send email to {recipient_email}: {e}")


def main(self):
	print("got path ", resolve_manifest_path("C:\\Projects\\StockGuidance\\data\\run_family_20251208_222008\\20251208_222008\\rep1"))


__name__ == "__main__" and main(StockRecommenderFromModel)

__all__ = ["StockRecommenderFromModel"]
