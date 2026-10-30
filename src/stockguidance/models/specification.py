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
	final_activation: str | Activation = "none"
	implementation_version: str = "stockguidance-models-v1"
	# These fields are intentionally after the v1 positional fields.  Existing
	# callers and v1 artifact decoding therefore retain their old constructor
	# shape while v2 jobs can opt into explicit hidden-layer controls.
	hidden_activations: tuple[Activation, ...] | None = None
	shared_hidden_activation: Activation | None = None

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
		if self.implementation_version not in ("stockguidance-models-v1", "stockguidance-models-v2"):
			raise ValueError("Unsupported model implementation version")
		if self.hidden_activations is not None:
			if not isinstance(self.hidden_activations, tuple) or len(self.hidden_activations) != len(self.hidden_widths):
				raise ValueError("Per-hidden-layer activations must match hidden widths")
			if any(not isinstance(value, Activation) for value in self.hidden_activations):
				raise ValueError("Per-hidden-layer activations must be typed")
		if self.shared_hidden_activation is not None and not isinstance(self.shared_hidden_activation, Activation):
			raise ValueError("Shared hidden activation must be typed")
		if self.hidden_activations is not None and self.shared_hidden_activation is not None:
			raise ValueError("Choose per-layer activations or a shared hidden activation, not both")
		if self.implementation_version == "stockguidance-models-v1":
			if self.final_activation != "none" or self.hidden_activations is not None or self.shared_hidden_activation is not None:
				raise ValueError("Raw v1 models require unrestricted output and shared hidden activation; use stockguidance-models-v2")
		else:
			if self.final_activation != "none" and not isinstance(self.final_activation, Activation):
				try:
					object.__setattr__(self, "final_activation", Activation(str(self.final_activation)))
				except ValueError:
					raise ValueError("Unsupported final activation") from None
			if self.architecture is Architecture.CONV_TRANSFORMER and (self.final_activation != "none" or self.hidden_activations is not None or self.shared_hidden_activation is not None):
				raise ValueError("Per-layer and final activations are unsupported for the conv_trans architecture")

	@property
	def resolved_hidden_activations(self) -> tuple[Activation, ...]:
		if self.hidden_activations is not None:
			return self.hidden_activations
		if self.shared_hidden_activation is not None:
			return (self.shared_hidden_activation,) * len(self.hidden_widths)
		return (self.activation,) * len(self.hidden_widths)

	@property
	def resolved_final_activation(self) -> Activation | None:
		return None if self.final_activation == "none" else Activation(self.final_activation)

	def to_mapping(self) -> dict[str, object]:
		"""Return the strict serialized model shape for this implementation version."""

		payload: dict[str, object] = {
			"architecture": self.architecture.value,
			"hidden_widths": list(self.hidden_widths),
			"activation": self.activation.value,
			"layer_types": [value.value for value in self.layer_types],
			"bias": self.bias.value,
			"layer_normalization": self.layer_normalization,
			"final_activation": self.final_activation if isinstance(self.final_activation, str) else self.final_activation.value,
			"implementation_version": self.implementation_version,
		}
		if self.implementation_version == "stockguidance-models-v2":
			payload["hidden_activations"] = None if self.hidden_activations is None else [value.value for value in self.hidden_activations]
			payload["shared_hidden_activation"] = None if self.shared_hidden_activation is None else self.shared_hidden_activation.value
		return payload

	@property
	def num_layers(self) -> int:
		return len(self.hidden_widths) + 1
