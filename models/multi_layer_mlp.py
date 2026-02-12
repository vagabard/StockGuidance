from __future__ import annotations

from torch import Tensor

try:
	import torch  # type: ignore
	import torch.nn as nn  # type: ignore
except Exception:  # pragma: no cover
	torch = None  # type: ignore
	nn = None  # type: ignore


from typing import Callable, List, Optional, Union

from .sparse_linear import SparseLinear, RandomPercentSparseLinear


class TransformerLayer(nn.Module):
	"""
	A simple transformer encoder layer wrapper.
	It optionally reshapes input to (batch, sequence_length, features_per_token).
	"""
	def __init__(self, in_features: int, out_features: int, nhead: int = 4, dim_feedforward: int = 256, sequence_length: int = 1):
		super().__init__()
		self.in_features = in_features
		self.out_features = out_features
		self.sequence_length = sequence_length

		# Determine if we should reshape.
		# If sequence_length > 1 and in_features is divisible by it, we use it.
		self.use_sequence = (self.sequence_length > 1) and (self.in_features % self.sequence_length == 0)

		if self.use_sequence:
			self.d_model_in = self.in_features // self.sequence_length
		else:
			self.d_model_in = self.in_features

		# For MultiLayerLinearNet, we often have different in/out features.
		# Transformer requires d_model to stay constant.
		# We'll use out_features as d_model if they are different from d_model_in.
		# If we are using sequence, out_features must also be divisible by sequence_length
		if self.use_sequence and (self.out_features % self.sequence_length == 0):
			self.d_model_out = self.out_features // self.sequence_length
		else:
			# If out_features is not divisible, we might need to change strategy or just not use sequence for output.
			# But for simplicity, let's assume if use_sequence is True, we want to maintain it.
			# If it's not divisible, we fallback to sequence_length 1 for this layer's logic.
			self.use_sequence = False
			self.d_model_in = self.in_features
			self.d_model_out = self.out_features

		if self.d_model_in != self.d_model_out:
			self.input_proj = nn.Linear(self.d_model_in, self.d_model_out)
		else:
			self.input_proj = nn.Identity()

		# Ensure nhead divides d_model_out
		effective_nhead = nhead
		if self.d_model_out % effective_nhead != 0:
			effective_nhead = 1

		encoder_layer = nn.TransformerEncoderLayer(
			d_model=self.d_model_out,
			nhead=effective_nhead,
			dim_feedforward=dim_feedforward,
			batch_first=True
		)
		self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=1)

	def forward(self, x: Tensor) -> Tensor:
		# x shape: [batch, in_features]
		if self.use_sequence:
			# Reshape to [batch, sequence_length, d_model_in]
			x = x.view(x.size(0), self.sequence_length, self.d_model_in)
		else:
			# Add sequence dimension: [batch, 1, in_features]
			x = x.unsqueeze(1)

		x = self.input_proj(x)
		x = self.transformer(x)

		if self.use_sequence:
			# Flatten back to [batch, out_features]
			x = x.reshape(x.size(0), self.out_features)
		else:
			# Remove sequence dimension: [batch, d_model_out]
			x = x.squeeze(1)
		return x


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
		layer_types: Optional[List[str]] = None,
		sequence_length: int = 1,
	):
		super().__init__()
		self.input_size = input_size
		self.output_size = output_size
		self.layer_types = layer_types
		self.sequence_length = sequence_length
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
			layers.append(self._create_layer(input_size, output_size, 0))
		else:
			# If widths list is shorter than n-1, pad it with the last width
			while len(widths) < (n - 1):
				widths.append(widths[-1])
			# If widths list is longer than n-1, truncate it
			widths = widths[:n-1]

		# First hidden layer: input -> widths[0]
		first_layer = self._create_layer(input_size, widths[0], 0)
		layers.append(first_layer)
		# Add LayerNorm after the very first linear layer if model is massive
		if input_size > 5000:
			layers.append(nn.LayerNorm(widths[0]))

		# Peek at first hidden layer activation
		sample_act = activation_factory()
		from . import SigmoidRange, xavier_init_weights

		layers.append(sample_act)

		# Internal hidden layers: widths[i] -> widths[i+1]
		for i in range(len(widths) - 1):
			next_layer = self._create_layer(widths[i], widths[i+1], i + 1)
			layers.append(next_layer)
			# Add LayerNorm for massive models to prevent activation explosion/vanishing
			if input_size > 5000:
				layers.append(nn.LayerNorm(widths[i+1]))
			layers.append(activation_factory())

		# Final output layer: widths[-1] -> output
		last_layer = self._create_layer(widths[-1], output_size, n - 1)
		layers.append(last_layer)

		# Add a tiny bias if Xavier initialized to prevent all-zeros initially
		if isinstance(sample_act, (nn.Sigmoid, nn.Tanh, SigmoidRange)):
			if hasattr(last_layer, 'bias') and last_layer.bias is not None:
				nn.init.uniform_(last_layer.bias, -1e-4, 1e-4)
		else:
			# For ReLU and others, ensure we don't start with all-zero bias if it helps
			if hasattr(last_layer, 'bias') and last_layer.bias is not None:
				nn.init.uniform_(last_layer.bias, -1e-4, 1e-4)

		if final_activation is not None:
			fa = final_activation()
			layers.append(fa)

		self.net = nn.Sequential(*layers)

		# Apply Xavier initialization if sigmoid, tanh or sigmoid_range is used
		# We check if activation_factory produces one of these
		if isinstance(sample_act, (nn.Sigmoid, nn.Tanh, SigmoidRange)):
			self.apply(xavier_init_weights)
			# Re-apply the tiny bias noise because self.apply(xavier_init_weights) zeroed it
			if hasattr(last_layer, 'bias') and last_layer.bias is not None:
				nn.init.uniform_(last_layer.bias, -1e-4, 1e-4)
		else:
			# For ReLU and others, we already applied the tiny bias above.
			# But we want to ensure it's not zeroed if any other initialization is added here.
			pass

	def _create_layer(self, in_features: int, out_features: int, layer_index: int) -> nn.Module:
		if self.layer_types:
			# If layer_types is shorter than needed, use the last one for subsequent layers
			type_idx = min(layer_index, len(self.layer_types) - 1)
			ltype = self.layer_types[type_idx].lower()
		else:
			ltype = "linear"

		if ltype == "linear":
			return nn.Linear(in_features, out_features)
		elif ltype == "sparse-structured":
			return SparseLinear(in_features, out_features)
		elif ltype == "sparse-random":
			# Using a default probability of 0.1 for sparse-random if not specified
			# The issue didn't specify how to pass parameters to these types
			return RandomPercentSparseLinear(in_features, out_features, connection_probability=0.1)
		elif ltype == "transformer":
			return TransformerLayer(in_features, out_features, sequence_length=self.sequence_length)
		else:
			# Fallback to linear
			return nn.Linear(in_features, out_features)

	def _is_linear_like(self, layer: nn.Module) -> bool:
		return isinstance(layer, (nn.Linear, SparseLinear, TransformerLayer))

	def forward(self, x: Tensor) -> Tensor:
		if x.dim() == 1:
			x = x.unsqueeze(0)
		return self.net(x)
