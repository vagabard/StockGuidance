from __future__ import annotations

from typing import Optional, Tuple

from .compat import torch, nn, optim

try:
	from models import make_model  # type: ignore
except Exception as e:
	print("make model error ", e.get_message(), "   ", e.get_stacktrace())
	make_model = None  # type: ignore


def create_model_and_optimizer(args, input_size: int, output_size: int, num_layers: int, activation: str, device, lr: float):
	"""Instantiate model via make_model and create Adam optimizer and AMP scaler."""
	if make_model is None:
		raise RuntimeError("models.make_model is unavailable")

	model = make_model(
		arch=str(args.arch),
		input_size=int(input_size),
		output_size=int(output_size),
		hidden_layers=int(args.hidden_layers),
		num_layers=int(num_layers),
		activation=str(activation),
	)
	model = model.to(device)
	optimizer = optim.Adam(model.parameters(), lr=float(lr))  # type: ignore[attr-defined]

	requested_precision = (args.precision or "auto").lower()
	use_amp = (device.type == "cuda") and (requested_precision in ("auto", "fp16", "bf16"))  # type: ignore[union-attr]
	amp_dtype = None
	if use_amp:
		# If auto and cuda, prefer fp16 unless explicitly bf16
		if requested_precision == "bf16":
			amp_dtype = torch.bfloat16
		else:
			amp_dtype = torch.float16
	scaler = torch.amp.GradScaler('cuda',enabled=(amp_dtype == torch.float16))  # type: ignore[attr-defined]
	return model, optimizer, scaler, use_amp, amp_dtype


__all__ = ["create_model_and_optimizer"]
