import math

import pytest
import torch
from torch.utils.data import DataLoader, TensorDataset

from dataclasses import replace
from stockguidance.config.training import decode_training_configuration
from stockguidance.models.specification import ModelSpec
from stockguidance.preprocessing import FittedTransform, NormalizationMethod
from stockguidance.training.configuration import DeviceChoice, LossObjective, LossSpec, TrainingConfig
from stockguidance.training.engine import evaluate_metrics
from stockguidance.training.losses import loss_batch, pairwise_components, pairwise_ranking_loss
from stockguidance.training.resources import resolve_resources
from stockguidance.training.service import train_model


def _dense_pairwise(outputs: torch.Tensor, targets: torch.Tensor) -> tuple[torch.Tensor, int, int]:
	losses = []
	ordered = 0
	for row_outputs, row_targets in zip(outputs, targets):
		for left in range(row_outputs.numel()):
			for right in range(left + 1, row_outputs.numel()):
				delta = row_targets[left] - row_targets[right]
				if delta == 0:
					continue
				score_delta = row_outputs[left] - row_outputs[right]
				losses.append(torch.nn.functional.softplus(-torch.sign(delta) * score_delta))
				ordered += int((score_delta * delta) > 0)
	return torch.stack(losses).sum(), len(losses), ordered


def test_pairwise_loss_matches_hand_calculation_and_is_translation_invariant() -> None:
	targets = torch.tensor([[2.0, 1.0, 0.0]])
	outputs = torch.tensor([[3.0, 1.0, 0.0]], requires_grad=True)
	expected = (torch.nn.functional.softplus(torch.tensor(-2.0)) + torch.nn.functional.softplus(torch.tensor(-3.0)) + torch.nn.functional.softplus(torch.tensor(-1.0))) / 3
	actual = pairwise_ranking_loss(outputs, targets, block_size=2)
	assert torch.allclose(actual, expected)
	assert torch.allclose(actual, pairwise_ranking_loss(outputs + 100.0, targets, block_size=8))
	actual.backward()
	assert torch.isfinite(outputs.grad).all()


def test_pairwise_blocks_match_dense_values_counts_and_gradients() -> None:
	targets = torch.tensor([[2.0, 1.0, 0.0, 0.0], [1.0, 4.0, 2.0, -1.0]])
	base = torch.tensor([[3.0, 1.0, -1.0, 4.0], [0.0, 5.0, 1.0, -2.0]])
	blocked_outputs = base.clone().requires_grad_()
	dense_outputs = base.clone().requires_grad_()
	blocked = pairwise_components(blocked_outputs, targets, 2)
	dense = _dense_pairwise(dense_outputs, targets)
	assert blocked[1:] == dense[1:]
	assert torch.allclose(blocked[0], dense[0])
	(blocked[0]).backward()
	(dense[0]).backward()
	assert torch.allclose(blocked_outputs.grad, dense_outputs.grad)


def test_pairless_dates_are_skipped_but_an_entirely_pairless_input_fails() -> None:
	targets = torch.tensor([[1.0, 1.0, 1.0], [2.0, 1.0, 0.0]])
	outputs = torch.tensor([[3.0, 2.0, 1.0], [1.0, 3.0, 0.0]])
	_, count, ordered = pairwise_components(outputs, targets, 2)
	assert count == 3 and ordered == 2
	with pytest.raises(ValueError, match="non-tied"):
		pairwise_ranking_loss(outputs[:1], targets[:1])


def test_rmse_zero_error_has_finite_zero_gradient() -> None:
	values = torch.tensor([[1.0, -2.0]], requires_grad=True)
	batch = loss_batch(values, values.detach(), LossSpec(LossObjective.RMSE, "loss-v2"))
	batch.loss.backward()
	assert batch.loss.item() == 0.0
	assert torch.equal(values.grad, torch.zeros_like(values))


def test_global_rmse_uses_total_squared_error_not_batch_average() -> None:
	# Identity returns the input as the prediction.  The batches have different
	# sizes, so averaging batch RMSEs would produce the wrong answer.
	inputs = torch.tensor([[0.0], [3.0], [3.0], [3.0]])
	targets = torch.zeros_like(inputs)
	loader = DataLoader(TensorDataset(inputs, targets), batch_size=3, shuffle=False)
	metrics = evaluate_metrics(torch.nn.Identity(), loader, resolve_resources(TrainingConfig(device=DeviceChoice.CPU)), LossSpec(LossObjective.RMSE, "loss-v2"))
	assert metrics.rmse == pytest.approx(math.sqrt(27.0 / 4.0))
	assert metrics.objective_loss == metrics.rmse


def test_normalized_outputs_are_measured_in_target_space() -> None:
	inputs = torch.tensor([[0.0], [1.0]])
	# Normalized target values correspond to target-space values 10 and 12.
	targets = torch.tensor([[0.0], [1.0]])
	loader = DataLoader(TensorDataset(inputs, targets), batch_size=2, shuffle=False)
	transform = FittedTransform(NormalizationMethod.STANDARD, "one_shared_scalar", (10.0,), (2.0,), (False,), 2)
	metrics = evaluate_metrics(torch.nn.Identity(), loader, resolve_resources(TrainingConfig(device=DeviceChoice.CPU)), LossSpec(), transform)
	assert metrics.mae == 0.0 and metrics.rmse == 0.0


@pytest.mark.parametrize("objective", [LossObjective.RMSE, LossObjective.PAIRWISE_RANKING])
def test_training_reports_v2_objective_and_target_metrics(training, tmp_path, objective: LossObjective) -> None:
	dataset, _, config = training
	loss = LossSpec(objective, "loss-v2")
	result = train_model(dataset, replace(config, loss=loss, epochs=1), tmp_path / objective.value)
	assert result.loss_objective == objective.value
	assert result.training_objective_loss is not None and result.training_rmse is not None
	assert result.training_non_tied_pair_count > 0
	assert result.training_ordering_accuracy is not None


def test_job_decoder_exposes_explicit_loss_v2() -> None:
	decoded = decode_training_configuration({"loss": {"objective": "rmse", "version": "loss-v2"}}, ModelSpec().to_mapping())
	assert decoded.loss == LossSpec(LossObjective.RMSE, "loss-v2")
