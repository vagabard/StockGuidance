from __future__ import annotations

import traceback
from typing import TYPE_CHECKING, Optional

from trainer.torch_interface.compat import torch, optim

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
	from trainer.state.family_run import FamilyRun

try:
	from models import make_model  # type: ignore
except Exception as e:
	traceback.print_exception(e)
	make_model = None  # type: ignore


def create_model_and_optimizer(
    family_run: FamilyRun,
    input_size: int,
    output_size: int,
    num_layers: int,
    activation: str,
    device: torch.device,
    lr: float,
    to_device: bool = True,
	prior_days: int = 0,
	optimizer_name: str = "adam",
	final_activation: Optional[str] = None,
) -> tuple[torch.nn.Module, optim.Optimizer, torch.amp.GradScaler, bool, Optional[torch.dtype]]:
	"""Instantiate model via make_model and create optimizer and AMP scaler."""
	if make_model is None:
		raise RuntimeError("models.make_model is unavailable")

	model = make_model(
		arch=str(family_run.neural_network_type),
		input_size=int(input_size),
		output_size=int(output_size),
		hidden_layers=family_run.hidden_layer_size,
		num_layers=int(num_layers),
		activation=str(activation),
		final_activation=final_activation,
		layer_types=family_run.layer_types,
		sequence_length=int(prior_days) + 1,
	)
	if to_device:
		model = model.to(device)

	optimizer_name = optimizer_name.lower()
	if optimizer_name == "adam":
		optimizer = optim.Adam(model.parameters(), lr=float(lr), fused=True)  # type: ignore[attr-defined]
	elif optimizer_name == "adamw":
		optimizer = optim.AdamW(model.parameters(), lr=float(lr), fused=True)  # type: ignore[attr-defined]
	elif optimizer_name == "sgd":
		optimizer = optim.SGD(model.parameters(), lr=float(lr), fused=True)
	else:
		raise ValueError(f"Unsupported optimizer: {optimizer_name}")

	requested_precision = (family_run.precision or "auto").lower()
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
