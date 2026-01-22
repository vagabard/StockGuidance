from __future__ import annotations

from torch import Tensor

try:
	import torch  # type: ignore
	import torch.nn as nn  # type: ignore
except Exception:  # pragma: no cover
	torch = None  # type: ignore
	nn = None  # type: ignore


from typing import Callable, List, Optional


class MultiLayerLinearNet(nn.Module):
	"""Configurable-depth MLP with configurable hidden widths.

	- Depth control: `num_layers` sets the total number of Linear layers, including
	  the final output layer. For example:
		* num_layers = 1 -> Linear(input_size, output_size)
		* num_layers = 2 -> Linear(input_size, hidden) -> Linear(hidden, output)
		* num_layers = 3 -> Linear(input, hidden) -> Linear(hidden, hidden) -> Linear(hidden, output)
	  This preserves the original "3-layer" default when `num_layers` is left at 3.

	- Width control: `hidden_layers` retains its previous meaning here as the
	  hidden layer widths. If not provided or positive, it defaults to `input_size`
	  to keep parity with prior behavior.
	"""
	def __init__(
		self,
		input_size: int,
		output_size: int,
		hidden_layers: List[int],
		activation_factory: Optional[Callable[[], nn.Module]] = None,
		final_activation: Optional[nn.Module] = None,
		num_layers: int = 3,
	):
		super().__init__()
		self.input_size = input_size
		self.output_size = output_size
		# Default activation factory to ReLU if not provided
		if activation_factory is None:
			activation_factory = lambda: nn.ReLU()

		# Resolve hidden layer widths
		# If hidden_layers is a list, use it.
		# If it's an empty list or contains values <= 0, we need to handle that.
		# For backward compatibility and uniform width support, we'll expand it if num_layers > 1.
		widths = []
		if isinstance(hidden_layers, list) and len(hidden_layers) > 0:
			for w in hidden_layers:
				widths.append(int(w) if w > 0 else int(input_size))
		else:
			# Fallback to input_size if no hidden layer width provided
			widths = [int(input_size)]

		# Build sequence of Linear and activation layers according to num_layers
		layers = []
		n = int(num_layers) if num_layers is not None else (len(widths) + 1 if len(widths) > 1 else 3)

		if n <= 1:
			# Degenerate case: direct linear mapping
			layers.append(nn.Linear(input_size, output_size))
		else:
			# If widths list is shorter than n-1, pad it with the last width
			while len(widths) < (n - 1):
				widths.append(widths[-1])
			# If widths list is longer than n-1, truncate it
			widths = widths[:n-1]

			# First hidden layer: input -> widths[0]
			layers.append(nn.Linear(input_size, widths[0]))
			layers.append(activation_factory())

			# Internal hidden layers: widths[i] -> widths[i+1]
			for i in range(len(widths) - 1):
				layers.append(nn.Linear(widths[i], widths[i+1]))
				layers.append(activation_factory())

			# Final output layer: widths[-1] -> output
			# Remove last activation if present
			if layers and not isinstance(layers[-1], nn.Linear):
				layers.pop()
			layers.append(nn.Linear(widths[-1], output_size))

		if final_activation is not None:
			fa = final_activation()
			layers.append(fa)

		self.net = nn.Sequential(*layers)

	def forward(self, x: Tensor) -> Tensor:
		if x.dim() == 1:
			x = x.unsqueeze(0)
		return self.net(x)
