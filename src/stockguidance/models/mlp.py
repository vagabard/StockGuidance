from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import TYPE_CHECKING, cast

from stockguidance.models.activations import sigmoid_range_type
from stockguidance.models.sparse import random_percent_type, sparse_linear_type
from stockguidance.models.transformer import transformer_layer_type

if TYPE_CHECKING:
	from torch import Tensor, nn


@cache
def mlp_type() -> type[nn.Module]:
	from torch import nn

	class MultiLayerLinearNet(nn.Module):
		def __init__(self, input_size: int, output_size: int, hidden_layers: list[int], activation_factory: Callable[[], nn.Module] | None = None, final_activation: Callable[[], nn.Module] | None = None, num_layers: int = 3, layer_types: list[str] | None = None, sequence_length: int = 1, bias_mode: str = "all", layer_normalization: bool = True, activation_factories: list[Callable[[], nn.Module]] | tuple[Callable[[], nn.Module], ...] | None = None) -> None:
			super().__init__()
			self.input_size, self.output_size = input_size, output_size
			self.num_layers, self.layer_types = num_layers, layer_types
			self.sequence_length, self.bias_mode = sequence_length, bias_mode
			self.layer_normalization = layer_normalization
			activation_factory = activation_factory or nn.ReLU
			hidden_activation_factories = list(activation_factories) if activation_factories is not None else [activation_factory] * max(0, num_layers - 1)
			if len(hidden_activation_factories) != max(0, num_layers - 1):
				raise ValueError("Hidden activation count must match hidden layer count")
			widths = [width if width > 0 else input_size for width in hidden_layers] or [input_size]
			widths.extend([widths[-1]] * max(0, num_layers - 1 - len(widths)))
			widths = widths[:max(0, num_layers - 1)]
			dimensions = [input_size, *widths, output_size]
			layers: list[nn.Module] = []
			for layer_index, (input_width, output_width) in enumerate(zip(dimensions, dimensions[1:])):
				layers.append(self._create_layer(input_width, output_width, layer_index))
				if layer_index < num_layers - 1:
					if layer_normalization and input_size > 5000:
						layers.append(nn.LayerNorm(output_width))
					layers.append(hidden_activation_factories[layer_index]())
			if final_activation is not None:
				layers.append(final_activation())
			self.net = nn.Sequential(*layers)
			self._initialize_linear_layers(hidden_activation_factories, sigmoid_range_type(), activation_factory)

		def _initialize_linear_layers(self, activation_factories: list[Callable[[], nn.Module]], sigmoid_range_type: type[nn.Module], fallback_factory: Callable[[], nn.Module]) -> None:
			linear_index = 0
			for layer in self.net:
				if not isinstance(layer, nn.Linear) or isinstance(layer, random_percent_type()):
					continue
				factory = activation_factories[min(linear_index, len(activation_factories) - 1)] if activation_factories else fallback_factory
				sample_act = factory()
				family = self._resolve_init_family(sample_act, sigmoid_range_type)
				if family == "xavier":
					nn.init.xavier_uniform_(layer.weight)
				elif family == "kaiming_leaky_relu":
					nn.init.kaiming_uniform_(layer.weight, a=float(getattr(sample_act, "negative_slope", 0.01)), mode="fan_in", nonlinearity="leaky_relu")
				else:
					nn.init.kaiming_uniform_(layer.weight, a=0.0, mode="fan_in", nonlinearity="relu")
				if layer.bias is not None:
					nn.init.zeros_(layer.bias)
				linear_index += 1

		def _resolve_init_family(self, sample_act: nn.Module, sigmoid_range_type: type[nn.Module]) -> str:
			if isinstance(sample_act, (nn.Sigmoid, nn.Tanh, nn.Tanhshrink, sigmoid_range_type)):
				return "xavier"
			if isinstance(sample_act, nn.LeakyReLU):
				return "kaiming_leaky_relu"
			return "kaiming_relu" if isinstance(sample_act, (nn.ReLU, nn.ReLU6, nn.GELU, nn.SiLU, nn.ELU, nn.CELU)) else "xavier"

		def _create_layer(self, in_features: int, out_features: int, layer_index: int) -> nn.Module:
			layer_type = self.layer_types[min(layer_index, len(self.layer_types) - 1)] if self.layer_types else "linear"
			is_input, is_output = layer_index == 0, layer_index == self.num_layers - 1
			use_bias = {"none": False, "internal": not is_input and not is_output, "noinput": not is_input, "nooutput": not is_output, "input": is_input, "output": is_output, "all": True}[self.bias_mode]
			if layer_type == "linear":
				return nn.Linear(in_features, out_features, bias=use_bias)
			if layer_type == "sparse-structured":
				return sparse_linear_type()(in_features, out_features, bias=use_bias)
			if layer_type in ("sparse-random", "random-percent"):
				return random_percent_type()(in_features, out_features, connection_probability=0.1 if layer_type == "sparse-random" else 0.4, bias=use_bias)
			if layer_type == "transformer":
				return transformer_layer_type()(in_features, out_features, sequence_length=self.sequence_length)
			raise ValueError("Unsupported layer type")

		def forward(self, inputs: Tensor) -> Tensor:
			return cast("Tensor", self.net(inputs.unsqueeze(0) if inputs.dim() == 1 else inputs))

	return MultiLayerLinearNet
