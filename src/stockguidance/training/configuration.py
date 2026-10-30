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


class LossObjective(StrEnum):
	MAE = "mae"
	RMSE = "rmse"
	PAIRWISE_RANKING = "pairwise_ranking"


@dataclass(frozen=True)
class LossSpec:
	"""Versioned objective selection for target-space training."""

	objective: LossObjective = LossObjective.MAE
	version: str = "mae-v1"
	pair_block_size: int | None = None

	def __post_init__(self) -> None:
		require_enum(self.objective, LossObjective, "loss objective")
		if self.version not in ("mae-v1", "loss-v2"):
			raise ValueError("Unsupported loss specification version")
		if self.objective is LossObjective.MAE and self.version == "mae-v1" and self.pair_block_size is not None:
			raise ValueError("The v1 MAE loss does not accept pairwise settings")
		if self.objective is not LossObjective.MAE and self.version != "loss-v2":
			raise ValueError("RMSE and pairwise ranking require loss-v2")
		if self.objective is LossObjective.PAIRWISE_RANKING:
			if self.pair_block_size is None:
				object.__setattr__(self, "pair_block_size", 128)
			require_positive_int(self.pair_block_size, "pair block size")
		elif self.pair_block_size is not None:
			require_positive_int(self.pair_block_size, "pair block size")

	def to_mapping(self) -> dict[str, object]:
		return {"objective": self.objective.value, "version": self.version, "pair_block_size": self.pair_block_size}


@dataclass(frozen=True)
class TrainingConfig:
	model: ModelSpec = ModelSpec()
	loss: LossSpec = LossSpec()
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
		if not isinstance(self.loss, LossSpec):
			raise ValueError("Training requires an explicit loss specification")
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
