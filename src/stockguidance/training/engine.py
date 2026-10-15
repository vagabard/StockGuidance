from __future__ import annotations

from math import isfinite
from typing import TYPE_CHECKING

from stockguidance.training.resources import Resources

if TYPE_CHECKING:
	from torch import Tensor, nn
	from torch.amp import GradScaler
	from torch.optim import Optimizer
	from torch.utils.data import DataLoader


def checked_predictions(model: nn.Module, inputs: Tensor, targets: Tensor) -> Tensor:
	import torch

	outputs = model(inputs)
	if not isinstance(outputs, torch.Tensor) or outputs.shape != targets.shape or not torch.isfinite(inputs).all() or not torch.isfinite(targets).all() or not torch.isfinite(outputs).all():
		raise ValueError("Model batches require matching finite inputs, outputs and targets")
	return outputs


def fit_epoch(model: nn.Module, loader: DataLoader[tuple[Tensor, Tensor]], optimizer: Optimizer, scaler: GradScaler, resources: Resources) -> float:
	import torch

	model.train()
	total_error, total_values = 0.0, 0
	for inputs, targets in loader:
		inputs, targets = inputs.to(resources.device), targets.to(resources.device)
		optimizer.zero_grad(set_to_none=True)
		with torch.autocast(device_type=resources.device.type, dtype=resources.amp_dtype, enabled=resources.amp_dtype is not None):
			outputs = checked_predictions(model, inputs, targets)
			loss = torch.nn.functional.l1_loss(outputs, targets)
		if not torch.isfinite(loss):
			raise ValueError("Training produced non-finite loss")
		torch.autograd.backward(tensors=(scaler.scale(loss),))
		scaler.unscale_(optimizer)
		if any(parameter.grad is not None and not torch.isfinite(parameter.grad).all() for parameter in model.parameters()):
			raise ValueError("Training produced non-finite gradients")
		scaler.step(optimizer)
		scaler.update()
		total_error += float(loss.detach()) * targets.numel()
		total_values += targets.numel()
	if not total_values:
		raise ValueError("Cannot fit an empty training dataset")
	result = total_error / total_values
	if not isfinite(result):
		raise ValueError("Training metric is non-finite")
	return result


def evaluate(model: nn.Module, loader: DataLoader[tuple[Tensor, Tensor]], resources: Resources) -> float:
	import torch

	model.eval()
	total_error, total_values = 0.0, 0
	with torch.inference_mode():
		for inputs, targets in loader:
			inputs, targets = inputs.to(resources.device), targets.to(resources.device)
			outputs = checked_predictions(model, inputs, targets)
			total_error += float(torch.abs(outputs.double() - targets.double()).sum())
			total_values += targets.numel()
	if not total_values:
		raise ValueError("Cannot evaluate an empty dataset")
	result = total_error / total_values
	if not isfinite(result):
		raise ValueError("Evaluation metric is non-finite")
	return result
