"""
PyTorch training module for date-indexed stock data instances.

This script uses `training_instance_provider.TrainingInstanceProvider` to
construct sequential instances without shuffling and trains a compact
convolution + transformer network (no dense Linear layers).

Data split policy (deterministic, by instance index):
  - First 75% of instance indexes -> training set
  - Next 15% (75%..90%) -> validation set
  - Final 10% (90%..) -> test set (used only for final scoring)

Feature window:
  - Configure how many prior days to include via `--prior-days` (>=0).
  - Input size is inferred from the returned feature tensor length.
  - Output size equals the label tensor length.

Network architecture (revised):
  - Conv1d with kernel_size=15 (connects to 14 nearest neighbors) on the 1D input sequence
  - Transformer encoder layer (channels as model width)
  - Conv1d with kernel_size=15
  - Transformer encoder layer
  - Conv1d with kernel_size=15
  - 1x1 Conv1d to project to output channels, then global average pooling over sequence length to produce output_size

Metrics:
  - Standard Mean Squared Error (MSE) on training and validation per epoch
  - MSE restricted to the largest-valued 10 outputs (per-sample, by predicted
	output values) and the smallest-valued 10 outputs, for both train and val
  - Final metrics are also computed on the held-out test set

All metrics are printed and also written to CSV so they can be graphed later.

Example (train):
  python train_pytorch_network.py --mode train --prior-days 3 --epochs 20 --batch-size 32 \
	  --lr 1e-3 --metrics-file data/training_metrics.csv

Memory estimate only (no training):
  python train_pytorch_network.py --mode memest --prior-days 3 --batch-size 64
"""
from __future__ import annotations

import argparse
import csv
import math
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Iterator, Iterable, List, Tuple, Optional
from contextlib import nullcontext

import numpy as np

from training_instance_provider import TrainingInstanceProvider

# Make torch optional so memest mode can run without it
try:  # pragma: no cover - runtime dependency
	import torch  # type: ignore
	import torch.nn as nn  # type: ignore
	import torch.optim as optim  # type: ignore
	from torch.utils.data import DataLoader  # type: ignore
	import torch.nn.functional as F  # type: ignore
	TORCH_AVAILABLE = True
except Exception:  # pragma: no cover - allow memest without torch
	torch = None  # type: ignore
	nn = None  # type: ignore
	optim = None  # type: ignore
	DataLoader = None  # type: ignore
	TORCH_AVAILABLE = False

# Model factory (architectures moved to models/ package)
try:
	from models import make_model  # type: ignore
except Exception:
	make_model = None  # type: ignore


@dataclass
class RangeDataset:
	"""A simple dataset that pulls instances from the provider by 1-based index.

	It does not shuffle and simply iterates over the provided inclusive index range.
	"""

	provider: TrainingInstanceProvider
	start_idx: int  # inclusive, 1-based
	end_idx: int    # inclusive, 1-based

	def __len__(self) -> int:
		return max(0, self.end_idx - self.start_idx + 1)

	def __iter__(self) -> Iterator[Tuple[torch.Tensor, torch.Tensor]]:
		for idx0 in range(len(self)):
			yield self.__getitem__(idx0)

	def __getitem__(self, idx0: int) -> Tuple[torch.Tensor, torch.Tensor]:
		i = int(self.start_idx + idx0)
		x, y = self.provider.get_feature_and_label_pair(i)
		# Ensure tensors are float32
		if not isinstance(x, torch.Tensor):
			x = torch.from_numpy(np.asarray(x, dtype=np.float32))
		else:
			x = x.to(dtype=torch.float32)
		if not isinstance(y, torch.Tensor):
			y = torch.from_numpy(np.asarray(y, dtype=np.float32))
		else:
			y = y.to(dtype=torch.float32)
		# Replace NaN/Inf in features and labels to avoid propagating invalid values
		x = torch.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
		y = torch.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
		return x, y


@dataclass
class IndexDataset:
	"""Dataset that iterates over an explicit list of 1-based indices.

	Used when randomized sampling is requested. Mirrors RangeDataset behavior
	for tensor conversion and NaN/Inf handling.
	"""

	provider: TrainingInstanceProvider
	indices: List[int]

	def __len__(self) -> int:
		return len(self.indices)

	def __iter__(self) -> Iterator[Tuple[torch.Tensor, torch.Tensor]]:
		for idx0 in range(len(self)):
			yield self.__getitem__(idx0)

	def __getitem__(self, idx0: int) -> Tuple[torch.Tensor, torch.Tensor]:
		i = int(self.indices[idx0])
		x, y = self.provider.get_feature_and_label_pair(i)
		# Ensure tensors are float32
		if not isinstance(x, torch.Tensor):
			x = torch.from_numpy(np.asarray(x, dtype=np.float32))
		else:
			x = x.to(dtype=torch.float32)
		if not isinstance(y, torch.Tensor):
			y = torch.from_numpy(np.asarray(y, dtype=np.float32))
		else:
			y = y.to(dtype=torch.float32)
		# Replace NaN/Inf
		x = torch.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
		y = torch.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
		return x, y


# Network classes moved to models/; in-place definition removed.


def batched(iterable: Iterator[Tuple[torch.Tensor, torch.Tensor]], batch_size: int) -> Iterator[Tuple[torch.Tensor, torch.Tensor]]:
	batch_x: List[torch.Tensor] = []
	batch_y: List[torch.Tensor] = []
	for x, y in iterable:
		batch_x.append(x)
		batch_y.append(y)
		if len(batch_x) == batch_size:
			yield torch.stack(batch_x, dim=0), torch.stack(batch_y, dim=0)
			batch_x.clear()
			batch_y.clear()
	if batch_x:
		yield torch.stack(batch_x, dim=0), torch.stack(batch_y, dim=0)


def mse_restricted(y_pred: torch.Tensor, y_true: torch.Tensor, mode: str, k: int = 10) -> torch.Tensor:
	"""Compute MSE using only top-k or bottom-k predicted outputs per sample.

	mode: "top" or "bottom"
	y_pred/y_true: shape (B, O)
	Returns scalar tensor.
	"""
	assert mode in ("top", "bottom")
	B, O = y_pred.shape
	if k <= 0:
		return torch.tensor(0.0, device=y_pred.device, dtype=y_pred.dtype)
	k_eff = min(k, O)

	if mode == "top":
		idx = torch.topk(y_pred, k_eff, dim=1).indices
	else:
		# smallest values -> topk on negative
		idx = torch.topk(-y_pred, k_eff, dim=1).indices

	# Gather predictions and truths at selected indices
	batch_idx = torch.arange(B, device=y_pred.device).unsqueeze(1).expand(-1, k_eff)
	sel_pred = y_pred[batch_idx, idx]
	sel_true = y_true[batch_idx, idx]
	return torch.mean((sel_pred - sel_true) ** 2)


