from __future__ import annotations

import os
import csv
import numpy as np
from typing import Tuple

import torch

from trainer.torch_interface.compat import TORCH_AVAILABLE


def tensorize_and_sanitize(x, y):
	"""Convert inputs to float32 torch_interface tensors and replace NaN/Inf values.

	Constructs tensors directly on the CUDA device when available to avoid
	extra host→device copies. If torch_interface is not available, returns inputs
	unchanged (memest mode).
	"""
	if not TORCH_AVAILABLE:
		return x, y

	# Prefer CUDA when available to save a CPU→GPU transfer later.
	device = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # type: ignore[attr-defined]
	print("1t and s x ", len(x))
	print("1t and s x0 ", x[0].shape)
	# Convert/transfer X
	if not isinstance(x, torch.Tensor):
		# Create directly on target device; as_tensor allows device specification.
		x =np.asarray(x, dtype=np.float32)
		print("2t and s x ", x.shape)
		x = torch.as_tensor(x, dtype=torch.float32, device=device)
		print("3t and s x ", x.shape)
	else:
		x = x.to(device=device, dtype=torch.float32)

	# Convert/transfer y
	if not isinstance(y, torch.Tensor):
		y = torch.as_tensor(np.asarray(y, dtype=np.float32), dtype=torch.float32, device=device)
	else:
		y = y.to(device=device, dtype=torch.float32)

	# Sanitize NaN/Inf values on-device
	x = torch.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
	print("4t and s x ", x.shape)
	y = torch.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
	return x, y


def count_elements(a) -> int:
	"""Return number of elements for torch_interface.Tensor or numpy.ndarray."""
	if hasattr(a, "numel"):
		try:
			return int(a.numel())  # type: ignore[attr-defined]
		except Exception:
			pass
	return int(np.size(a))


def fmt_bytes(num_bytes: float) -> str:
	units = ["B", "KB", "MB", "GB", "TB"]
	n = float(num_bytes)
	i = 0
	while n >= 1024.0 and i < len(units) - 1:
		n /= 1024.0
		i += 1
	return f"{n:.2f} {units[i]}"


def open_metrics_writer(metrics_file: str) -> Tuple[object, csv.writer]:
	"""Create the metrics CSV (with header) and return (file_handle, csv_writer)."""
	os.makedirs(os.path.dirname(metrics_file), exist_ok=True)
	f = open(metrics_file, mode="w", newline="", encoding="utf-8")
	writer = csv.writer(f)
	header = [
		"epoch",
		"train_mse",
		"val_mse",
		"train_top10_mse",
		"val_top10_mse",
		# "train_bottom10_mse",
		# "val_bottom10_mse",
		#"train_rank_err",
		#"val_rank_err",
	]
	writer.writerow(header)
	return f, writer


__all__ = [
	"tensorize_and_sanitize",
	"count_elements",
	"fmt_bytes",
	"open_metrics_writer",
]
