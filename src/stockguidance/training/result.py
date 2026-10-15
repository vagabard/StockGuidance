from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from stockguidance.artifacts.contracts import ArtifactReference


class EvaluationKind(StrEnum):
	IN_SAMPLE = "in_sample"
	CHRONOLOGICAL_HOLDOUT = "chronological_holdout"


@dataclass(frozen=True)
class EpochMetrics:
	epoch: int
	training_mae: float
	learning_rate: float


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