def rank_range_displacement(
	y_pred: torch.Tensor,
	y_true: torch.Tensor,
	lower: int,
	upper: int,
) -> torch.Tensor:
	"""Rank-range displacement error suitable as a metric/loss.

	For each sample and each rank position p in [lower, upper] (1-based positions):
	  - Let i_pred_at_p be the index of the item ranked p by predictions.
		Let true_pos_of_that be its position in the true ranking (1-based).
	  - Let i_true_at_p be the index of the item ranked p by the true labels.
		Let pred_pos_of_that be its position in the predicted ranking (1-based).
	  - Position error for p = (p - true_pos_of_that)^2 + (pred_pos_of_that - p)^2

	The metric returns the mean error across the selected positions and across the batch.

	Notes:
	  - If lower/upper exceed the number of outputs, they are clipped to [1, O].
	  - NaN/Inf are assumed already handled by caller.
	"""
	assert y_pred.ndim == 2 and y_true.ndim == 2, "Expected (B, O) tensors"
	B, O = y_pred.shape
	if O == 0:
		return torch.tensor(0.0, device=y_pred.device, dtype=y_pred.dtype)

	# Clip the rank window to valid range [1, O]
	lo = max(1, int(lower))
	hi = max(1, int(upper))
	if lo > hi:
		lo, hi = hi, lo
	lo = max(1, min(lo, O))
	hi = max(1, min(hi, O))
	count = hi - lo + 1
	if count <= 0:
		return torch.tensor(0.0, device=y_pred.device, dtype=y_pred.dtype)

	# Sort to get rankings (descending = higher is better)
	pred_order = torch.argsort(y_pred, dim=1, descending=True)
	true_order = torch.argsort(y_true.to(y_pred.dtype), dim=1, descending=True)

	total_err = torch.zeros((), device=y_pred.device, dtype=y_pred.dtype)
	for b in range(B):
		idx_pred = pred_order[b]
		idx_true = true_order[b]

		# Build inverse position maps: position of each index in the ranking
		pos_pred = torch.empty(O, device=y_pred.device, dtype=torch.long)
		pos_true = torch.empty(O, device=y_pred.device, dtype=torch.long)
		pos_pred[idx_pred] = torch.arange(O, device=y_pred.device)
		pos_true[idx_true] = torch.arange(O, device=y_pred.device)

		sample_err = torch.zeros((), device=y_pred.device, dtype=y_pred.dtype)
		# Iterate 1-based positions
		for p in range(lo, hi + 1):
			p0 = p - 1  # 0-based position
			i_pred_at_p = idx_pred[p0]
			true_pos_of_that = pos_true[i_pred_at_p].item() + 1  # to 1-based int

			i_true_at_p = idx_true[p0]
			pred_pos_of_that = pos_pred[i_true_at_p].item() + 1  # to 1-based int

			d1 = float(p - true_pos_of_that)
			d2 = float(pred_pos_of_that - p)
			sample_err = sample_err + torch.tensor(d1 * d1 + d2 * d2, device=y_pred.device, dtype=y_pred.dtype)

		# average over positions in the window
		total_err = total_err + (sample_err / float(count))

	# mean over batch
	return total_err / float(B if B > 0 else 1)


# -------------------------------
# Differentiable ranking surrogate loss
# -------------------------------
def ranking_surrogate_loss(
	y_pred: torch.Tensor,
	y_true: torch.Tensor,
	temperature: float = 1.0,
) -> torch.Tensor:
	"""Listwise surrogate loss that encourages predicted scores to match the
	ranking implied by labels via softmax distribution matching.

	loss = KL( softmax(y_true / T) || softmax(y_pred / T) )

	- Works for arbitrary real-valued labels; larger label => higher desired rank.
	- `temperature` > 0 controls sharpness of target distribution.
	- Returns a scalar mean over the batch.
	"""
	assert y_pred.ndim == 2 and y_true.ndim == 2, "Expected (B, O) tensors"
	T = max(1e-6, float(temperature))
	# Compute distributions per sample
	log_q = F.log_softmax(y_pred / T, dim=1)
	p = F.softmax(y_true.to(y_pred.dtype) / T, dim=1)
	# KL(p || q) = sum p * (log p - log q)
	# F.kl_div expects input=log_q, target=p, reduction='batchmean' for mean over batch
	kl = F.kl_div(log_q, p, reduction='batchmean')
	return kl



def ranking_score(
	predicted_order: Iterable,
	true_order: Iterable,
) -> float:
	"""Compute the ranking score described in the issue using log-base-2 discounts.

	Definitions (let N be the list length):
	  - Let ``true_order`` be the ideal ordering (best first), containing the same items as ``predicted_order``.
	  - For the item placed at 1-based position ``p`` in ``predicted_order``, let ``t0`` be its 0-based index in ``true_order``.
	  - Its gain is ``N - t0``.
	  - The discount (denominator) is:
			1                    if p == 1
			log2(p)              if p >= 2
	  - The total score is the sum of gain/discount across positions p=1..N.

	This matches the examples in the description where the maximum score is:
		N + (N-1)/log2(2) + (N-2)/log2(3) + ...

	Returns the unnormalized score (higher is better).
	"""
	import math

	pred = list(predicted_order)
	truth = list(true_order)
	if len(pred) != len(truth):
		raise ValueError("predicted_order and true_order must have the same length")
	N = len(pred)
	if N == 0:
		return 0.0

	# Build map from item -> 0-based true position
	pos_true = {item: i for i, item in enumerate(truth)}

	score = 0.0
	for p1, item in enumerate(pred, start=1):  # p1 is 1-based position
		if item not in pos_true:
			raise ValueError("All items in predicted_order must exist in true_order")
		t0 = pos_true[item]  # 0-based true position
		gain = float(N - t0)
		denom = 1.0 if p1 == 1 else math.log2(float(p1))
		score += (gain / denom)
	return score


def ranking_max_score(list_length: int) -> float:
	"""Maximum possible score for a list of given length using the same formula.

	For ideal ordering, the item with true position t0 (0-based) is placed at p = t0 + 1, so the score is:
		sum_{t0=0..N-1} (N - t0) / denom(p)   where denom(1)=1 and denom(p>=2)=log2(p)
	Which expands to: N + (N-1)/log2(2) + (N-2)/log2(3) + ...
	"""
	import math

	N = int(list_length)
	if N <= 0:
		return 0.0
	total = float(N)  # p = 1 term (N - 0) / 1
	for p in range(2, N + 1):
		gain = float(N - (p - 1))  # N - t0, where t0 = p-1
		total += gain / math.log2(float(p))
	return total


def ranking_error(
	predicted_order: Iterable,
	true_order: Iterable,
) -> float:
	"""Return a normalized error in [0, 1] where 0 is perfect ranking and 1 is worst.

	Error = 1 - (score / max_score).
	If ``max_score`` is 0 (degenerate N=0), returns 0.0.
	"""
	s = ranking_score(predicted_order, true_order)
	m = ranking_max_score(len(list(true_order)))
	return 0.0 if m == 0.0 else max(0.0, min(1.0, 1.0 - (s / m)))


# -------------------------------
# New single-argument API (integers only)
# -------------------------------

def _positions_from_orders(predicted_order: Iterable, true_order: Iterable) -> List[int]:
	"""Build a positions array from two orders (internal helper).

	Returns a list `pos` of length N, where `pos[p-1]` is the 1-based true position
	of the item placed at predicted position `p`.
	"""
	pred = list(predicted_order)
	truth = list(true_order)
	if len(pred) != len(truth):
		raise ValueError("predicted_order and true_order must have the same length")
	N = len(pred)
	if N == 0:
		return []

	# Map item -> 0-based index in true order
	pos_true = {item: i for i, item in enumerate(truth)}

	positions: List[int] = []
	for item in pred:
		if item not in pos_true:
			raise ValueError("All items in predicted_order must exist in true_order")
		t0 = pos_true[item]  # 0-based
		positions.append(t0 + 1)  # store 1-based true position
	return positions


