from __future__ import annotations

try:
	import torch  # type: ignore
	import torch.nn as nn  # type: ignore
except Exception:  # pragma: no cover
	torch = None  # type: ignore
	nn = None  # type: ignore


from typing import Callable, Optional


class MultiLayerLinearNet(nn.Module):
	"""Configurable-depth MLP with uniform hidden width.

	- Depth control: `num_layers` sets the total number of Linear layers, including
	  the final output layer. For example:
		* num_layers = 1 -> Linear(input_size, output_size)
		* num_layers = 2 -> Linear(input_size, hidden) -> Linear(hidden, output)
		* num_layers = 3 -> Linear(input, hidden) -> Linear(hidden, hidden) -> Linear(hidden, output)
	  This preserves the original "3-layer" default when `num_layers` is left at 3.

	- Width control: `hidden_layers` retains its previous meaning here as the
	  hidden layer width. If not positive, it defaults to `input_size` to keep
	  parity with prior behavior.
	"""
	def __init__(
		self,
		input_size: int,
		output_size: int,
		hidden_layers: int,
		activation_factory: Optional[Callable[[], nn.Module]] = None,
		num_layers: int = 3,
	):
		super().__init__()
		width = int(hidden_layers) if hidden_layers and hidden_layers > 0 else int(input_size)

		# Default activation factory to ReLU if not provided
		if activation_factory is None:
			activation_factory = lambda: nn.ReLU()

		# Build sequence of Linear and activation layers according to num_layers
		layers = []
		n = int(num_layers) if num_layers is not None else 3
		if n <= 1:
			# Degenerate case: direct linear mapping
			layers.append(nn.Linear(input_size, output_size))
		else:
			# First hidden layer: input -> hidden
			layers.append(nn.Linear(input_size, width))
			layers.append(activation_factory())
			# Internal hidden layers before output: repeat (n - 2) times of [Linear(width,width) + Act]
			for _ in range(max(0, n - 2)):
				layers.append(nn.Linear(width, width))
				layers.append(activation_factory())
			# Final output layer: hidden -> output (remove the last activation if present)
			# Remove the last activation added in the loop to ensure no activation after output
			if isinstance(layers[-1], nn.Module) and not isinstance(layers[-1], nn.Linear):
				layers.pop()
			layers.append(nn.Linear(width, output_size))

		self.net = nn.Sequential(*layers)

	def forward(self, x: "torch.Tensor") -> "torch.Tensor":
		if x.dim() == 1:
			x = x.unsqueeze(0)
		return self.net(x)
