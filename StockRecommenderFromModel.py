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
import traceback
from pathlib import Path
from typing import Iterable, List, Tuple, Union, Optional

import numpy as np
import torch  # type: ignore
from torch import nn

from trainer import torch_interface

TORCH_AVAILABLE = True

from models import load_model_from_manifest  # type: ignore
from models.utils import resolve_manifest_path  # type: ignore
from database.db_connect import get_connection  # type: ignore


ArrayLike = Union[np.ndarray, "torch_interface.Tensor", Iterable[float]]

print_recommendation_type = False

# Minimum number of trades (transactions) required for a ticker to be
# considered in the recommendations. Tickers with fewer trades are skipped.
# Adjust this constant to tune the threshold.
MIN_TRADES_FOR_RECOMMENDATION = 200


class StockRecommenderFromModel:
	def __init__(
		self,
		model_or_path: "List[torch_interface.nn.Module]| str",  # type: ignore[name-defined]
		output_directory: Optional[str] = None,
		model_dict: Optional[dict] = None,
		device: str = "auto",
		*,
#		manifest: Optional[dict] = None,
		ensemble_normalize: bool = False,
	) -> None:
		"""Create a recommender from either a saved model path or existing models.

		Simplified control flow:
		- If a string is passed, models are loaded from the path and then the class
		  is constructed from those models.
		- If models are passed (list preferred), the class is constructed directly
		  from that list via a common method.

		Args:
			model_or_path: Path/dir to load models from, or a list of models. A single
				model instance is accepted for backward compatibility and wrapped into a list.
			device: "auto" (cuda if available else cpu), "cpu", or "cuda".
			manifest: Manifest dict to use when models are supplied directly.
			run_dir: Unused; kept for backward compatibility.
		"""
		self.input_size = None
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
		self._ensemble_models: List["torch_interface.nn.Module"] = []  # type: ignore[name-defined]
		self._is_ensemble: bool = False
		self._manifest: dict = {}
		self.output_directory = output_directory

		# Unified path: either load from string source, or build from provided models
		if isinstance(model_or_path, str):
			models, loaded_manifest = self._load_models_from_source(model_or_path)
			self.output_directory = model_or_path
			# Merge overrides
			final_manifest = dict(loaded_manifest)
			if model_dict:
				final_manifest.update(model_dict)
			self._construct_from_models(models, manifest=final_manifest)
		elif isinstance(model_or_path, List) and isinstance(model_or_path[0], nn.Module) :
			self._construct_from_models(model_or_path)
		else:
			raise ValueError("Must provide either a model path or a list of models.")

	def _construct_from_models(
		self,
		models: List["torch_interface.nn.Module"],  # type: ignore[name-defined]
		*,
		manifest: Optional[dict] = None,
	) -> None:
		"""Finalize construction from a list of models, ensuring unified manifest."""
		self._ensemble_models = [m.to(self._device).eval() for m in models]
		self._is_ensemble = len(self._ensemble_models) > 1

		self._manifest = dict(manifest or {})
		# Cache common dims
		self.input_size: int = self._ensemble_models[0].input_size
		self.output_size: int = self._ensemble_models[0].output_size
		if self.input_size <= 0 or self.output_size <= 0:
			raise ValueError("Invalid model manifest: missing input_size/output_size")

	def _load_models_from_source(self, source: str) -> Tuple[List["torch_interface.nn.Module"], dict]:  # type: ignore[name-defined]
		"""Load models from a path or directory and return (models, manifest)."""
		path_str = os.path.abspath(source)
		models: List["torch_interface.nn.Module"] = []  # type: ignore[name-defined]
		manifests: List[dict] = []

		if os.path.isdir(path_str):
			rep_dirs: List[str] = []
			try:
				direct_rep_to_check = Path(path_str.lower()).name
				if direct_rep_to_check.startswith("rep"):
					rest = direct_rep_to_check[3:]
					if rest and rest.isdigit():
						rep_dirs.append(path_str)
				else:
					for name in sorted(os.listdir(path_str)):
						full = os.path.join(path_str, name)
						if not os.path.isdir(full):
							continue
						if not name.lower().startswith("rep"):
							continue
						rest = name[3:]
						if rest and rest.isdigit():
							rep_dirs.append(full)
			except FileNotFoundError as e:
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

	def get_manifest(self) -> dict:
		"""Return a shallow copy of the underlying manifest metadata.

		This is primarily intended for tooling/CLIs that need to inspect
		training-time configuration such as ``prior_days`` or feature flags.
		Callers should treat the returned dict as read-only.
		"""

		return dict(self._manifest)

	def get_model_number(self):
		return len(self._ensemble_models)

	def get_output_directory(self):
		return self.output_directory

	# resolve_manifest_path moved to models.utils

	def get_required_previous_days(self) -> int:
		"""Return how many prior days the model expects as context."""
		return int(self._manifest.get("prior_days", 0))

	def get_day_change_centered(self) -> str:
		"""Return the day-change centering mode used when training the model.

		If not present in the manifest, this falls back to "zero", which matches
		the historical default used in training scripts.
		"""

		value = self._manifest.get("day_change_centered")
		if isinstance(value, str) and value:
			return value
		return "zero"

	def get_current_day_change_used(self) -> bool:
		"""Return whether current-day price change features were used.

		Prefers the canonical manifest key "current_day_change", but also
		accepts older aliases such as "current_day_change_used" or even the
		original CLI argument name "change_in_price_today" if present in the
		manifest for backward compatibility.
		"""
		if "current_day_change" in self._manifest:
			return bool(self._manifest.get("current_day_change"))
		# Legacy alias from earlier manifests
		if "current_day_change_used" in self._manifest:
			return bool(self._manifest.get("current_day_change_used"))
		# Fallback to raw arg name if ever serialized directly
		if "change_in_price_today" in self._manifest:
			return bool(self._manifest.get("change_in_price_today"))
		return False

	def get_volume_ratio_used(self) -> bool:
		"""Return the type of volume ratio used in the model."""
		return self._manifest.get("volume_ratio", False)

	def get_vwap_ratio_used(self) -> bool:
		"""Return the type of vwap ratio used in the model."""
		return self._manifest.get("vwap_ratio", False)

	def get_tranaction_ratio_used(self) -> bool:
		"""Return whether transaction-ratio features were used.

		The manifest historically stored this under the misspelled key
		"tranaction_ratio". New manifests use the correctly spelled
		"transaction_ratio" key. For robustness, we check both.
		"""
		if "transaction_ratio" in self._manifest:
			return bool(self._manifest.get("transaction_ratio"))
		return bool(self._manifest.get("tranaction_ratio", False))


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

	def getBuyRecommendations(
		self,
		feature_vector: ArrayLike,
		top_k: int = 10,
		index_base: int = 1,
		*,
		current_date_id: Optional[int] = None,
		dbinfo_file: Optional[str] = None,
		transactions_per_ticker: Optional[Union[np.ndarray, Iterable[float]]] = None,
	) -> Tuple[np.ndarray, List[int]]:
		"""Run the model and produce recommendations.

		feature_vector: Flattened input matching the model's expected input_size.
		top_k: How many top outputs to return as ticker ids.
		return_scores: If True, return the full output scores as a NumPy array as first item.
		index_base: 1 to return ticker ids as 1..T, 0 to return 0..T-1.

		Always-on transactions filter:
		- The list of recommended tickers is filtered to include only those with
		  current-day transactions >= MIN_TRADES_FOR_RECOMMENDATION.
		- Provide one of the following so the filter can be applied:
		  (a) transactions_per_ticker: iterable/array of length T with counts
		  (b) current_date_id + dbinfo_file: function will query raw_stock_data
		  (c) If the feature vector includes a transactions column (from the
		      transaction feature flag), it will be used as a fallback.
		If none of the above are available, a ValueError is raised to enforce
		the always-on filter.

		Returns: (scores, top_ticker_ids)
		  - scores: NumPy array of shape (T,) with model outputs.
		  - top_ticker_ids: list[int] of length min(top_k, T) mapping to ticker ids.
		"""

		global print_recommendation_type

		if top_k <= 0:
			top_k = 1
		top_k = int(min(top_k, self._output_size))

		feature_tensor = self._to_tensor(feature_vector)
		with torch.no_grad():  # type: ignore[attr-defined]
			feature_tensor = torch.nan_to_num(feature_tensor, nan=0.0, posinf=0.0, neginf=0.0)  # type: ignore[attr-defined]
			input_to_model = feature_tensor.reshape(1, -1)

			# If this instance represents an ensemble of models (e.g. rep1/rep2
			# directories under a common run directory), aggregate their outputs.
			# model_count = len(self._ensemble_models)  # type: ignore[attr-defined]
			# Log how many models are being used for this recommendation call.
			# print(
			# 	f"StockRecommenderFromModel: using {model_count} model(s) "
			# 	"for ensemble recommendations.",
			# )
			agg: Optional["torch_interface.Tensor"] = None  # type: ignore[name-defined]
			for one_model in self._ensemble_models:  # type: ignore[attr-defined]
				raw_output_from_model = one_model(input_to_model)
				raw_output_from_model = torch.nan_to_num(raw_output_from_model, nan=0.0, posinf=0.0, neginf=0.0)  # type: ignore[attr-defined]
				out_from_model_flat = raw_output_from_model.reshape(-1).to(torch.float32)  # type: ignore[attr-defined]
				if self._ensemble_normalize:
					# Normalize this model's scores so they sum to 1. Guard against
					# degenerate all-zero outputs by leaving them as-is.
					if not print_recommendation_type:
						print(
							"StockRecommenderFromModel: applying per-model "
							"normalization before ensemble aggregation.",
						)
						print_recommendation_type = True
					total = float(out_from_model_flat.sum().item())
					if total > 0.0:
						out_from_model_flat = out_from_model_flat / total
				if agg is None:
					agg = out_from_model_flat
				else:
					agg = agg + out_from_model_flat
			if agg is None:
				raise RuntimeError("Model aggregation failed (could be aggregation of a single model).")
				# Fallback: no ensemble members produced outputs; behave like a
				# single zero vector to avoid crashes.