def ranking_score_from_positions(positions: List[int]) -> float:
	"""Compute ranking score from a single integer list `positions`.

	Input contract:
	  - `positions` is a permutation of integers 1..N (N = len(positions)).
	  - `positions[p-1]` indicates the true 1-based rank of the item placed at
		predicted position `p` (1 is top/best).

	Scoring:
	  - Let t0 = positions[p-1] - 1 (0-based true position).
	  - gain = N - t0
	  - discount = 1 if p==1 else log2(p)
	  - score = sum_p gain/discount
	"""
	import math
	# Fast vectorized implementation using NumPy, with validation
	N = int(len(positions))
	if N == 0:
		return 0.0

	# Convert once to numpy for speed
	pos_arr = np.asarray(positions, dtype=np.int64)

	# Validate permutation of 1..N (O(N))
	# Uses vectorized checks to minimize Python overhead
	if pos_arr.size != N:
		raise ValueError("positions must be a permutation of integers 1..N")
	if N > 0:
		# Check bounds and uniqueness
		if pos_arr.min() < 1 or pos_arr.max() > N:
			raise ValueError("positions must be a permutation of integers 1..N")
		# Uniqueness via bincount
		counts = np.bincount(pos_arr, minlength=N + 1)
		# indices 1..N should each have count==1; index 0 must be 0
		if counts[0] != 0 or (counts[1:] != 1).any():
			raise ValueError("positions must be a permutation of integers 1..N")

	# gains: N - (positions-1)
	t0 = pos_arr - 1  # 0-based true positions
	gains = (N - t0).astype(np.float64)

	# discounts: p=1 -> 1.0, else log2(p)
	if N == 1:
		return float(gains[0])
	p = np.arange(1, N + 1, dtype=np.float64)
	discounts = np.ones_like(p)
	discounts[1:] = np.log2(p[1:])
	return float(np.sum(gains / discounts))


def ranking_error_from_positions(positions: List[int]) -> float:
	"""Normalized ranking error from a single integer list `positions`.

	Returns a value in [0, 1]: 0 is perfect (positions == [1,2,..,N]), 1 is worst.
	"""
	m = ranking_max_score(len(positions))
	if m == 0.0:
		return 0.0
	s = ranking_score_from_positions(positions)
	return max(0.0, min(1.0, 1.0 - (s / m)))


# -------------------------------
# Ranking list generation from model outputs and labels
# -------------------------------

def vector_to_ranking_indices(vec) -> List[int]:
	"""Return a ranking of indices for a 1D vector of scores.

	- Input can be: torch.Tensor (1D), numpy.ndarray (1D), or any sequence of numbers.
	- Higher score ranks earlier (descending).
	- Ties are broken deterministically by smaller index (i comes before j when scores are equal).

	Returns a list of 0-based indices in ranked order, e.g. [i, j, k, ...].
	"""
	# Fast path: use NumPy stable argsort to ensure ties broken by smaller index
	if TORCH_AVAILABLE and isinstance(vec, torch.Tensor):  # type: ignore
		if vec.ndim != 1:
			raise ValueError("vector_to_ranking_indices: expected 1D tensor")
		# Move to CPU NumPy once (existing code already moved to Python list)
		arr = vec.detach().cpu().numpy()
		# Stable sort by negative scores (desc), ties keep smaller index first
		idx = np.argsort(-arr, kind="mergesort")
		return idx.astype(np.int64).tolist()
	elif isinstance(vec, np.ndarray):
		if vec.ndim != 1:
			raise ValueError("vector_to_ranking_indices: expected 1D array")
		idx = np.argsort(-vec, kind="mergesort")
		return idx.astype(np.int64).tolist()
	else:
		# Convert generic sequence to NumPy for speed and stability
		arr = np.asarray(list(vec), dtype=np.float64)
		idx = np.argsort(-arr, kind="mergesort")
		return idx.astype(np.int64).tolist()


def matrix_to_ranking_indices(mat) -> List[List[int]]:
	"""Return rankings per row for a 2D matrix of scores.

	- Input can be: torch.Tensor (2D), numpy.ndarray (2D), or a sequence of 1D sequences.
	- For each row, higher score ranks earlier; ties broken by smaller column index.

	Returns a list of lists of 0-based indices: one ranking per row.
	"""
	# Batch implementation using NumPy stable argsort along axis=1
	if TORCH_AVAILABLE and isinstance(mat, torch.Tensor):  # type: ignore
		if mat.ndim != 2:
			raise ValueError("matrix_to_ranking_indices: expected 2D tensor")
		arr = mat.detach().cpu().numpy()
	elif isinstance(mat, np.ndarray):
		if mat.ndim != 2:
			raise ValueError("matrix_to_ranking_indices: expected 2D array")
		arr = mat
	else:
		arr = np.asarray([list(row) for row in mat], dtype=np.float64)

	idx = np.argsort(-arr, axis=1, kind="mergesort")
	return idx.astype(np.int64).tolist()


def positions_from_prediction_and_labels(pred_scores, true_labels) -> List[int]:
	"""Build the integer `positions` list from prediction and label vectors.

	Contract (per sample):
	  - `pred_scores`: 1D scores (higher is better) from the model for each output node.
	  - `true_labels`: 1D ground-truth scores/labels for the same outputs.

	Returns:
	  - `positions` list of length N where `positions[p-1]` is the 1-based true
		rank of the item placed at predicted position `p`.

	This output is directly consumable by `ranking_score_from_positions` and
	`ranking_error_from_positions`.
	"""
	# Produce 0-based index rankings for predictions and labels
	pred_order = vector_to_ranking_indices(pred_scores)
	true_order = vector_to_ranking_indices(true_labels)

	# Fast path when using integer index lists 0..N-1 (which vector_to_ranking_indices returns)
	N = len(pred_order)
	if N == 0:
		return []
	# Convert to NumPy for vectorized inverse mapping
	pred_idx = np.asarray(pred_order, dtype=np.int64)
	true_idx = np.asarray(true_order, dtype=np.int64)
	# Validate same length
	if pred_idx.shape[0] != true_idx.shape[0]:
		raise ValueError("positions_from_prediction_and_labels: predicted and label lengths differ")
	# Build inverse rank mapping for true order: item -> true 0-based position
	inv_true = np.empty(N, dtype=np.int64)
	inv_true[true_idx] = np.arange(N, dtype=np.int64)
	# positions[p-1] = 1 + true_rank_of_item_at_pred_pos
	positions = (inv_true[pred_idx] + 1).astype(np.int64)
	return positions.tolist()


def batch_positions_from_prediction_and_labels(pred_batch, true_batch) -> List[List[int]]:
	"""Batch version of `positions_from_prediction_and_labels`.

	- `pred_batch` and `true_batch` are 2D tensors/arrays/sequences with shape (B, O).
	- Returns a list of `positions` lists, one per batch element.
	"""
	# Vectorized batch computation using NumPy
	# Convert inputs to NumPy 2D arrays on CPU
	if TORCH_AVAILABLE and isinstance(pred_batch, torch.Tensor):  # type: ignore
		pred_arr = pred_batch.detach().cpu().numpy()
	elif isinstance(pred_batch, np.ndarray):
		pred_arr = pred_batch
	else:
		pred_arr = np.asarray([list(row) for row in pred_batch], dtype=np.float64)

	if TORCH_AVAILABLE and isinstance(true_batch, torch.Tensor):  # type: ignore
		true_arr = true_batch.detach().cpu().numpy()
	elif isinstance(true_batch, np.ndarray):
		true_arr = true_batch
	else:
		true_arr = np.asarray([list(row) for row in true_batch], dtype=np.float64)

	if pred_arr.shape != true_arr.shape:
		raise ValueError("batch_positions_from_prediction_and_labels: shapes differ")

	# Get per-row rankings (0-based indices) with stable sort
	pred_ord = np.argsort(-pred_arr, axis=1, kind="mergesort")
	true_ord = np.argsort(-true_arr, axis=1, kind="mergesort")

	B, N = pred_ord.shape
	if B == 0 or N == 0:
		return [[] for _ in range(B)]

	# Build inverse mapping of true ranks per row: for each row r, inv_true_r[item] = true 0-based rank
	inv_true = np.empty_like(true_ord)
	# Use advanced indexing to fill inverse ranks efficiently
	row_idx = np.arange(B)[:, None]
	inv_true[row_idx, true_ord] = np.arange(N)[None, :]

	positions = (inv_true[row_idx, pred_ord] + 1).astype(np.int64)
	return positions.tolist()



