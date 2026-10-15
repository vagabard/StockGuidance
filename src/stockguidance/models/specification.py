from dataclasses import dataclass
from enum import StrEnum

from stockguidance.config.validation import require_enum, require_positive_int


class Architecture(StrEnum):
	MLP = "mlp"
	CONV_TRANSFORMER = "conv_trans"


class Activation(StrEnum):
	RELU = "relu"
	GELU = "gelu"
	TANH = "tanh"
	SIGMOID = "sigmoid"
	SIGMOID_RANGE = "sigmoid_range"
	SILU = "silu"
	LEAKY_RELU = "leaky_relu"
	ELU = "elu"
	TANHSHRINK = "tanhshrink"


class LayerType(StrEnum):
	LINEAR = "linear"
	SPARSE_STRUCTURED = "sparse-structured"
	SPARSE_RANDOM = "sparse-random"
	RANDOM_PERCENT = "random-percent"
	TRANSFORMER = "transformer"


class BiasMode(StrEnum):
	NONE = "none"
	INTERNAL = "internal"
	NO_INPUT = "noinput"
	NO_OUTPUT = "nooutput"
	INPUT = "input"
	OUTPUT = "output"
	ALL = "all"


@dataclass(frozen=True)
class ModelSpec:
	architecture: Architecture = Architecture.MLP
	hidden_widths: tuple[int, ...] = (64, 64, 64)
	activation: Activation = Activation.LEAKY_RELU
	layer_types: tuple[LayerType, ...] = (LayerType.LINEAR,)
	bias: BiasMode = BiasMode.ALL
	layer_normalization: bool = True
	final_activation: str = "none"
	implementation_version: str = "stockguidance-models-v1"

	def __post_init__(self) -> None:
		require_enum(self.architecture, Architecture, "architecture")
		require_enum(self.activation, Activation, "activation")
		require_enum(self.bias, BiasMode, "bias")
		if not isinstance(self.hidden_widths, tuple):
			raise ValueError("Hidden widths must be an explicit tuple")
		for width in self.hidden_widths:
			require_positive_int(width, "hidden width")
		if not isinstance(self.layer_types, tuple) or not self.layer_types or len(self.layer_types) > self.num_layers:
			raise ValueError("Layer types must be a nonempty tuple no longer than the network")
		for layer in self.layer_types:
			require_enum(layer, LayerType, "layer type")
		if type(self.layer_normalization) is not bool:
			raise ValueError("Layer normalization must be boolean")
		if self.final_activation != "none":
			raise ValueError("Raw close-to-close return regression requires an unrestricted output")
		if self.implementation_version != "stockguidance-models-v1":
			raise ValueError("Unsupported model implementation version")

	@property
	def num_layers(self) -> int:
		return len(self.hidden_widths) + 1
