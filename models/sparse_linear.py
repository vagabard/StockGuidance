import torch
import torch.nn as nn
from typing import Optional

class SparseLinear(nn.Module):
	"""
	A custom sparse linear layer with a specific connection pattern.
	Uses torch.sparse to reduce memory and potentially increase speed.
	"""
	def __init__(self, input_size: int, output_size: int, bias: bool = True):
		super().__init__()
		self.input_size = input_size
		self.output_size = output_size

		indices_tensor = self.create_indices()

		# Weight values
		self.weight_values = nn.Parameter(torch.Tensor(indices_tensor.size(1)))
		self.register_buffer('indices', indices_tensor)

		if bias:
			self.bias = nn.Parameter(torch.Tensor(output_size))
		else:
			self.register_parameter('bias', None)

		self.reset_parameters()

	def create_indices(self) -> torch.Tensor:
		indices = []
		current_out_idx = 0
		mod_val = 1

		while current_out_idx < self.output_size:
			for _ in range(mod_val):
				if current_out_idx < self.output_size:
					# output node 1-based index 'k' maps to input 'i' where i % mod_val == k % mod_val
					# Using 0-based indexing for code: k = current_out_idx + 1
					k = current_out_idx + 1
					remainder = k % mod_val
					for i in range(self.input_size):
						if i % mod_val == remainder:
							indices.append([current_out_idx, i])
					current_out_idx += 1
				else:
					break
			mod_val += 1

		if not indices:
			# Fallback for very small output_size if needed, though patterns cover it
			return torch.empty((2, 0), dtype=torch.long)
		else:
			return torch.tensor(indices, dtype=torch.long).t()

	def reset_parameters(self):
		# Kaiming uniform initialization for weights
		fan_in = self.input_size # Simplified
		nn.init.uniform_(self.weight_values, -1 / (fan_in**0.5), 1 / (fan_in**0.5))
		if self.bias is not None:
			nn.init.uniform_(self.bias, -1 / (fan_in**0.5), 1 / (fan_in**0.5))

	def forward(self, x):
		# x shape: [batch, input_size]
		# Result: (batch, output_size)

		# Using cuBLAS by expanding to a dense matrix.
		# This is faster than torch.sparse.mm on GPU for these dimensions and sparsity.
		# It also maintains the memory benefit of storing fewer weights (only non-zero values).
		weight = torch.zeros(self.output_size, self.input_size, device=x.device, dtype=x.dtype)
		weight[self.indices[0], self.indices[1]] = self.weight_values

		out = torch.mm(x, weight.t())

		if self.bias is not None:
			out += self.bias

		return out


class RandomPercentLinear(nn.Linear):
	"""
	A subtype of nn.Linear that masks connections randomly based on a probability.
	Each connection (i, j) is enabled independently with probability connection_probability.
	"""
	def __init__(self, input_size: int, output_size: int, connection_probability: float, bias: bool = True):
		super().__init__(input_size, output_size, bias)
		self.connection_probability = connection_probability

		# Create a fixed mask
		# Each connection (i, j) has probability connection_probability of being 1
		probs = torch.rand(output_size, input_size)
		mask = (probs < connection_probability).float()
		self.register_buffer('mask', mask)

	def forward(self, x: torch.Tensor) -> torch.Tensor:
		# Apply mask to weights before computation
		masked_weight = self.weight * self.mask
		return torch.nn.functional.linear(x, masked_weight, self.bias)