def eval_epoch(
	model: nn.Module,
	batch_iter: Iterable[Tuple[torch.Tensor, torch.Tensor]],
	device: torch.device,
	amp_enabled: bool = False,
	amp_dtype: Optional[torch.dtype] = None,
	rank_lower: int = 1,
	rank_upper: int = 10,
	compute_rank_err: bool = True,
) -> Tuple[float, float, float, float, float]:
	model.eval()
	total = 0
	mse_sum = 0.0
	top10_sum = 0.0
	bot10_sum = 0.0
	# Use autocast for 16-bit/bfloat16 inference on CUDA when enabled
	amp_cm = (
		torch.amp.autocast('cuda', dtype=amp_dtype) if (amp_enabled and device.type == "cuda") else nullcontext()  # type: ignore[attr-defined]
	)
	rde_sum = 0.0
	# Ranking error (normalized 0..1, lower is better)
	rank_err_sum = 0.0
	with torch.no_grad():
		with amp_cm:
			for xb, yb in batch_iter:
				xb = torch.nan_to_num(xb.to(device), nan=0.0, posinf=0.0, neginf=0.0)
				yb = torch.nan_to_num(yb.to(device), nan=0.0, posinf=0.0, neginf=0.0)
				pred = model(xb)
				pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
				# ensure label dtype matches prediction to avoid dtype upcasting
				mse = torch.mean((pred - yb.to(pred.dtype)) ** 2)
				top10 = mse_restricted(pred, yb.to(pred.dtype), mode="top", k=10)
				bot10 = mse_restricted(pred, yb.to(pred.dtype), mode="bottom", k=10)
				# rank displacement over configured window
				rde = rank_range_displacement(pred, yb.to(pred.dtype), rank_lower, rank_upper)
				# normalized ranking error using positions (our new measure)
				if compute_rank_err:
					try:
						pos_list = batch_positions_from_prediction_and_labels(pred.detach().cpu(), yb.detach().cpu())
						# average error across the batch
						batch_rank_err = float(np.mean([ranking_error_from_positions(pos) for pos in pos_list])) if len(pos_list) > 0 else 0.0
					except Exception:
						batch_rank_err = 0.0
				else:
					batch_rank_err = 0.0
				bs = xb.size(0)
				total += bs
				mse_sum += mse.item() * bs
				top10_sum += top10.item() * bs
				bot10_sum += bot10.item() * bs
				rde_sum += rde.item() * bs
				rank_err_sum += batch_rank_err * bs
	denom = max(1, total)
	rde_avg = rde_sum / denom
	rank_err_avg = rank_err_sum / denom
	return mse_sum / denom, top10_sum / denom, bot10_sum / denom, rde_avg, rank_err_avg