#				agg = torch.zeros(self._output_size, dtype=torch.float32, device=self._device)  # type: ignore[attr-defined]
			out_from_model_flat = agg.detach().cpu()

		# Build the allow-mask to exclude tickers with too-few trades on the current day.
		# This filter is ALWAYS enforced; we obtain transactions from, in priority order:
		# 1) explicit transactions_per_ticker argument
		# 2) database query using (current_date_id, dbinfo_file)
		# 3) transactions column present in the feature vector (legacy fallback)
		T = int(self._output_size)
		mask_allowed: Optional["torch_interface.Tensor"] = None  # type: ignore[name-defined]
		transactions_vec_tensor: Optional["torch_interface.Tensor"] = None  # type: ignore[name-defined]

		# 1) Explicit vector provided by caller
		if transactions_per_ticker is not None:
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
					if 1 <= tid <= T and val is not None:
						try:
							arr[tid - 1] = float(val)
						except Exception:
							pass
				transactions_vec_tensor = torch.from_numpy(arr).to(self._device)  # type: ignore[attr-defined]
			finally:
				try:
					conn.close()
				except Exception:
					pass

		# 3) Fallback to transactions in the feature vector (if present)
		if transactions_vec_tensor is None:
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
					fv_current = feature_tensor[current_start:current_end]
					indices = torch.arange(T, device=fv_current.device, dtype=torch.int64)  # type: ignore[attr-defined]
					col_idx = indices * F + int(transactions_col)
					transactions_vec_tensor = fv_current[col_idx].to(torch.float32)  # type: ignore[attr-defined]
			except Exception:
				transactions_vec_tensor = None

		# Enforce availability
		if transactions_vec_tensor is None:
			raise ValueError(
				"Transactions filter requires counts for the current date. Provide 'transactions_per_ticker' or both 'current_date_id' and 'dbinfo_file', or include the transactions feature in the vector."
			)

		# Build mask
		mask_allowed = torch.isfinite(transactions_vec_tensor) & (transactions_vec_tensor >= float(MIN_TRADES_FOR_RECOMMENDATION))  # type: ignore[attr-defined]

		# Convert to numpy for easy handling (retain original scores)
		scores_np = out_from_model_flat.numpy()

		# Argpartition/argsort for top-k indices (largest first)
		# Apply the allow-mask by setting disallowed scores to -inf
		if mask_allowed is not None and mask_allowed.numel() == self._output_size:  # type: ignore[attr-defined]
			masked_scores = out_from_model_flat.clone()
			masked_scores[~mask_allowed] = -float("inf")
			topk_vals, topk_idx = torch.topk(masked_scores, k=top_k, largest=True, sorted=True)  # type: ignore[attr-defined]
		else:
			# Defensive: should not happen because mask is required; keep fallback
			topk_vals, topk_idx = torch.topk(out_from_model_flat, k=top_k, largest=True, sorted=True)  # type: ignore[attr-defined]
		idx_list = topk_idx.tolist()

		# Map to ticker ids if mapping present; else convert to index_base offset
		top_ids: List[int] = []
		# if self._ticker_id_map and len(self._ticker_id_map) == self._output_size:
		# 	for i in idx_list:
		# 		if self._ticker_id_map[i]:
		# 			top_ids.append(int(self._ticker_id_map[i]))
		# else:
		if True:
			# Dense slots -> ticker ids 1..T or 0..T-1
			offset = 1 if int(index_base) == 1 else 0
			for i in idx_list:
				top_ids.append(int(i + offset))

		return scores_np, top_ids

	@property
	def output_size(self):
		return self._output_size

	@output_size.setter
	def output_size(self, value):
		self._output_size = value


def main(self):
	print("got path ", resolve_manifest_path("C:\\Projects\\StockGuidance\\data\\run_family_20251208_222008\\20251208_222008\\rep1"))


__name__ == "__main__" and main(StockRecommenderFromModel)

__all__ = ["StockRecommenderFromModel"]
