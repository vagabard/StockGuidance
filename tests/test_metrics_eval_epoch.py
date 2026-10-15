from __future__ import annotations

import pytest
import torch

from trainer.metrics import eval_epoch


class _ConstantModel(torch.nn.Module):
	def __init__(self, output_dim: int, fill_value: float = 0.0) -> None:
		super().__init__()
		self.output_dim = output_dim
		self.fill_value = fill_value

	def forward(self, xb: torch.Tensor) -> torch.Tensor:  # type: ignore[override]
		batch = xb.size(0)
		return torch.full((batch, self.output_dim), float(self.fill_value), device=xb.device)


def test_eval_epoch_returns_mean_mse_and_topk_without_rank(monkeypatch) -> None:
	model = _ConstantModel(output_dim=2, fill_value=0.0)
	# Two batches with known labels; predictions are all zeros.
	xb1 = torch.zeros((2, 1))
	yb1 = torch.tensor([[1.0, 3.0], [2.0, 0.0]])
	xb2 = torch.zeros((1, 1))
	yb2 = torch.tensor([[-1.0, 1.0]])
	batches = [(xb1, yb1), (xb2, yb2)]

	mse, top10, rde, counts = eval_epoch(
		model,
		batches,
		device=torch.device("cpu"),
		compute_rank_err=False,
		compute_rank_disp=False,
	)

	expected = 8.0 / 3.0  # (3.5 * 2 + 1 * 1) / total_items(3)
	assert mse == pytest.approx(expected)
	assert top10 == pytest.approx(expected)  # top-k loss mirrors mse for this setup
	assert rde == 0.0
	assert counts["label_pos"] == 4
	assert counts["label_neg"] == 1
	assert counts["label_zero"] == 1
	assert counts["pred_zero"] == 6 # 3 samples * 2 outputs