def main(argv: Optional[List[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Train a PyTorch dense network on stock instances.")
	parser.add_argument("--mode", type=str, default="train", choices=["train", "memest"], help="Run mode: train or memest (memory estimate only)")
	# Accept single int or inclusive range like "2-4" for multiple runs
	parser.add_argument("--prior-days", type=str, default="0", help="How many prior days to include in the feature window (>=0) or a range like '2-4' to run multiple")
	parser.add_argument("--epochs", type=int, default=10, help="Number of training epochs")
	parser.add_argument("--batch-size", type=int, default=32, help="Mini-batch size")
	parser.add_argument("--lr", type=float, default=1e-3, help="Learning rate for Adam")
	parser.add_argument("--metrics-file", type=str, default=os.path.join("data", "training_metrics.csv"), help="Path to CSV for epoch metrics")
	parser.add_argument("--device", type=str, default="auto", choices=["auto", "cpu", "cuda"], help="Device selection")
	parser.add_argument("--limit", type=int, default=None, help="Optional limit of DB rows during load (for debugging)")
	parser.add_argument("--hidden-layers", type=int, default=0, help="Hidden layer width for MLP (defaults to input size if <=0)")
	# Accept single int or inclusive range like "2-4" for multiple runs
	parser.add_argument("--mlp-layers", type=str, default="3", help="Total number of Linear layers in the MLP (including the output layer), or a range like '2-4'")
	parser.add_argument("--log-interval", type=int, default=50, help="How many batches between progress prints during training (0 to disable)")
	parser.add_argument("--close-ratio-only", action="store_true", help="Use only the close_ratio_to_prior as the single input feature per ticker (smaller feature vector)")
	# Rank displacement metric window
	parser.add_argument("--rank-lower", type=int, default=1, help="Lower bound (inclusive, 1-based) of rank window for displacement error metric")
	parser.add_argument("--rank-upper", type=int, default=10, help="Upper bound (inclusive, 1-based) of rank window for displacement error metric")
	# Ranking loss switching (metric is always logged)
	parser.add_argument("--use-ranking-error-loss", action="store_true", help="Use differentiable ranking surrogate loss (softmax KL) instead of MSE for backprop")
	parser.add_argument("--rank-temp", type=float, default=1.0, help="Temperature for ranking surrogate loss softmax (T>0, higher is softer)")
	# Ability to disable rank error calculation (saves CPU during training/eval)
	parser.add_argument(
		"--disable-rank-err",
		action="store_true",
		help="Disable rank error calculation and logging (sets values to 0.0)",
	)
	parser.add_argument(
		"--split-mode",
		type=str,
		choices=["sequential", "random"],
		default="sequential",
		help="How to split train/val/test: sequential (by time/index) or random (shuffled indices)",
	)
	parser.add_argument(
		"--seed",
		type=int,
		default=None,
		help="Random seed used when --split-mode random is selected (optional)",
	)
	parser.add_argument(
		"--precision",
		type=str,
		choices=["auto", "fp32", "fp16", "bf16"],
		default="auto",
		help="Math precision for training/inference: auto (CUDA->fp16 else fp32), fp32, fp16, or bf16",
	)
	parser.add_argument(
		"--arch",
		type=str,
		default="conv_trans",
		choices=["conv_trans", "mlp"],
		help="Model architecture: conv_trans (Conv+Transformer) or mlp (configurable-layer MLP)",
	)
	parser.add_argument(
		"--activation",
		type=str,
		default="relu",
		choices=["relu", "gelu", "tanh", "sigmoid", "silu", "swish", "leaky_relu", "elu"],
		help="Activation function for MLP hidden layers (ignored for conv_trans)",
	)
	parser.add_argument(
		"--day-change-centered",
		type=str,
		default="one",
		choices=["one", "half", "zero"],
		help="Center of next_day_change_in_price ratio",
	)
	# DataLoader and shuffling options
	parser.add_argument(
		"--use-dataloader",
		action="store_true",
		help="Use torch DataLoader for batching (enables workers, pin_memory, and optional shuffling)",
	)
	# Optional post-training simulation
	parser.add_argument(
		"--simulate-after",
		action="store_true",
		help="After saving the trained model, run simulate_trading on it",
	)
	parser.add_argument(
		"--simulate-percent",
		type=float,
		default=10.0,
		help="Percent of dates to simulate in simulate_trading (last N percent)",
	)
	parser.add_argument(
		"--simulate-top-k",
		type=str,
		default="10",
		help=(
			"Top-K tickers to buy per day for simulate_trading. Accepts a single int (e.g., 10) "
			"or a comma-separated list (e.g., 3,5,10)."
		),
	)
	parser.add_argument(
		"--simulate-sp500-id",
		type=int,
		default=None,
		help="Optional ticker id to use as S&P500 benchmark during simulation",
	)
	parser.add_argument(
		"--simulate-prior-only",
		action="store_true",
		help="When set, simulation builds features strictly from prior days [d-1..d-N] if available",
	)
	parser.add_argument(
		"--shuffle",
		action="store_true",
		help="Shuffle training samples each epoch (only when --use-dataloader). Default off",
	)
	parser.add_argument(
		"--num-workers",
		type=int,
		default=0,
		help="Number of DataLoader workers (only when --use-dataloader)",
	)
	parser.add_argument(
		"--pin-memory",
		action="store_true",
		help="Enable DataLoader pin_memory for faster host->GPU transfer (only when --use-dataloader)",
	)
	args = parser.parse_args(argv)

	# Helper to parse single int or inclusive range "a-b" into a list of ints
	def _parse_int_or_range(val: str) -> List[int]:
		try:
			return [int(val)]
		except Exception:
			pass
		if isinstance(val, str) and "-" in val:
			parts = val.split("-", 1)
			try:
				a = int(parts[0].strip())
				b = int(parts[1].strip())
			except Exception:
				raise argparse.ArgumentTypeError(f"Invalid range value: {val}")
			if b < a:
				a, b = b, a
			# constrain to non-negative
			a = max(0, a)
			b = max(0, b)
		return list(range(a, b + 1))
		raise argparse.ArgumentTypeError(f"Invalid integer or range value: {val}")

	# Helper to parse simulate top-k which may be a single int or comma-separated list
	def _parse_topk(val) -> List[int]:
		# Already an int
		try:
			if isinstance(val, int):
				v = int(val)
				return [v]
		except Exception:
			pass
		s = str(val).strip() if val is not None else "10"
		if "," in s:
			parts = [p.strip() for p in s.split(",") if p.strip()]
			out: List[int] = []
			for p in parts:
				try:
					out.append(int(p))
				except Exception:
					raise argparse.ArgumentTypeError(f"Invalid --simulate-top-k entry: '{p}'")
			if not out:
				return [10]
			return out
		# Single value path
		try:
			return [int(s)]
		except Exception:
			raise argparse.ArgumentTypeError(f"Invalid --simulate-top-k value: '{val}'")

	prior_days_list = _parse_int_or_range(str(args.prior_days))
	mlp_layers_list = _parse_int_or_range(str(args.mlp_layers))

	# Determine if this is a single run explicitly specified without range syntax
	# We only consider it a "no-range" run when both options are single values
	# and the original argument strings did not contain a '-'. This preserves
	# prior behavior for explicit ranges like "2-4" as well as single-value lists.
	single_run_no_range = (
		len(prior_days_list) == 1
		and len(mlp_layers_list) == 1
		and ("-" not in str(args.prior_days))
		and ("-" not in str(args.mlp_layers))
	)

	# Build a timestamped run directory and place metrics inside it
	ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
	# Separate the provided metrics path into directory and filename
	metrics_dir = os.path.dirname(args.metrics_file)
	metrics_base = os.path.basename(args.metrics_file)
	if not metrics_base:
		metrics_base = "training_metrics.csv"
	# Create run subdirectory: <metrics_dir>/<timestamp>
	base_run_dir = os.path.join(metrics_dir if metrics_dir else "", ts)
	# Ensure the run directory exists now so we can write parameters file early
	os.makedirs(base_run_dir, exist_ok=True)
	# Informative prints so the user can find outputs
	print(f"Run directory: {base_run_dir}")

	# We will iterate over combinations of prior_days and mlp_layers
	# Each combination writes its own outputs with identifying suffixes

	# Support both torch.Tensor (train mode) and numpy.ndarray (memest mode)
	def _count_elements(a) -> int:
		# torch.Tensor has .numel(); numpy arrays do not
		if hasattr(a, "numel"):
			try:
				return int(a.numel())  # type: ignore[attr-defined]
			except Exception:
				pass
		# Fallback to numpy sizing
		return int(np.size(a))

	# Prepare a single provider instance up-front and reuse it across runs
	# to avoid reloading data from the database for each configuration.
	provider = TrainingInstanceProvider(
		use_torch=(args.mode == "train" and TORCH_AVAILABLE),
		limit=args.limit,
		close_ratio_only=bool(args.close_ratio_only),
		day_change_centered=args.day_change_centered,
	)

	# Define a function to run one configuration using the shared provider
	def _run_one(prior_days: int, mlp_layers: int) -> None:
		# Set the sliding window size for this configuration
		provider.set_how_many_days_in_past_to_use(int(prior_days))

		max_inst = provider.get_maximum_number_of_test_train_instances()
		if max_inst <= 1:
			raise RuntimeError("Not enough instances to train. Consider reducing --prior-days or loading more data.")

		# Determine input/output sizes from the first instance
		x0, y0 = provider.get_feature_and_label_pair(1)
		input_size = _count_elements(x0)
		output_size = _count_elements(y0)

		# Compute split indices (1-based)
		n = max_inst
		if args.split_mode == "sequential":
			# Deterministic by index order: 75/15/10
			train_end = int(math.floor(0.75 * n))
			val_end = int(math.floor(0.90 * n))
			train_ds = RangeDataset(provider, 1, max(1, train_end))
			val_start = max(1, train_end + 1)
			val_ds = RangeDataset(provider, val_start, max(val_start, val_end))
			test_start = max(val_end + 1, 1)
			test_ds = RangeDataset(provider, test_start, n)
		else:
			# Randomized split: shuffle indices then assign 75/15/10
			indices = list(range(1, n + 1))
			if args.seed is not None:
				rng = random.Random(int(args.seed))
				rng.shuffle(indices)
			else:
				random.shuffle(indices)
			train_n = max(1, int(math.floor(0.75 * n)))
			val_n = int(math.floor(0.15 * n))
			# Ensure we don't exceed n
			train_n = min(train_n, n)
			val_n = min(val_n, max(0, n - train_n))
			test_n = max(0, n - train_n - val_n)
			train_idx = indices[:train_n]
			val_idx = indices[train_n:train_n + val_n]
			test_idx = indices[train_n + val_n:train_n + val_n + test_n]
			train_ds = IndexDataset(provider, train_idx)
			val_ds = IndexDataset(provider, val_idx)
			test_ds = IndexDataset(provider, test_idx)

		# Build output directory
		# If no range was provided (single-run), write directly under the timestamp
		# directory. Otherwise, create a per-config subdirectory to avoid collisions.
		suffix = f"prior{prior_days}_layers{mlp_layers}"
		if single_run_no_range:
			run_dir = base_run_dir
		else:
			run_dir = os.path.join(base_run_dir, suffix)
		os.makedirs(run_dir, exist_ok=True)
		metrics_file = os.path.join(run_dir, f"{suffix}_" + metrics_base)

		# On first setup of the training split for this configuration, write out
		# label_limits.txt with per-date min/max of the label vector observed in the
		# training set. This is deterministic given the split, so we can compute it
		# once up-front instead of during the epoch loop.
		try:
			label_limits_path = os.path.join(run_dir, "label_limits.txt")
			if not os.path.exists(label_limits_path):
				# Build the list of 1-based instance indices that belong to the training set
				if isinstance(train_ds, RangeDataset):
					train_indices = list(range(int(train_ds.start_idx), int(train_ds.end_idx) + 1))
				else:
					# IndexDataset
					train_indices = list(getattr(train_ds, "indices", []))

				# Compute per-date min/max from provider cached arrays
				rows: List[Tuple[int, float, float]] = []
				for i in train_indices:
					# Map 1-based instance index to absolute date index `d` used inside provider
					d = int(provider._num_prior + (int(i) - 1))
					if d < 0 or d >= int(provider._D):
						continue
					date_id = int(provider._dates[d])
					vec = provider._Y[d]
					# Guard against NaNs/Infs: restrict to finite values
					if hasattr(np, "isfinite"):
						mask = np.isfinite(vec)
						if mask.any():
							vmin = float(np.min(vec[mask]))
							vmax = float(np.max(vec[mask]))
						else:
							vmin = 0.0
							vmax = 0.0
					else:
						# Fallback: use plain min/max (may propagate NaN)
						vmin = float(np.min(vec))
						vmax = float(np.max(vec))
					rows.append((date_id, vmin, vmax))

				# De-duplicate in case indices accidentally contain repeats
				seen = set()
				unique_rows: List[Tuple[int, float, float]] = []
				for dt, lo, hi in rows:
					if dt not in seen:
						seen.add(dt)
						unique_rows.append((dt, lo, hi))

				# Sort by date_id ascending for readability
				unique_rows.sort(key=lambda t: t[0])

				with open(label_limits_path, mode="w", encoding="utf-8", newline="") as lf:
					lf.write("date_id,min_label,max_label\n")
					for dt, lo, hi in unique_rows:
						lf.write(f"{dt},{lo:.10f},{hi:.10f}\n")
		except Exception as e:
			print(f"Warning: failed to write label_limits.txt: {e}")

		# Write run parameters for traceability (per-config)
		sim_topks_resolved = _parse_topk(getattr(args, "simulate_top_k", "10"))
		try:
			params_path = os.path.join(run_dir, "runParameters.txt")
			with open(params_path, mode="w", encoding="utf-8") as pf:
				pf.write("Run Timestamp: " + ts + "\n")
				pf.write("Parameters:\n")
				for k, v in sorted(vars(args).items()):
					pf.write(f"  {k}: {v}\n")
				pf.write(f"  resolved_prior_days: {prior_days}\n")
				pf.write(f"  resolved_mlp_layers: {mlp_layers}\n")
				pf.write(f"  resolved_simulate_top_k: {sim_topks_resolved}\n")
		except Exception as e:
			print(f"Warning: failed to write runParameters.txt: {e}")

		# Helper: human-readable bytes (local to function)
		def fmt_bytes(num_bytes: float) -> str:
			units = ["B", "KB", "MB", "GB", "TB"]
			n = float(num_bytes)
			i = 0
			while n >= 1024.0 and i < len(units) - 1:
				n /= 1024.0
				i += 1
			return f"{n:.2f} {units[i]}"

		# Device selection (only needed for train mode)
		if args.mode == "train":
			print(f"Mode:Train")
			if not TORCH_AVAILABLE:
				raise RuntimeError("PyTorch is required for training. Please install a CUDA-enabled PyTorch build.")
			# Ensure CUDA allocator uses expandable segments before any CUDA initialization
			try:
				conf_key = "PYTORCH_ALLOC_CONF"
				new_part = "expandable_segments:True"
				prev_conf = os.environ.get(conf_key)
				if not prev_conf:
					os.environ[conf_key] = new_part
				elif "expandable_segments" not in prev_conf:
					os.environ[conf_key] = f"{prev_conf},{new_part}"
				print(f"CUDA alloc conf: {conf_key}={os.environ.get(conf_key)}")
			except Exception:
				pass
			if args.device == "auto":
				device = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # type: ignore[attr-defined]
			else:
				device = torch.device("cuda" if args.device == "cuda" else "cpu")  # type: ignore[attr-defined]

			requested_precision = (args.precision or "auto").lower()
			if requested_precision not in ("auto", "fp32", "fp16", "bf16"):
				raised_prec = "auto"
			else:
				raised_prec = requested_precision
			if raised_prec == "auto":
				eff_precision = "fp16" if device.type == "cuda" else "fp32"  # type: ignore[union-attr]
			else:
				eff_precision = raised_prec
			amp_enabled = (device.type == "cuda") and (eff_precision in ("fp16", "bf16"))  # type: ignore[union-attr]
			amp_dtype = None
			if amp_enabled:
				amp_dtype = torch.float16 if eff_precision == "fp16" else torch.bfloat16
			print(f"Selected device: {device.type.upper()} ({torch.cuda.get_device_name(device) if device.type == 'cuda' else 'CPU'})")  # type: ignore[union-attr]
			print(f"Selected precision: {eff_precision.upper()} | autocast={'ON' if amp_enabled else 'OFF'}")

			feature_mode = "close_ratio_only" if args.close_ratio_only else "full_feature_set"
			print(f"Config: prior_days={prior_days} epochs={args.epochs} batch_size={args.batch_size} lr={args.lr} feature_mode={feature_mode} mlp_layers={mlp_layers} split_mode={args.split_mode} seed={args.seed} use_dataloader={bool(args.use_dataloader)} shuffle={bool(args.shuffle)} workers={int(args.num_workers)} pin_memory={bool(args.pin_memory)}")
			print(f"Instances: total={n} | splits -> train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}")
			print(f"Tensor sizes: input_size={input_size} output_size={output_size}")
			device_name = torch.cuda.get_device_name(device) if device.type == "cuda" else "CPU"  # type: ignore[union-attr]
			print(f"Selected device: {device.type.upper()} ({device_name})")  # type: ignore[union-attr]

		# If only estimating memory, do calculations and exit early (no CUDA requirement)
		if args.mode == "memest":
			print(f"Mode:Memest")
			B = int(args.batch_size)
			S = int(input_size)
			channels = 16
			ks = 15
			nhead = 4 if channels % 4 == 0 else 1
			dim_ff = max(64, channels * 4)
			conv1_params = (1 * channels * ks) + channels
			conv2_params = (channels * channels * ks) + channels
			conv3_params = (channels * channels * ks) + channels
			proj_params = (channels * output_size * 1) + output_size
			attn_params_per = (3 * channels * channels) + (3 * channels) + (channels * channels) + channels
			ff_params_per = (channels * dim_ff + dim_ff) + (dim_ff * channels + channels)
			ln_params_per = 4 * channels
			trans_params_per = attn_params_per + ff_params_per + ln_params_per
			trans_total = 2 * trans_params_per
			param_count = conv1_params + conv2_params + conv3_params + proj_params + trans_total
			param_bytes = float(param_count) * 4.0
			grad_bytes = param_bytes
			adam_state_bytes = param_bytes * 2.0
			activ_elems = B * (5 * channels * S + output_size * S)
			activations_bytes = float(activ_elems) * 4.0
			activations_peak_train_bytes = activations_bytes * 2.0
			batch_input_bytes = B * S * 4.0
			batch_target_bytes = B * output_size * 4.0
			D = int(provider._D)
			TF = int(provider._TF)
			T = int(provider._T)
			dates_bytes = provider._dates.size * 4.0
			X_bytes = D * TF * 8.0
			Y_bytes = D * T * 8.0
			provider_ram_bytes = dates_bytes + X_bytes + Y_bytes
			n_train = len(train_ds)
			materialized_train_feats_bytes = n_train * input_size * 4.0
			materialized_train_labels_bytes = n_train * output_size * 4.0
			materialized_train_total = materialized_train_feats_bytes + materialized_train_labels_bytes
			model_bytes_total = param_bytes + grad_bytes + adam_state_bytes
			train_peak_bytes = model_bytes_total + activations_peak_train_bytes + batch_input_bytes + batch_target_bytes
			infer_peak_bytes = param_bytes + batch_input_bytes + batch_target_bytes + activations_bytes
			print("Memory estimate (approximate):")
			print(f"  Input size (sequence length): {S} | Output size: {output_size}")
			print(f"  Instances: total={n} train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}")
			print("\nModel state (float32):")
			print(f"  Parameters: {param_count:,} -> {fmt_bytes(param_bytes)}")
			print(f"  Gradients: ~{fmt_bytes(grad_bytes)}")
			print(f"  Adam states (m,v): ~{fmt_bytes(adam_state_bytes)}")
			print(f"  Model total (params + grads + Adam): ~{fmt_bytes(model_bytes_total)}")
			print("\nPer-batch (batch_size = %d):" % B)
			print(f"  Inputs: {fmt_bytes(batch_input_bytes)} | Targets: {fmt_bytes(batch_target_bytes)}")
			print(f"  Activations (forward): ~{fmt_bytes(activations_bytes)}")
			print(f"  Training peak (model + activations*2 + batch): ~{fmt_bytes(train_peak_bytes)}")
			print(f"  Inference peak (model + activations + batch): ~{fmt_bytes(infer_peak_bytes)}")
			print("\nProvider cached arrays (host RAM):")
			print(f"  dates int32: {fmt_bytes(dates_bytes)} | X_flat float64: {fmt_bytes(X_bytes)} | Y float64: {fmt_bytes(Y_bytes)}")
			print(f"  Provider total: ~{fmt_bytes(provider_ram_bytes)}")
			print("\nIf you fully materialize all TRAIN features/labels as float32 tensors (not required by script):")
			print(f"  Train features total: {fmt_bytes(materialized_train_feats_bytes)}")
			print(f"  Train labels total:   {fmt_bytes(materialized_train_labels_bytes)}")
			print(f"  Materialized train total: {fmt_bytes(materialized_train_total)}")
			return

		# Enforce GPU usage: throw an error if not using CUDA (train mode only)
		if args.mode == "train":
			if device.type != "cuda":  # type: ignore[union-attr]
				if not torch.cuda.is_available():  # type: ignore[union-attr]
					raise RuntimeError(
						"CUDA GPU is required for training, but torch.cuda.is_available() is False. "
						"Install a CUDA-enabled PyTorch build and ensure a compatible GPU/driver is present."
					)
				else:
					raise RuntimeError(
						"CUDA GPU is required for training, but a non-CUDA device was selected. "
						"Please run with --device cuda."
					)
		# Model, loss, optimizer (train mode only)
		if args.mode == "train":
			if make_model is None:
				raise RuntimeError("Model factory not available. Ensure the 'models' package exists and is importable.")
			model = make_model(
				args.arch,
				input_size,
				output_size,
				args.hidden_layers,
				activation=(args.activation if args.arch == "mlp" else None),
				num_layers=mlp_layers,
			).to(device)  # type: ignore[arg-type]
			optimizer = optim.Adam(model.parameters(), lr=float(args.lr))  # type: ignore[attr-defined]
			use_amp = amp_enabled
			scaler = torch.amp.GradScaler('cuda', enabled=(use_amp and amp_dtype == torch.float16))  # type: ignore[attr-defined]
			if use_amp and amp_dtype == torch.float16:
				print("Precision runtime: autocast=float16 with GradScaler")
			elif use_amp and amp_dtype == torch.bfloat16:
				print("Precision runtime: autocast=bfloat16 (no GradScaler)")
			else:
				print("Precision runtime: float32 (no autocast)")
			param_count = sum(p.numel() for p in model.parameters())
			print(f"Model parameters: {param_count:,}")

		# Ensure metrics directory exists
		os.makedirs(os.path.dirname(metrics_file), exist_ok=True)

		# Build batch providers (DataLoader or simple generator)
		use_dl = (args.mode == "train") and bool(args.use_dataloader) and TORCH_AVAILABLE
		if use_dl:
			# DataLoader-based batching
			train_loader = DataLoader(
				train_ds,
				batch_size=args.batch_size,
				shuffle=bool(args.shuffle),
				drop_last=False,
				num_workers=int(args.num_workers),
				pin_memory=bool(args.pin_memory) and (device.type == 'cuda'),  # type: ignore[union-attr]
			)
			val_loader = DataLoader(
				val_ds,
				batch_size=args.batch_size,
				shuffle=False,
				drop_last=False,
				num_workers=int(args.num_workers),
				pin_memory=bool(args.pin_memory) and (device.type == 'cuda'),  # type: ignore[union-attr]
			)
			test_loader = DataLoader(
				test_ds,
				batch_size=args.batch_size,
				shuffle=False,
				drop_last=False,
				num_workers=int(args.num_workers),
				pin_memory=bool(args.pin_memory) and (device.type == 'cuda'),  # type: ignore[union-attr]
			)
		else:
			train_loader = None  # type: ignore[assignment]
			val_loader = None  # type: ignore[assignment]
			test_loader = None  # type: ignore[assignment]

		# Open CSV and write header
		with open(metrics_file, mode="w", newline="", encoding="utf-8") as f:
			writer = csv.writer(f)
			header = [
				"epoch",
				"train_mse",
				"val_mse",
				"train_top10_mse",
				"val_top10_mse",
				"train_bottom10_mse",
				"val_bottom10_mse",
				"train_rank_disp",
				"val_rank_disp",
				"train_rank_err",
				"val_rank_err",
			]
			writer.writerow(header)

			# Training loop
			if args.mode == "train":
				best_val_mse = float("inf")
				for epoch in range(1, args.epochs + 1):
					print(f"\n=== Starting epoch {epoch}/{args.epochs} ===")
					epoch_start = time.time()
					model.train()
					total = 0
					train_loss_sum = 0.0
					train_top10_sum = 0.0
					train_bot10_sum = 0.0
					train_rde_sum = 0.0
					train_rank_err_sum = 0.0

					batch_idx = 0
					if use_dl:
						train_iter = train_loader  # iterate DataLoader anew each epoch
						total_batches = len(train_loader) if hasattr(train_loader, "__len__") else 0  # type: ignore[arg-type]
					else:
						train_iter = batched(iter(train_ds), args.batch_size)
						total_batches = (len(train_ds) + args.batch_size - 1) // args.batch_size
					for xb, yb in train_iter:
						xb = xb.to(device)
						yb = yb.to(device)
						optimizer.zero_grad(set_to_none=True)
						if use_amp:
							with torch.amp.autocast('cuda', dtype=amp_dtype):  # type: ignore[attr-defined]
								xb = torch.nan_to_num(xb, nan=0.0, posinf=0.0, neginf=0.0)
								yb = torch.nan_to_num(yb, nan=0.0, posinf=0.0, neginf=0.0)
								pred = model(xb)
								pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
								if bool(args.use_ranking_error_loss):
									loss = ranking_surrogate_loss(pred, yb.to(pred.dtype), temperature=float(args.rank_temp))
								else:
									loss = torch.mean((pred - yb.to(pred.dtype)) ** 2)
							if not torch.isfinite(loss):
								optimizer.zero_grad(set_to_none=True)
								batch_idx += 1
								continue
							elif amp_dtype == torch.float16:
								scaler.scale(loss).backward()
								scaler.step(optimizer)
								scaler.update()
							else:
								loss.backward()
								optimizer.step()
						else:
							xb = torch.nan_to_num(xb, nan=0.0, posinf=0.0, neginf=0.0)
							yb = torch.nan_to_num(yb, nan=0.0, posinf=0.0, neginf=0.0)
							pred = model(xb)
							pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
							if bool(args.use_ranking_error_loss):
								loss = ranking_surrogate_loss(pred, yb.to(pred.dtype), temperature=float(args.rank_temp))
							else:
								loss = torch.mean((pred - yb.to(pred.dtype)) ** 2)
							if not torch.isfinite(loss):
								optimizer.zero_grad(set_to_none=True)
								batch_idx += 1
								continue
							loss.backward()
							optimizer.step()

					bs = xb.size(0)
					total += bs
					train_loss_sum += loss.item() * bs
					train_top10_sum += mse_restricted(pred.detach(), yb.to(pred.dtype), mode="top", k=10).item() * bs
					train_bot10_sum += mse_restricted(pred.detach(), yb.to(pred.dtype), mode="bottom", k=10).item() * bs
					train_rde_sum += rank_range_displacement(pred.detach(), yb.to(pred.dtype), int(args.rank_lower), int(args.rank_upper)).item() * bs
					if not bool(args.disable_rank_err):
						try:
							pos_list = batch_positions_from_prediction_and_labels(pred.detach().cpu(), yb.detach().cpu())
							batch_rank_err = float(np.mean([ranking_error_from_positions(pos) for pos in pos_list])) if len(pos_list) > 0 else 0.0
						except Exception:
							batch_rank_err = 0.0
					else:
						batch_rank_err = 0.0
					train_rank_err_sum += batch_rank_err * bs

					batch_idx += 1
					if args.log_interval and args.log_interval > 0 and (batch_idx % args.log_interval == 0 or batch_idx == total_batches):
						avg_loss_so_far = train_loss_sum / max(1, total)
						print(f"Epoch {epoch}/{args.epochs} | batch {batch_idx}/{total_batches} | curr_loss={loss.item():.6f} | avg_loss={avg_loss_so_far:.6f}")

					train_mse = train_loss_sum / max(1, total)
					train_top10 = train_top10_sum / max(1, total)
					train_bot10 = train_bot10_sum / max(1, total)
					train_rde = train_rde_sum / max(1, total)
					train_rank_err = (train_rank_err_sum / max(1, total))

					# Build a fresh iterator for validation each epoch when not using DataLoader
					val_iter = val_loader if use_dl else batched(iter(val_ds), args.batch_size)
					val_mse, val_top10, val_bot10, val_rde, val_rank_err = eval_epoch(
						model, val_iter, device, amp_enabled=use_amp, amp_dtype=amp_dtype,
						rank_lower=int(args.rank_lower), rank_upper=int(args.rank_upper), compute_rank_err=(not bool(args.disable_rank_err))
					)

					epoch_dur = time.time() - epoch_start
					msg = (f"Epoch {epoch:03d} | train_mse={train_mse:.6f} val_mse={val_mse:.6f} "
						  f"train_top10={train_top10:.6f} val_top10={val_top10:.6f} "
						  f"train_bottom10={train_bot10:.6f} val_bottom10={val_bot10:.6f} "
						  f"train_rank_disp={train_rde:.3f} val_rank_disp={val_rde:.3f} "
						  f"train_rank_err={train_rank_err:.4f} val_rank_err={val_rank_err:.4f}"
						  + (" [rank_err:off]" if bool(args.disable_rank_err) else "")
						  + f" | time={epoch_dur:.2f}s")
					print(msg)

					row = [epoch, train_mse, val_mse, train_top10, val_top10, train_bot10, val_bot10, train_rde, val_rde, train_rank_err, val_rank_err]
					writer.writerow(row)
					f.flush()

					if epoch == 1 or val_mse <= best_val_mse:
						best_val_mse = val_mse
						print(f"New best validation MSE: {best_val_mse:.6f} (epoch {epoch})")

		# Final evaluation on test set (train mode only)
		if args.mode == "train":
			# Build a fresh iterator for test as well when not using DataLoader
			test_iter = test_loader if use_dl else batched(iter(test_ds), args.batch_size)
			test_mse, test_top10, test_bot10, test_rde, test_rank_err = eval_epoch(
				model, test_iter, device, amp_enabled=use_amp, amp_dtype=amp_dtype,
				rank_lower=int(args.rank_lower), rank_upper=int(args.rank_upper), compute_rank_err=(not bool(args.disable_rank_err))
			)
			print("Final Test Metrics:")
			print(f"test_mse={test_mse:.6f} test_top10={test_top10:.6f} test_bottom10={test_bot10:.6f} test_rank_disp={test_rde:.3f} test_rank_err={test_rank_err:.4f}")

			# Append final test metrics to a sidecar file next to metrics CSV (include suffix)
			test_metrics_path = os.path.splitext(metrics_file)[0] + "_test.csv"
			with open(test_metrics_path, mode="w", newline="", encoding="utf-8") as f_test:
				writer_test = csv.writer(f_test)
				writer_test.writerow(["metric", "value"])
				writer_test.writerow(["test_mse", test_mse])
				writer_test.writerow(["test_top10_mse", test_top10])
				writer_test.writerow(["test_bottom10_mse", test_bot10])
				writer_test.writerow(["test_rank_disp", test_rde])
				writer_test.writerow(["test_rank_err", test_rank_err])

			print(f"Wrote per-epoch metrics to: {metrics_file}")
			print(f"Wrote final test metrics to: {test_metrics_path}")

			# Save model artifacts with suffix
			try:
				model_state_path = os.path.join(run_dir, f"{suffix}_model_state.pt")
				optimizer_state_path = os.path.join(run_dir, f"{suffix}_optimizer_state.pt")
				manifest_path = os.path.join(run_dir, f"{suffix}_model_manifest.json")
				torch.save(model.state_dict(), model_state_path)
				try:
					torch.save(optimizer.state_dict(), optimizer_state_path)
				except Exception as opt_e:
					print(f"Warning: could not save optimizer state: {opt_e}")
				manifest = {
					"timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
					"arch": str(args.arch),
					"input_size": int(input_size),
					"output_size": int(output_size),
					"hidden_layers": int(args.hidden_layers),
					"mlp_layers": int(mlp_layers),
					"activation": (str(args.activation) if args.arch == "mlp" else None),
					"prior_days": int(prior_days),
					"close_ratio_only": bool(args.close_ratio_only),
					"precision": str((args.precision or "auto").lower()),
					"state_dict_path": os.path.basename(model_state_path),
					"optimizer_state_path": os.path.basename(optimizer_state_path),
					"metrics_csv": os.path.basename(metrics_file),
					"test_metrics_csv": os.path.basename(test_metrics_path),
				}
				with open(manifest_path, "w", encoding="utf-8") as mf:
					json.dump(manifest, mf, indent=2)
				print("Saved trained model and manifest:")
				print(f"  Weights: {model_state_path}")
				print(f"  Optimizer: {optimizer_state_path}")
				print(f"  Manifest: {manifest_path}")

				# Optionally run simulation using the freshly saved model
				if bool(args.simulate_after):
					try:
						# Import locally to avoid overhead when not requested
						import simulate_trading as _sim
						model_ref = manifest_path if os.path.exists(manifest_path) else run_dir
						print("Running simulate_trading on trained model...")
						out_path = _sim.simulate(
							model_path=model_ref,
							percent=float(args.simulate_percent),
							top_k=sim_topks_resolved if isinstance(sim_topks_resolved, list) and len(sim_topks_resolved) > 0 else 10,
							host=os.getenv("MYSQL_HOST", "localhost"),
							database=os.getenv("MYSQL_DB", "stockforecasting"),
							user=os.getenv("MYSQL_USER", "Redbeard"),
							password=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"),
							port=int(os.getenv("MYSQL_PORT", "3306")),
							driver="auto",
							limit=None,
							close_ratio_only=bool(args.close_ratio_only),
							sp500_ticker_id=(int(args.simulate_sp500_id) if args.simulate_sp500_id is not None else None),
							prefer_prior_only=bool(args.simulate_prior_only),
						)
						print(f"Simulation complete. Results written to: {out_path}")
					except Exception as sim_e:
						print(f"Warning: simulate_trading failed: {sim_e}")
			except Exception as save_e:
				print(f"Warning: failed to save model artifacts: {save_e}")

	# Execute runs for all combinations
	for pd in prior_days_list:
		for ml in mlp_layers_list:
			_run_one(int(pd), int(ml))
	return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
	raise SystemExit(main())
