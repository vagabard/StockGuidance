from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
	from torch import Tensor, nn


@cache
def convolution_type() -> type[nn.Module]:
	from torch import nn

	class ConvolutionAndTransformerFeedForwareNet(nn.Module):
		def __init__(self, input_size: int, output_size: int, hidden_layers: list[int]) -> None:
			super().__init__()
			self.conv1 = nn.Conv1d(1, 16, kernel_size=15, padding=7)
			self.conv2 = nn.Conv1d(16, 16, kernel_size=15, padding=7)
			self.conv3 = nn.Conv1d(16, 16, kernel_size=15, padding=7)
			self.trans1 = nn.TransformerEncoder(nn.TransformerEncoderLayer(d_model=16, nhead=4, dim_feedforward=64, activation="gelu", batch_first=True, norm_first=True), num_layers=1)
			self.trans2 = nn.TransformerEncoder(nn.TransformerEncoderLayer(d_model=16, nhead=4, dim_feedforward=64, activation="gelu", batch_first=True, norm_first=True), num_layers=1)
			self.proj = nn.Conv1d(16, output_size, kernel_size=1)
			self.act = nn.ReLU()

		def forward(self, inputs: Tensor) -> Tensor:
			if inputs.dim() == 1:
				inputs = inputs.unsqueeze(0)
			sequence = inputs.reshape(inputs.size(0), 1, inputs.size(1))
			sequence = self.act(self.conv1(sequence))
			sequence = self.trans1(sequence.transpose(1, 2)).transpose(1, 2)
			sequence = self.act(self.conv2(sequence))
			sequence = self.trans2(sequence.transpose(1, 2)).transpose(1, 2)
			return cast("Tensor", self.proj(self.act(self.conv3(sequence))).mean(dim=2))

	return ConvolutionAndTransformerFeedForwareNet
