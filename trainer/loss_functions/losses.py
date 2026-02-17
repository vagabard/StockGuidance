from __future__ import annotations

from trainer import torch
from trainer.torch_interface.compat import F


def ranking_surrogate_loss(y_pred: "torch.Tensor", y_true: "torch.Tensor", temperature: float = 1.0) -> "torch.Tensor":
	"""Listwise surrogate loss encouraging predicted ranks to match labels via softmax distribution matching.

	loss = KL( softmax(y_true / T) || softmax(y_pred / T) ), averaged over batch.
	"""
	assert y_pred.ndim == 2 and y_true.ndim == 2, "Expected (B, O) tensors"
	T = max(1e-6, float(temperature))
	log_q = F.log_softmax(y_pred / T, dim=1)
	p = F.softmax(y_true.to(y_pred.dtype) / T, dim=1)
	return F.kl_div(log_q, p, reduction='batchmean')


__all__ = ["ranking_surrogate_loss", "mse_restricted", "rank_range_displacement"]


def mse_restricted(y_pred: "torch.Tensor", y_true: "torch.Tensor", mode: str, k: int = 10) -> "torch.Tensor":
	"""MSE limited to top/bottom-k entries determined by predicted values (per-sample)
	AND top/bottom-k entries determined by true values (per-sample).

	The result is the average of these two restricted MSEs.

	mode: "top" or "bottom"
	"""
	assert mode in {"top", "bottom"}
	B, O = y_pred.shape
	if O == 0:
		return torch.tensor(0.0, device=y_pred.device, dtype=y_pred.dtype)
	kk = int(max(1, min(int(k), O)))
	largest = (mode == "top")

	# Part 1: MSE on top/bottom k predictions
	idx_pred = torch.topk(y_pred, kk, dim=1, largest=largest, sorted=False).indices
	gather_pred_1 = torch.gather(y_pred, 1, idx_pred)
	gather_true_1 = torch.gather(y_true.to(y_pred.dtype), 1, idx_pred)
	mse_pred_based = torch.mean((gather_pred_1 - gather_true_1) ** 2)

	# Part 2: MSE on top/bottom k labels
	idx_true = torch.topk(y_true.to(y_pred.dtype), kk, dim=1, largest=largest, sorted=False).indices
	gather_pred_2 = torch.gather(y_pred, 1, idx_true)
	gather_true_2 = torch.gather(y_true.to(y_pred.dtype), 1, idx_true)
	mse_true_based = torch.mean((gather_pred_2 - gather_true_2) ** 2)

	return (mse_pred_based + mse_true_based) / 2.0


def rank_range_displacement(y_pred: "torch.Tensor", y_true: "torch.Tensor", lower: int, upper: int) -> "torch.Tensor":
	"""Rank displacement error averaged over a 1-based window [lower..upper]."""
	B, O = y_pred.shape
	if O == 0:
		return torch.tensor(0.0, device=y_pred.device, dtype=y_pred.dtype)
	lo = max(1, int(lower))
	hi = max(1, int(upper))
	if lo > hi:
		lo, hi = hi, lo
	lo = max(1, min(lo, O))
	hi = max(1, min(hi, O))
	count = hi - lo + 1
	if count <= 0:
		return torch.tensor(0.0, device=y_pred.device, dtype=y_pred.dtype)

	pred_order = torch.argsort(y_pred, dim=1, descending=True)
	true_order = torch.argsort(y_true.to(y_pred.dtype), dim=1, descending=True)

	total_err = torch.zeros((), device=y_pred.device, dtype=y_pred.dtype)
	for b in range(B):
		idx_pred = pred_order[b]
		idx_true = true_order[b]
		pos_pred = torch.empty(O, device=y_pred.device, dtype=torch.long)
		pos_true = torch.empty(O, device=y_pred.device, dtype=torch.long)
		pos_pred[idx_pred] = torch.arange(O, device=y_pred.device)
		pos_true[idx_true] = torch.arange(O, device=y_pred.device)

		sample_err = torch.zeros((), device=y_pred.device, dtype=y_pred.dtype)
		for p in range(lo, hi + 1):
			p0 = p - 1
			i_pred_at_p = idx_pred[p0]
			true_pos_of_that = pos_true[i_pred_at_p].item() + 1
			i_true_at_p = idx_true[p0]
			pred_pos_of_that = pos_pred[i_true_at_p].item() + 1
			d1 = float(p - true_pos_of_that)
			d2 = float(pred_pos_of_that - p)
			sample_err = sample_err + torch.tensor(d1 * d1 + d2 * d2, device=y_pred.device, dtype=y_pred.dtype)
		total_err = total_err + (sample_err / float(count))
	return total_err / float(B if B > 0 else 1)
