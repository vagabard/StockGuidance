from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.validation import require_aware_datetime
from stockguidance.database.repositories.closes import CloseRepository
from stockguidance.datasets.training import build_training_dataset
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.specification import TargetSpec
from stockguidance.training.configuration import TrainingConfig
from stockguidance.training.result import TrainingResult
from stockguidance.training.service import train_model


@dataclass(frozen=True)
class HistoryTrainingRequest:
	decisions: tuple[DecisionTiming, ...]
	training_cutoff: datetime
	features: FeatureSpec
	target: TargetSpec
	configuration: TrainingConfig
	artifact_path: Path
	evaluation_decisions: tuple[DecisionTiming, ...] = ()
	evaluation_cutoff: datetime | None = None
	checkpoint_path: Path | None = None
	resume_from: Path | None = None

	def __post_init__(self) -> None:
		require_aware_datetime(self.training_cutoff, "training cutoff")
		if not isinstance(self.features, FeatureSpec) or not isinstance(self.target, TargetSpec) or not isinstance(self.configuration, TrainingConfig) or not isinstance(self.artifact_path, Path):
			raise ValueError("History training requires typed specifications, settings and an explicit artifact path")
		if not isinstance(self.decisions, tuple) or not self.decisions or not isinstance(self.evaluation_decisions, tuple):
			raise ValueError("History training requires explicit immutable decisions")
		if any(not isinstance(timing, DecisionTiming) or timing.decision_cutoff > self.training_cutoff for timing in self.decisions):
			raise ValueError("Training decisions must precede the fitting cutoff")
		if any(right.entry_session.sequence <= left.entry_session.sequence for left, right in zip(self.decisions, self.decisions[1:])):
			raise ValueError("Training decisions must be unique and chronological")
		if bool(self.evaluation_decisions) != (self.evaluation_cutoff is not None):
			raise ValueError("Evaluation requires both decisions and a label cutoff")
		if self.evaluation_cutoff is not None:
			require_aware_datetime(self.evaluation_cutoff, "evaluation cutoff")
			if any(not isinstance(timing, DecisionTiming) or not self.training_cutoff <= timing.decision_cutoff <= self.evaluation_cutoff or timing.entry_session.sequence <= self.decisions[-1].entry_session.sequence for timing in self.evaluation_decisions):
				raise ValueError("Evaluation decisions must follow the fitting period and cutoff")
			if any(right.entry_session.sequence <= left.entry_session.sequence for left, right in zip(self.evaluation_decisions, self.evaluation_decisions[1:])):
				raise ValueError("Evaluation decisions must be unique and chronological")


def train_from_history(request: HistoryTrainingRequest, boundary: HistoryBoundary, calendar: SessionCalendar, publication: DailyPublicationPolicy, repository: CloseRepository) -> TrainingResult:
	dataset = build_training_dataset(request.decisions, request.training_cutoff, request.features, request.target, boundary, calendar, publication, repository)
	evaluation = None if request.evaluation_cutoff is None else build_training_dataset(request.evaluation_decisions, request.evaluation_cutoff, request.features, request.target, boundary, calendar, publication, repository)
	return train_model(dataset, request.configuration, request.artifact_path, evaluation=evaluation, checkpoint_path=request.checkpoint_path, resume_from=request.resume_from)
