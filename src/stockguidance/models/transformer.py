from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
	from torch import Tensor, nn


@cache
def transformer_layer_type() -> type[nn.Module]:
	from torch import nn

	class TransformerLayer(nn.Module):
		def __init__(self, in_features: int, out_features: int, nhead: int = 4, dim_feedforward: int = 256, sequence_length: int = 1) -> None:
			super().__init__()
			self.in_features, self.out_features = in_features, out_features
			self.sequence_length = sequence_length
			self.use_sequence = sequence_length > 1 and in_features % sequence_length == 0 and out_features % sequence_length == 0
			self.d_model_in = in_features // sequence_length if self.use_sequence else in_features
			self.d_model_out = out_features // sequence_length if self.use_sequence else out_features
			self.input_proj = nn.Linear(self.d_model_in, self.d_model_out) if self.d_model_in != self.d_model_out else nn.Identity()
			effective_heads = nhead if self.d_model_out % nhead == 0 else 1
			encoder = nn.TransformerEncoderLayer(d_model=self.d_model_out, nhead=effective_heads, dim_feedforward=dim_feedforward, batch_first=True)
			self.transformer = nn.TransformerEncoder(encoder, num_layers=1)

		def forward(self, inputs: Tensor) -> Tensor:
			sequence = inputs.reshape(inputs.size(0), self.sequence_length, self.d_model_in) if self.use_sequence else inputs.unsqueeze(1)
			outputs = self.transformer(self.input_proj(sequence))
			return cast("Tensor", outputs.reshape(inputs.size(0), self.out_features) if self.use_sequence else outputs.squeeze(1))

	return TransformerLayer
