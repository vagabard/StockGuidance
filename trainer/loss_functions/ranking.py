from __future__ import annotations

from typing import Iterable, List
import numpy as np


def ranking_score(predicted_order: Iterable, true_order: Iterable) -> float:
	import math

	pred = list(predicted_order)
	truth = list(true_order)
	if len(pred) != len(truth):
		raise ValueError("predicted_order and true_order must have the same length")
	N = len(pred)
	if N == 0:
		return 0.0
	pos_true = {item: i for i, item in enumerate(truth)}
	score = 0.0
	for p1, item in enumerate(pred, start=1):  # 1-based predicted position
		if item not in pos_true:
			raise ValueError("All items in predicted_order must exist in true_order")
		t0 = pos_true[item]  # 0-based true position
		gain = float(N - t0)
		# Discount by TRUE position (heavier discount as true position gets lower/worse)
		true_p1 = float(t0 + 1)  # 1-based true position
		denom = 0.5 if true_p1 == 1.0 else math.log2(true_p1)
		score += (gain / denom)
	return score


def ranking_max_score(list_length: int) -> float:
	import math

	N = int(list_length)
	if N <= 0:
		return 0.0
	# Maximum achieved when predicted equals true order; discount by true position
	total = 0.0
	for p in range(1, N + 1):  # p is both predicted and true position in perfect ranking
		gain = float(N - (p - 1))
		denom = 0.5 if p == 1 else math.log2(float(p))
		total += gain / denom
	return total


def ranking_error(predicted_order: Iterable, true_order: Iterable) -> float:
	s = ranking_score(predicted_order, true_order)
	m = ranking_max_score(len(list(true_order)))
	return 0.0 if m == 0.0 else max(0.0, min(1.0, 1.0 - (s / m)))


def _positions_from_orders(predicted_order: Iterable, true_order: Iterable) -> List[int]:
	pred = list(predicted_order)
	truth = list(true_order)
	if len(pred) != len(truth):
		raise ValueError("predicted_order and true_order must have the same length")
	N = len(pred)
	if N == 0:
		return []
	pos_true = {item: i for i, item in enumerate(truth)}
	positions: List[int] = []
	for item in pred:
		if item not in pos_true:
			raise ValueError("All items in predicted_order must exist in true_order")
		t0 = pos_true[item]
		positions.append(t0 + 1)
	return positions


def ranking_score_from_positions(positions: List[int]) -> float:
	"""Compute ranking score from a single integer list `positions`.

	Input contract:
	  - `positions` is a permutation of integers 1..N (N = len(positions)).
	  - `positions[p-1]` indicates the true 1-based rank of the item placed at
		predicted position `p` (1 is top/best).

	Scoring (discount by TRUE position):
	  - Let t0 = positions[p-1] - 1 (0-based true position).
	  - gain = N - t0
	  - discount = 0.5 if (true position)==1 else log2(true position)
	  - score = sum_p gain/discount
	"""


	import math


	N = len(positions)
	if N == 0:
		return 0.0
	# Validate permutation of 1..N
	if sorted(positions) != list(range(1, N + 1)):
		raise ValueError("positions must be a permutation of 1..N")
	total = 0.0
	for p1, true_pos1 in enumerate(positions, start=1):
		t0 = int(true_pos1) - 1
		gain = float(N - t0)
		# Discount by TRUE position (not predicted)
		tp = float(true_pos1)
		denom = 0.5 if tp == 1.0 else math.log2(tp)
		total += gain / denom
	return total


def ranking_error_from_positions(positions: List[int]) -> float:
	s = ranking_score_from_positions(positions)
	m = ranking_max_score(len(positions))
	return 0.0 if m == 0.0 else max(0.0, min(1.0, 1.0 - (s / m)))


def vector_to_ranking_indices(vec) -> List[int]:
	"""Return a ranking of indices for a 1D vector of scores.

	- Input can be: torch_interface.Tensor (1D), numpy.ndarray (1D), or any sequence of numbers.
	- Higher score ranks earlier (descending).
	- Ties are broken deterministically by smaller index (i comes before j when scores are equal).

	Returns a list of 0-based indices in ranked order, e.g. [i, j, k, ...].
	"""
	arr = np.asarray(vec)
	if arr.ndim != 1:
		raise ValueError("vector_to_ranking_indices expects a 1D array-like")
	# returns 0-based indices, stable descending sort
	return np.argsort(-arr, kind="mergesort").tolist()


def matrix_to_ranking_indices(mat) -> List[List[int]]:
	arr = np.asarray(mat)
	if arr.ndim != 2:
		raise ValueError("matrix_to_ranking_indices expects a 2D array-like")
	return np.argsort(-arr, axis=1, kind="mergesort").tolist()


def positions_from_prediction_and_labels(pred_scores, true_labels) -> List[int]:
	pred_order = vector_to_ranking_indices(pred_scores)
	true_order = vector_to_ranking_indices(true_labels)
	N = len(pred_order)
	if N == 0:
		return []
	pred_idx = np.asarray(pred_order, dtype=np.int64)
	true_idx = np.asarray(true_order, dtype=np.int64)
	if pred_idx.shape[0] != true_idx.shape[0]:
		raise ValueError("positions_from_prediction_and_labels: predicted and label lengths differ")
	inv_true = np.empty(N, dtype=np.int64)
	inv_true[true_idx] = np.arange(N, dtype=np.int64)
	positions = (inv_true[pred_idx] + 1).astype(np.int64)
	return positions.tolist()


def batch_positions_from_prediction_and_labels(pred_batch, true_batch) -> List[List[int]]:
	"""Batch version of `positions_from_prediction_and_labels`.

	- `pred_batch` and `true_batch` are 2D tensors/arrays/sequences with shape (B, O).
	- Returns a list of `positions` lists, one per batch element.
	"""
	# Convert to numpy
	pred_arr = np.asarray(pred_batch)
	true_arr = np.asarray(true_batch)
	if pred_arr.shape != true_arr.shape:
		raise ValueError("batch_positions_from_prediction_and_labels: shapes differ")
	pred_ord = np.argsort(-pred_arr, axis=1, kind="mergesort")
	true_ord = np.argsort(-true_arr, axis=1, kind="mergesort")
	B, N = pred_ord.shape
	if B == 0 or N == 0:
		return [[] for _ in range(B)]
	inv_true = np.empty_like(true_ord)
	row_idx = np.arange(B)[:, None]
	inv_true[row_idx, true_ord] = np.arange(N)[None, :]
	positions = (inv_true[row_idx, pred_ord] + 1).astype(np.int64)
	return positions.tolist()


__all__ = [
	"ranking_score",
	"ranking_max_score",
	"ranking_error",
	"ranking_score_from_positions",
	"ranking_error_from_positions",
	"vector_to_ranking_indices",
	"matrix_to_ranking_indices",
	"positions_from_prediction_and_labels",
	"batch_positions_from_prediction_and_labels",
]
