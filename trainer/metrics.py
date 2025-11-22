from __future__ import annotations

from typing import Iterable, Tuple, Optional

import numpy as np

from .compat import torch, nn
from .ranking import batch_positions_from_prediction_and_labels, ranking_error_from_positions


def mse_restricted(y_pred: "torch.Tensor", y_true: "torch.Tensor", mode: str, k: int = 10) -> "torch.Tensor":
	"""MSE limited to top/bottom-k entries determined by predicted values (per-sample).

	mode: "top" or "bottom"
	"""
	assert mode in {"top", "bottom"}
	B, O = y_pred.shape
	if O == 0:
		return torch.tensor(0.0, device=y_pred.device, dtype=y_pred.dtype)
	kk = int(max(1, min(int(k), O)))
	if mode == "top":
		idx = torch.topk(y_pred, kk, dim=1, largest=True, sorted=False).indices
	else:
		idx = torch.topk(y_pred, kk, dim=1, largest=False, sorted=False).indices
	gather_pred = torch.gather(y_pred, 1, idx)
	gather_true = torch.gather(y_true.to(y_pred.dtype), 1, idx)
	return torch.mean((gather_pred - gather_true) ** 2)


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


def eval_epoch(
	model: "nn.Module",
	batch_iter: Iterable[Tuple["torch.Tensor", "torch.Tensor"]],
	device: "torch.device",
	amp_enabled: bool = False,
	amp_dtype: Optional["torch.dtype"] = None,
	rank_lower: int = 1,
	rank_upper: int = 10,
	compute_rank_err: bool = True,
	compute_rank_disp: bool = True,
) -> Tuple[float, float, float, float]:
	model.eval()
	total = 0
	mse_sum = 0.0
	top10_sum = 0.0
	from contextlib import nullcontext
	amp_cm = (
		torch.amp.autocast('cuda', dtype=amp_dtype) if (amp_enabled and device.type == "cuda") else nullcontext()  # type: ignore[attr-defined]
	)
	rde_sum = 0.0
	rank_err_sum = 0.0
	with torch.no_grad():
		with amp_cm:
			for xb, yb in batch_iter:
				pred = model(xb)
				pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
				label_to_pred_dtype = yb.to(pred.dtype)
				mse = torch.mean((pred - label_to_pred_dtype) ** 2)
				top10 = mse_restricted(pred, label_to_pred_dtype, mode="top", k=10)
				if compute_rank_disp:
					rde = rank_range_displacement(pred, yb.to(pred.dtype), rank_lower, rank_upper)
				else:
					rde = torch.tensor(0.0, device=pred.device, dtype=pred.dtype)
				if compute_rank_err:
					try:
						pos_list = batch_positions_from_prediction_and_labels(pred.detach().cpu(), yb.detach().cpu())
						batch_rank_err = float(np.mean([ranking_error_from_positions(pos) for pos in pos_list])) if len(pos_list) > 0 else 0.0
					except Exception:
						batch_rank_err = 0.0
				else:
					batch_rank_err = 0.0
				bs = xb.size(0)
				total += bs
				mse_sum += mse.item() * bs
				top10_sum += top10.item() * bs
				rde_sum += (rde.item() if compute_rank_disp else 0.0) * bs
				rank_err_sum += batch_rank_err * bs
	denom = max(1, total)
	rde_avg = rde_sum / denom
	rank_err_avg = rank_err_sum / denom
	return mse_sum / denom, top10_sum / denom, rde_avg, rank_err_avg


__all__ = [
	"mse_restricted",
	"rank_range_displacement",
	"eval_epoch",
]
