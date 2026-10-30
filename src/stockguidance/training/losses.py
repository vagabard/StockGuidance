"""Typed target-space objectives and bounded ranking metrics.

The model sees one row per decision session and one output per frozen ticker.
Ranking therefore compares columns within each row; it never compares two
different dates.  Pair blocks bound the temporary ``date x ticker x ticker``
work while the accumulator keeps the exact dense weighting.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt
from typing import TYPE_CHECKING

from stockguidance.training.configuration import LossObjective, LossSpec

if TYPE_CHECKING:
	from torch import Tensor


@dataclass(frozen=True)
class BatchLoss:
	"""A differentiable batch loss plus detached accounting totals."""

	loss: Tensor
	objective_numerator: float
	objective_denominator: int
	absolute_error: float
	squared_error: float
	value_count: int
	pair_count: int
	ordered_pair_count: int


@dataclass(frozen=True)
class LossMetrics:
	objective_loss: float
	mae: float
	rmse: float
	pair_count: int
	ordered_pair_count: int
	value_count: int

	@property
	def ordering_accuracy(self) -> float | None:
		return None if self.pair_count == 0 else self.ordered_pair_count / self.pair_count


def _matrix(value: Tensor, name: str) -> Tensor:
	import torch

	if not isinstance(value, torch.Tensor) or value.ndim not in (1, 2):
		raise ValueError(f"{name} must be a one- or two-dimensional tensor")
	result = value.unsqueeze(0) if value.ndim == 1 else value
	if result.numel() == 0 or not torch.isfinite(result).all():
		raise ValueError(f"{name} must be nonempty and finite")
	return result


def _checked_pair_block_size(value: int) -> int:
	if type(value) is not int or value <= 0:
		raise ValueError("Pair block size must be a positive integer")
	return value


def pairwise_components(outputs: Tensor, targets: Tensor, block_size: int) -> tuple[Tensor, int, int]:
	"""Return exact ranking loss sum, non-tied pair count, and ordered count."""

	import torch

	outputs = _matrix(outputs, "Outputs")
	targets = _matrix(targets, "Targets")
	if outputs.shape != targets.shape:
		raise ValueError("Outputs and targets must have matching shapes")
	block_size = _checked_pair_block_size(block_size)
	columns = outputs.shape[1]
	loss_sum = outputs.new_zeros(())
	pair_count = 0
	ordered_pair_count = 0
	for left_start in range(0, columns, block_size):
		left_end = min(columns, left_start + block_size)
		for right_start in range(left_start, columns, block_size):
			right_end = min(columns, right_start + block_size)
			target_delta = targets[:, left_start:left_end].unsqueeze(2) - targets[:, right_start:right_end].unsqueeze(1)
			score_delta = outputs[:, left_start:left_end].unsqueeze(2) - outputs[:, right_start:right_end].unsqueeze(1)
			valid = target_delta != 0
			if right_start == left_start:
				valid = valid & torch.triu(torch.ones_like(valid, dtype=torch.bool), diagonal=1)
			count = int(valid.sum().item())
			if count == 0:
				continue
			margin = torch.sign(target_delta) * score_delta
			loss_sum = loss_sum + torch.nn.functional.softplus(-margin).masked_select(valid).sum()
			pair_count += count
			ordered_pair_count += int(((score_delta * target_delta) > 0).masked_select(valid).sum().item())
	return loss_sum, pair_count, ordered_pair_count


def pairwise_ranking_loss(outputs: Tensor, targets: Tensor, *, block_size: int = 128) -> Tensor:
	"""Compute mean softplus(-sign(target delta) * score delta).

	Pairless rows are skipped by the block kernel.  A completely pairless input
	is rejected because it cannot establish a ranking objective.
	"""

	loss_sum, pair_count, _ = pairwise_components(outputs, targets, block_size)
	if pair_count == 0:
		raise ValueError("Pairwise ranking requires at least one non-tied ticker pair")
	return loss_sum / pair_count


def _safe_rmse(squared_error: Tensor, count: int) -> Tensor:
	import torch

	mean_squared = squared_error / count
	# The inner branch is constant at exactly zero, so the derivative is zero
	# instead of the NaN produced by differentiating sqrt(0).
	positive_root = torch.sqrt(torch.where(mean_squared > 0, mean_squared, torch.ones_like(mean_squared)))
	return torch.where(mean_squared > 0, positive_root, torch.zeros_like(positive_root))


def loss_batch(outputs: Tensor, targets: Tensor, specification: LossSpec) -> BatchLoss:
	"""Build one objective and its exact detached metric totals."""

	import torch

	if not isinstance(specification, LossSpec):
		raise ValueError("Loss calculation requires a typed loss specification")
	outputs = _matrix(outputs, "Outputs")
	targets = _matrix(targets, "Targets")
	if outputs.shape != targets.shape:
		raise ValueError("Outputs and targets must have matching shapes")
	absolute = torch.abs(outputs.double() - targets.double())
	squared = (outputs.double() - targets.double()) ** 2
	value_count = outputs.numel()
	absolute_error = float(absolute.sum().detach())
	squared_error = float(squared.sum().detach())
	with torch.no_grad():
		_, pair_count, ordered_pair_count = pairwise_components(outputs.detach(), targets.detach(), specification.pair_block_size or 128)
	if specification.objective is LossObjective.MAE:
		loss = torch.nn.functional.l1_loss(outputs, targets)
		objective_numerator, objective_denominator = absolute.sum().item(), value_count
	elif specification.objective is LossObjective.RMSE:
		loss = _safe_rmse((outputs - targets).pow(2).sum(), value_count)
		objective_numerator, objective_denominator = squared_error, value_count
	else:
		loss_sum, pair_count, ordered_pair_count = pairwise_components(outputs, targets, specification.pair_block_size or 128)
		if pair_count:
			loss = loss_sum / pair_count
		else:
			# Pairless dates are skipped.  This has a zero gradient and lets the
			# engine reject an entirely pairless training set after all batches.
			loss = outputs.sum() * 0.0
		objective_numerator, objective_denominator = float(loss_sum.detach()), pair_count
	return BatchLoss(loss, float(objective_numerator), objective_denominator, absolute_error, squared_error, value_count, pair_count, ordered_pair_count)


def summarize_totals(specification: LossSpec, objective_numerator: float, objective_denominator: int, absolute_error: float, squared_error: float, value_count: int, pair_count: int, ordered_pair_count: int) -> LossMetrics:
	if value_count <= 0 or objective_denominator <= 0:
		raise ValueError("Cannot summarize an empty loss calculation")
	objective_loss = sqrt(objective_numerator / objective_denominator) if specification.objective is LossObjective.RMSE else objective_numerator / objective_denominator
	mae = absolute_error / value_count
	rmse = sqrt(squared_error / value_count)
	if not all(isfinite(value) for value in (objective_loss, mae, rmse)):
		raise ValueError("Loss metrics are non-finite")
	return LossMetrics(objective_loss, mae, rmse, pair_count, ordered_pair_count, value_count)


__all__ = ["BatchLoss", "LossMetrics", "LossObjective", "LossSpec", "loss_batch", "pairwise_components", "pairwise_ranking_loss", "summarize_totals"]
