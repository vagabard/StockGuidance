from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.market_data.availability import AvailabilityPolicy


class EvaluationKind(StrEnum):
	IN_SAMPLE = "in_sample"
	CHRONOLOGICAL_HOLDOUT = "chronological_holdout"


@dataclass(frozen=True)
class EpochMetrics:
	epoch: int
	training_mae: float
	learning_rate: float
	objective_loss: float | None = None
	training_rmse: float | None = None
	non_tied_pair_count: int = 0
	ordering_accuracy: float | None = None


@dataclass(frozen=True)
class ExcludedSample:
	entry_session: date
	feature_state: str
	target_state: str
	price_states: tuple[str, ...]


@dataclass(frozen=True)
class TrainingResult:
	artifact: ArtifactReference
	completed_epochs: int
	training_samples: int
	excluded_training: tuple[ExcludedSample, ...]
	epochs: tuple[EpochMetrics, ...]
	training_mae: float
	evaluation_kind: EvaluationKind
	evaluation_mae: float | None
	evaluation_samples: int
	excluded_evaluation: tuple[ExcludedSample, ...]
	evaluation_cutoff: datetime | None
	evaluation_revision: str | None
	device: str
	precision: str
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT
	data_snapshot: datetime | None = None
	data_revision: str | None = None
	loss_objective: str = "mae"
	training_objective_loss: float | None = None
	training_rmse: float | None = None
	training_non_tied_pair_count: int = 0
	training_ordering_accuracy: float | None = None
	evaluation_objective_loss: float | None = None
	evaluation_rmse: float | None = None
	evaluation_non_tied_pair_count: int = 0
	evaluation_ordering_accuracy: float | None = None
	raw_return_metrics_available: bool = False
	training_raw_return_mae: float | None = None
	training_raw_return_rmse: float | None = None
	evaluation_raw_return_mae: float | None = None
	evaluation_raw_return_rmse: float | None = None
	loss_version: str = "mae-v1"
	target_metric_units: str = "fractional_return"
	objective_loss_units: str = "target_units"
