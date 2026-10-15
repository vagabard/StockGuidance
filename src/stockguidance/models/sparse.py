from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from torch import Tensor, nn


@cache
def sparse_linear_type() -> type[nn.Module]:
	import torch
	from torch import nn

	class SparseLinear(nn.Module):
		indices: Tensor
		bias: nn.Parameter | None

		def __init__(self, input_size: int, output_size: int, bias: bool = True) -> None:
			super().__init__()
			self.input_size, self.output_size = input_size, output_size
			indices = self.create_indices()
			self.weight_values = nn.Parameter(torch.empty(indices.size(1)))
			self.register_buffer("indices", indices)
			self.register_parameter("bias", nn.Parameter(torch.empty(output_size)) if bias else None)
			self.reset_parameters()

		def create_indices(self) -> Tensor:
			indices: list[list[int]] = []
			output_index, modulus = 0, 1
			while output_index < self.output_size:
				for group_index in range(modulus):
					if output_index >= self.output_size:
						break
					remainder = (output_index + 1) % modulus
					indices.extend([output_index, input_index] for input_index in range(self.input_size) if input_index % modulus == remainder)
					output_index += 1
				modulus += 1
			return torch.tensor(indices, dtype=torch.long).t() if indices else torch.empty((2, 0), dtype=torch.long)

		def reset_parameters(self) -> None:
			bound = self.input_size ** -0.5
			nn.init.uniform_(self.weight_values, -bound, bound)
			if self.bias is not None:
				nn.init.uniform_(self.bias, -bound, bound)

		def forward(self, inputs: Tensor) -> Tensor:
			weight = torch.zeros(self.output_size, self.input_size, device=inputs.device, dtype=inputs.dtype)
			weight[self.indices[0], self.indices[1]] = self.weight_values
			outputs = torch.mm(inputs, weight.t())
			return outputs if self.bias is None else outputs + self.bias

	return SparseLinear


@cache
def random_percent_type() -> type[nn.Module]:
	import torch
	from torch import nn

	class RandomPercentLinear(nn.Linear):
		mask: Tensor

		def __init__(self, input_size: int, output_size: int, connection_probability: float, bias: bool = True) -> None:
			super().__init__(input_size, output_size, bias)
			self.connection_probability = connection_probability
			self.register_buffer("mask", (torch.rand(output_size, input_size) < connection_probability).float())

		def forward(self, inputs: Tensor) -> Tensor:
			return nn.functional.linear(inputs, self.weight * self.mask, self.bias)

	return RandomPercentLinear
