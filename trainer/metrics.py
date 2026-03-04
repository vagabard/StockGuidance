from __future__ import annotations

from typing import Iterable, Tuple, Optional

import numpy as np
import torch

from trainer.torch_interface.compat import  nn
from .loss_functions.losses import mse_restricted, rank_range_displacement
from trainer.loss_functions.ranking import batch_positions_from_prediction_and_labels, ranking_error_from_positions


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
	pin_memory: bool = False,
) -> Tuple[float, float, float, dict[str, int]]:
	model.eval()
	total = 0
	mse_sum = 0.0
	top10_sum = 0.0
	counts = {"label_pos": 0, "label_neg": 0, "label_zero": 0, "pred_pos": 0, "pred_neg": 0, "pred_zero": 0}
	from contextlib import nullcontext
	amp_cm = (
		torch.amp.autocast('cuda', dtype=amp_dtype) if (amp_enabled and device.type == "cuda") else nullcontext()  # type: ignore[attr-defined]
	)
	rde_sum = 0.0
	#rank_err_sum = 0.0
	with torch.no_grad():
		with amp_cm:
			for xb, yb in batch_iter:
				if not isinstance(xb, torch.Tensor):
					xb = torch.as_tensor(xb, dtype=torch.float32, device=device)
				else:
					xb = xb.to(device=device, dtype=torch.float32, non_blocking=pin_memory)
				if not isinstance(yb, torch.Tensor):
					yb = torch.as_tensor(yb, dtype=torch.float32, device=device)
				else:
					yb = yb.to(device=device, dtype=torch.float32, non_blocking=pin_memory)
				# print(xb.shape)
				# print(yb.shape)
				pred = model(xb)
				if not torch.isfinite(pred).all():
					print("  WARNING: Model output contains NaNs or Infs during evaluation!")
				pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
				label_to_pred_dtype = yb.to(pred.dtype)
				mse = torch.mean((pred - label_to_pred_dtype) ** 2)
				top10 = mse_restricted(pred, label_to_pred_dtype, mode="top", k=10)

				counts["label_pos"] += int((yb > 0).sum().item())
				counts["label_neg"] += int((yb < 0).sum().item())
				counts["label_zero"] += int((yb == 0).sum().item())
				counts["pred_pos"] += int((pred > 0).sum().item())
				counts["pred_neg"] += int((pred < 0).sum().item())
				counts["pred_zero"] += int((pred == 0).sum().item())

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
				#rank_err_sum += batch_rank_err * bs

				# Explicitly clear tensors to help GC
				del xb, yb, pred, label_to_pred_dtype, mse, top10, rde
				if compute_rank_err:
					del pos_list
	denom = max(1, total)
	rde_avg = rde_sum / denom
	#rank_err_avg = rank_err_sum / denom
	return mse_sum / denom, top10_sum / denom, rde_avg, counts # , rank_err_avg


__all__ = [
	"eval_epoch",
]
