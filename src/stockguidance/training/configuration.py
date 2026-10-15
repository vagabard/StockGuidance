from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from stockguidance.config.validation import require_enum, require_positive_int
from stockguidance.models.specification import ModelSpec


class OptimizerChoice(StrEnum):
	ADAM = "adam"
	ADAMW = "adamw"
	SGD = "sgd"


class DeviceChoice(StrEnum):
	AUTO = "auto"
	CPU = "cpu"
	CUDA = "cuda"


class Precision(StrEnum):
	AUTO = "auto"
	FP32 = "fp32"
	FP16 = "fp16"
	BF16 = "bf16"


@dataclass(frozen=True)
class TrainingConfig:
	model: ModelSpec = ModelSpec()
	epochs: int = 80
	batch_size: int = 256
	learning_rate: float = 1e-7
	learning_rate_gamma: float = 1.0
	optimizer: OptimizerChoice = OptimizerChoice.ADAMW
	device: DeviceChoice = DeviceChoice.AUTO
	precision: Precision = Precision.FP32
	seed: int = 0
	shuffle: bool = False
	num_workers: int = 0
	pin_memory: bool = False

	def __post_init__(self) -> None:
		if not isinstance(self.model, ModelSpec):
			raise ValueError("Training requires an explicit model specification")
		for name in ("epochs", "batch_size"):
			require_positive_int(getattr(self, name), name)
		for name in ("learning_rate", "learning_rate_gamma"):
			value = getattr(self, name)
			if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value) or value <= 0:
				raise ValueError("Learning rate and decay must be finite and positive")
		for value, enum_type, name in ((self.optimizer, OptimizerChoice, "optimizer"), (self.device, DeviceChoice, "device"), (self.precision, Precision, "precision")):
			require_enum(value, enum_type, name)
		if type(self.seed) is not int or not 0 <= self.seed < 2 ** 63 or type(self.num_workers) is not int or self.num_workers < 0:
			raise ValueError("Seed and worker count must be nonnegative supported integers")
		if type(self.shuffle) is not bool or type(self.pin_memory) is not bool:
			raise ValueError("Shuffle and pin-memory settings must be boolean")
