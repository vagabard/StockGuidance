from __future__ import annotations

from typing import TYPE_CHECKING

from stockguidance.preprocessing import FittedTransform
from stockguidance.training.configuration import LossSpec
from stockguidance.training.losses import LossMetrics, loss_batch, summarize_totals
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


def _target_space(outputs: Tensor, targets: Tensor, output_transform: FittedTransform | None) -> tuple[Tensor, Tensor]:
	if output_transform is None:
		return outputs, targets
	if output_transform.axis != "one_shared_scalar":
		raise ValueError("Training output normalization must use one shared scalar")
	location, scale = outputs.new_tensor(output_transform.location[0]), outputs.new_tensor(output_transform.scale[0])
	return outputs * scale + location, targets * scale + location


def _empty_totals() -> tuple[float, int, float, float, int, int, int]:
	return 0.0, 0, 0.0, 0.0, 0, 0, 0


def fit_epoch_metrics(model: nn.Module, loader: DataLoader[tuple[Tensor, Tensor]], optimizer: Optimizer, scaler: GradScaler, resources: Resources, specification: LossSpec = LossSpec(), output_transform: FittedTransform | None = None) -> LossMetrics:
	import torch

	model.train()
	objective_numerator, objective_denominator, absolute_error, squared_error, value_count, pair_count, ordered_pair_count = _empty_totals()
	for inputs, targets in loader:
		inputs, targets = inputs.to(resources.device), targets.to(resources.device)
		optimizer.zero_grad(set_to_none=True)
		with torch.autocast(device_type=resources.device.type, dtype=resources.amp_dtype, enabled=resources.amp_dtype is not None):
			outputs = checked_predictions(model, inputs, targets)
			target_outputs, target_values = _target_space(outputs, targets, output_transform)
			batch = loss_batch(target_outputs, target_values, specification)
			loss = batch.loss
		if not torch.isfinite(loss):
			raise ValueError("Training produced non-finite loss")
		torch.autograd.backward(tensors=(scaler.scale(loss),))
		scaler.unscale_(optimizer)
		if any(parameter.grad is not None and not torch.isfinite(parameter.grad).all() for parameter in model.parameters()):
			raise ValueError("Training produced non-finite gradients")
		scaler.step(optimizer)
		scaler.update()
		objective_numerator += batch.objective_numerator
		objective_denominator += batch.objective_denominator
		absolute_error += batch.absolute_error
		squared_error += batch.squared_error
		value_count += batch.value_count
		pair_count += batch.pair_count
		ordered_pair_count += batch.ordered_pair_count
	if not value_count:
		raise ValueError("Cannot fit an empty training dataset")
	if specification.objective.value == "pairwise_ranking" and pair_count == 0:
		raise ValueError("Pairwise ranking requires at least one non-tied ticker pair in training")
	if objective_denominator == 0:
		# A pairless ranking batch contributes no objective, but an entirely
		# pairless epoch was rejected above.
		objective_denominator = pair_count
	return summarize_totals(specification, objective_numerator, objective_denominator, absolute_error, squared_error, value_count, pair_count, ordered_pair_count)


def fit_epoch(model: nn.Module, loader: DataLoader[tuple[Tensor, Tensor]], optimizer: Optimizer, scaler: GradScaler, resources: Resources, specification: LossSpec = LossSpec(), output_transform: FittedTransform | None = None) -> float:
	"""Compatibility wrapper returning the epoch objective scalar."""

	return fit_epoch_metrics(model, loader, optimizer, scaler, resources, specification, output_transform).objective_loss


def evaluate_metrics(model: nn.Module, loader: DataLoader[tuple[Tensor, Tensor]], resources: Resources, specification: LossSpec = LossSpec(), output_transform: FittedTransform | None = None) -> LossMetrics:
	import torch

	model.eval()
	objective_numerator, objective_denominator, absolute_error, squared_error, value_count, pair_count, ordered_pair_count = _empty_totals()
	with torch.inference_mode():
		for inputs, targets in loader:
			inputs, targets = inputs.to(resources.device), targets.to(resources.device)
			outputs = checked_predictions(model, inputs, targets)
			target_outputs, target_values = _target_space(outputs, targets, output_transform)
			batch = loss_batch(target_outputs, target_values, specification)
			objective_numerator += batch.objective_numerator
			objective_denominator += batch.objective_denominator
			absolute_error += batch.absolute_error
			squared_error += batch.squared_error
			value_count += batch.value_count
			pair_count += batch.pair_count
			ordered_pair_count += batch.ordered_pair_count
	if not value_count:
		raise ValueError("Cannot evaluate an empty dataset")
	if specification.objective.value == "pairwise_ranking" and pair_count == 0:
		raise ValueError("Pairwise ranking requires at least one non-tied ticker pair")
	if objective_denominator == 0:
		raise ValueError("Cannot evaluate a pairless ranking dataset")
	return summarize_totals(specification, objective_numerator, objective_denominator, absolute_error, squared_error, value_count, pair_count, ordered_pair_count)


def evaluate(model: nn.Module, loader: DataLoader[tuple[Tensor, Tensor]], resources: Resources, specification: LossSpec = LossSpec(), output_transform: FittedTransform | None = None) -> float:
	"""Compatibility wrapper returning target-space MAE."""

	return evaluate_metrics(model, loader, resources, specification, output_transform).mae
