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

import json
import os
from typing import Iterable, List, Tuple, Union, Optional

import numpy as np

try:  # torch is optional at import-time, but required for inference
	import torch  # type: ignore
	TORCH_AVAILABLE = True
except Exception:  # pragma: no cover - allow environment without torch
	torch = None  # type: ignore
	TORCH_AVAILABLE = False

from models import load_model_from_manifest  # type: ignore


ArrayLike = Union[np.ndarray, "torch.Tensor", Iterable[float]]

print_recommendation_type = False


class StockRecommenderFromModel:
	def __init__(
		self,
		model_or_path: "torch.nn.Module | str",  # type: ignore[name-defined]
		model_path: Optional[str] = None,
		model_dict: Optional[dict] = None,
		device: str = "auto",
		*,
		manifest: Optional[dict] = None,
		run_dir: Optional[str] = None,
		ensemble_normalize: bool = False,
	) -> None:
		"""Create a recommender from either a saved model path or an existing model.

		This constructor is intentionally backward compatible:
		- If ``model_or_path`` is a string, it is treated as before: a path to
		  either the model_manifest.json file or a directory containing it.
		- If ``model_or_path`` is an already loaded ``torch.nn.Module`` then the
		  caller must also supply ``manifest`` describing the model metadata
		  (at minimum ``input_size`` and ``output_size``). ``run_dir`` may be
		  provided to point at the directory that would normally contain the
		  manifest/ticker mapping files (defaults to current working directory).

		Args:
			model_or_path: Path/dir to a manifest *or* an already-loaded model.
			device: "auto" (cuda if available else cpu), "cpu", or "cuda".
			manifest: Manifest dict to use when an existing model instance is
				provided instead of a path. Ignored for path-based loading.
			run_dir: Optional directory used to resolve ``ticker_id_map.json``
				when an existing model is supplied. Ignored for path-based loading.
		"""
		if not TORCH_AVAILABLE:
			raise RuntimeError("PyTorch is required to use StockRecommenderFromModel.")
		global print_recommendation_type
		print_recommendation_type= False
		# Resolve device once; used in both construction modes
		if device == "auto":
			dev = "cuda" if torch.cuda.is_available() else "cpu"  # type: ignore[attr-defined]
		else:
			dev = device
		self._device = torch.device(dev)  # type: ignore[call-arg]
		self._ensemble_normalize: bool = bool(ensemble_normalize)
		self._ensemble_models: List["torch.nn.Module"] = []  # type: ignore[name-defined]
		self._is_ensemble: bool = False

		# --- Mode 1: path/dir string (existing behaviour, extended for ensembles) ---
		if isinstance(model_or_path, str):
			path_str = os.path.abspath(model_or_path)
			# If a directory is provided, first look for repeated-run subdirectories
			# such as "rep1", "rep2", ... that each contain their own manifest.
			if os.path.isdir(path_str):
				rep_dirs: List[str] = []
				try:
					for name in sorted(os.listdir(path_str)):
						full = os.path.join(path_str, name)
						if not os.path.isdir(full):
							continue
						# Heuristic: treat subdirectories starting with "rep" followed by
						# digits as repeated-run directories.
						if not name.lower().startswith("rep"):
							continue
						rest = name[3:]
						if rest and rest.isdigit():
							rep_dirs.append(full)
				except FileNotFoundError:
					rep_dirs = []

				# If we detected repeated-run directories, treat them as an ensemble.
				if rep_dirs:
					models: List["torch.nn.Module"] = []  # type: ignore[name-defined]
					manifests: List[dict] = []
					for d in rep_dirs:
						manifest_path = self._resolve_manifest_path(d)
						if not os.path.exists(manifest_path):
							continue
						m, man, _ = load_model_from_manifest(
							manifest_path, map_location=str(self._device)
						)
						models.append(m)
						manifests.append(man)

					if not models:
						# Fall back to single-model resolution below.
						resolved_manifest = self._resolve_manifest_path(path_str)
						if not os.path.exists(resolved_manifest):
							raise FileNotFoundError(
								f"Model manifest not found: {resolved_manifest}"
							)
						model, manifest_dict, _ = load_model_from_manifest(
							resolved_manifest, map_location=str(self._device)
						)
						self._model = model
						self._manifest = manifest_dict
						self._dir = os.path.dirname(os.path.abspath(resolved_manifest))
					else:
						# Validate that all ensemble members share the same input/output sizes.
						base_manifest = manifests[0]
						input_size = int(base_manifest.get("input_size", 0))
						output_size = int(base_manifest.get("output_size", 0))
						for man in manifests[1:]:
							if int(man.get("input_size", 0)) != input_size or int(
								man.get("output_size", 0)
							) != output_size:
								raise ValueError(
									"All ensemble members must share input_size and output_size"
								)

						self._ensemble_models = [m.to(self._device).eval() for m in models]
						self._is_ensemble = len(self._ensemble_models) > 1
						# For backward-compatible attribute access, expose the first
						# model/manifest as the primary.
						self._model = self._ensemble_models[0]
						self._manifest = dict(base_manifest)
						self._dir = path_str
				else:
					# No repeated-run subdirectories; fall back to the original
					# single-model directory/manifest resolution.
					resolved_manifest = self._resolve_manifest_path(path_str)
					if not os.path.exists(resolved_manifest):
						raise FileNotFoundError(
							f"Model manifest not found: {resolved_manifest}"
						)

					model, manifest_dict, _ = load_model_from_manifest(
						resolved_manifest, map_location=str(self._device)
					)
					self._model = model
					self._manifest = manifest_dict
					self._dir = os.path.dirname(os.path.abspath(resolved_manifest))
			else:
				# Path is not a directory – treat as a file path as before.
				resolved_manifest = self._resolve_manifest_path(path_str)
				if not os.path.exists(resolved_manifest):
					raise FileNotFoundError(f"Model manifest not found: {resolved_manifest}")

				model, manifest_dict, _ = load_model_from_manifest(
					resolved_manifest, map_location=str(self._device)
				)
				self._model = model
				self._manifest = manifest_dict
				self._dir = os.path.dirname(os.path.abspath(resolved_manifest))
		else:
			# --- Mode 2: an already loaded torch.nn.Module -------------------
			model = model_or_path
			resolved_manifest = self._resolve_manifest_path(model_path or "")
			if model_dict:
				manifest = model_dict
			else:
				manifest = manifest or json.loads(
					open(resolved_manifest, "r", encoding="utf-8").read()
				)
			if manifest is None:
				raise ValueError(
					"When passing an existing model instance, a manifest dict "
					"with at least 'input_size' and 'output_size' must be provided."
				)
			self._model = model.to(self._device)
			self._model.eval()
			self._manifest = dict(manifest)
			self._dir = os.path.abspath(run_dir or os.getcwd())

		# Optional ticker id mapping sidecar
		self._ticker_id_map: Optional[List[int]] = None
		map_path = os.path.join(self._dir, "ticker_id_map.json")
		if os.path.exists(map_path):
			try:
				with open(map_path, "r", encoding="utf-8") as f:
					arr = json.load(f)
				if isinstance(arr, list) and all(isinstance(x, int) for x in arr):
					self._ticker_id_map = list(arr)
			except Exception:
				self._ticker_id_map = None

		# Cache common dims
		self._input_size: int = int(self._manifest.get("input_size", 0))
		self._output_size: int = int(self._manifest.get("output_size", 0))
		if self._input_size <= 0 or self._output_size <= 0:
			raise ValueError("Invalid model manifest: missing input_size/output_size")

	def get_manifest(self) -> dict:
		"""Return a shallow copy of the underlying manifest metadata.

		This is primarily intended for tooling/CLIs that need to inspect
		training-time configuration such as ``prior_days`` or feature flags.
		Callers should treat the returned dict as read-only.
		"""

		return dict(self._manifest)

	@staticmethod
	def _resolve_manifest_path(path: str) -> str:
		"""Resolve a manifest path using the new convention:
		- If a directory is provided, look for files that end with 'model_manifest.json'.
		  If multiple are found, raise an error. If exactly one is found, use it.
		- If a weights file (.pt/.pth) is provided, search its directory the same way.
		- If a JSON file path is provided, use it directly.
		- Otherwise, return the path as-is.
		"""
		def _find_manifest_in_dir(d: str) -> str:
			try:
				candidates = [
					os.path.join(d, f)
					for f in os.listdir(d)
					if f.lower().endswith("model_manifest.json")
				]
			except FileNotFoundError:
				candidates = []

			if len(candidates) == 1:
				return candidates[0]
			if len(candidates) > 1:
				raise RuntimeError(
					f"Multiple manifest files found in '{d}': "
					+ ", ".join(os.path.basename(c) for c in candidates)
				)
			# None found; return a conventional default path (will be validated by caller)
			return os.path.join(d, "model_manifest.json")

		# If given a directory, search for '*model_manifest.json' inside it
		if os.path.isdir(path):
			return _find_manifest_in_dir(path)
		# If given a JSON file, assume it's the manifest
		if path.lower().endswith(".json"):
			return path
		# If given a weights file, search sibling directory for '*model_manifest.json'
		if path.lower().endswith(".pt") or path.lower().endswith(".pth"):
			d = os.path.dirname(os.path.abspath(path))
			return _find_manifest_in_dir(d)
		# Fallback: treat as manifest
		return path

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


	def get_input_size(self) -> int:
		"""Return the expected flattened input feature size."""
		return self._input_size

	def _to_tensor(self, x: ArrayLike) -> "torch.Tensor":  # type: ignore[name-defined]
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
		if t.numel() != self._input_size:  # type: ignore[attr-defined]
			raise ValueError(
				f"Feature vector size mismatch: expected {self._input_size}, got {int(t.numel())}"
			)
		return t

	def getBuyRecommendations(
		self,
		feature_vector: ArrayLike,
		top_k: int = 10,
		return_scores: bool = True,
		index_base: int = 1,
	) -> Tuple[np.ndarray, List[int]]:
		"""Run the model and produce recommendations.

		feature_vector: Flattened input matching the model's expected input_size.
		top_k: How many top outputs to return as ticker ids.
		return_scores: If True, return the full output scores as a NumPy array as first item.
		index_base: 1 to return ticker ids as 1..T, 0 to return 0..T-1.

		Returns: (scores, top_ticker_ids)
		  - scores: NumPy array of shape (T,) with model outputs.
		  - top_ticker_ids: list[int] of length min(top_k, T) mapping to ticker ids.
		"""

		global print_recommendation_type

		if top_k <= 0:
			top_k = 1
		top_k = int(min(top_k, self._output_size))

		t = self._to_tensor(feature_vector)
		with torch.no_grad():  # type: ignore[attr-defined]
			t = torch.nan_to_num(t, nan=0.0, posinf=0.0, neginf=0.0)  # type: ignore[attr-defined]
			inp = t.reshape(1, -1)

			# If this instance represents an ensemble of models (e.g. rep1/rep2
			# directories under a common run directory), aggregate their outputs.
			if getattr(self, "_is_ensemble", False) and getattr(self, "_ensemble_models", None):
				model_count = len(self._ensemble_models)  # type: ignore[attr-defined]
				# Log how many models are being used for this recommendation call.
				print(
					f"StockRecommenderFromModel: using {model_count} model(s) "
					"for ensemble recommendations.",
				)
				agg: Optional["torch.Tensor"] = None  # type: ignore[name-defined]
				for m in self._ensemble_models:  # type: ignore[attr-defined]
					out = m(inp)
					out = torch.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)  # type: ignore[attr-defined]
					out_flat = out.reshape(-1).to(torch.float32)  # type: ignore[attr-defined]
					if self._ensemble_normalize:
						# Normalize this model's scores so they sum to 1. Guard against
						# degenerate all-zero outputs by leaving them as-is.
						if not print_recommendation_type:
							print(
								"StockRecommenderFromModel: applying per-model "
								"normalization before ensemble aggregation.",
							)
							print_recommendation_type = True
						total = float(out_flat.sum().item())
						if total > 0.0:
							out_flat = out_flat / total
					if agg is None:
						agg = out_flat
					else:
						agg = agg + out_flat
				if agg is None:
					# Fallback: no ensemble members produced outputs; behave like a
					# single zero vector to avoid crashes.
					agg = torch.zeros(self._output_size, dtype=torch.float32, device=self._device)  # type: ignore[attr-defined]
				out_flat = agg.detach().cpu()
			else:
				# Single-model case: preserve original behaviour.
				if not print_recommendation_type:
					print(
						"StockRecommenderFromModel: using 1 model (no ensemble) "
						"for recommendations.",
					)
					print_recommendation_type = True
				out = self._model(inp)
				out = torch.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)  # type: ignore[attr-defined]
				# Ensure 1D output shape (T,)
				out_flat = out.reshape(-1).detach().cpu().to(torch.float32)  # type: ignore[attr-defined]

		# Convert to numpy for easy handling
		scores_np = out_flat.numpy()

		# Argpartition/argsort for top-k indices (largest first)
		# Use torch.topk for simplicity and speed
		topk_vals, topk_idx = torch.topk(out_flat, k=top_k, largest=True, sorted=True)  # type: ignore[attr-defined]
		idx_list = topk_idx.tolist()

		# Map to ticker ids if mapping present; else convert to index_base offset
		top_ids: List[int] = []
		if self._ticker_id_map and len(self._ticker_id_map) == self._output_size:
			for i in idx_list:
				top_ids.append(int(self._ticker_id_map[i]))
		else:
			# Dense slots -> ticker ids 1..T or 0..T-1
			offset = 1 if int(index_base) == 1 else 0
			for i in idx_list:
				top_ids.append(int(i + offset))

		return scores_np, top_ids


__all__ = ["StockRecommenderFromModel"]
