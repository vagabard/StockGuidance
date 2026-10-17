from dataclasses import dataclass
from datetime import datetime

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.validation import require_aware_datetime
from stockguidance.database.repositories.closes import CloseRepository
from stockguidance.datasets.building import build_sample
from stockguidance.datasets.sample import Sample
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.specification import TargetSpec


@dataclass(frozen=True)
class TrainingDataset:
	samples: tuple[Sample, ...]
	training_cutoff: datetime
	data_snapshot: datetime | None = None

	def __post_init__(self) -> None:
		require_aware_datetime(self.training_cutoff, "training cutoff")
		if self.data_snapshot is not None:
			require_aware_datetime(self.data_snapshot, "data snapshot")
		if self.data_snapshot is not None and self.data_snapshot < self.training_cutoff:
			raise ValueError("Training data snapshot must include the fitting cutoff")
		if not isinstance(self.samples, tuple) or any(not isinstance(sample, Sample) or sample.label_cutoff != self.training_cutoff for sample in self.samples):
			raise ValueError("Training samples must use one explicit training cutoff")
		if self.samples:
			first = self.samples[0]
			if any(sample.source != first.source or sample.features.specification != first.features.specification or sample.target_specification != first.target_specification or sample.calendar_version != first.calendar_version for sample in self.samples):
				raise ValueError("Training samples have incompatible source/feature/target semantics")
		if any(right.timing.entry_session.sequence <= left.timing.entry_session.sequence for left, right in zip(self.samples, self.samples[1:])):
			raise ValueError("Training samples must be unique and chronological")
		if self.samples and self.data_snapshot is not None and any(sample.data_snapshot != self.data_snapshot for sample in self.samples):
			raise ValueError("Training samples must use one data snapshot")

	@property
	def eligible(self) -> tuple[Sample, ...]:
		return tuple(sample for sample in self.samples if sample.training_eligible)

	@property
	def excluded(self) -> tuple[Sample, ...]:
		return tuple(sample for sample in self.samples if not sample.training_eligible)


def build_training_dataset(
	decisions: tuple[DecisionTiming, ...], training_cutoff: datetime,
	specification: FeatureSpec, target: TargetSpec, boundary: HistoryBoundary,
	calendar: SessionCalendar, publication: DailyPublicationPolicy, repository: CloseRepository,
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT, data_snapshot: datetime | None = None,
) -> TrainingDataset:
	require_aware_datetime(training_cutoff, "training cutoff")
	if not isinstance(decisions, tuple) or any(not isinstance(timing, DecisionTiming) or timing.decision_cutoff > training_cutoff for timing in decisions):
		raise ValueError("Training decisions must be immutable and available by the training cutoff")
	if any(right.entry_session.sequence <= left.entry_session.sequence for left, right in zip(decisions, decisions[1:])):
		raise ValueError("Training decisions must be unique and chronological")
	if not isinstance(availability_policy, AvailabilityPolicy):
		raise ValueError("Training requires a typed availability policy")
	query_as_of = availability_policy.query_as_of(training_cutoff, data_snapshot)
	return TrainingDataset(tuple(build_sample(specification, target, timing, boundary, calendar, publication, repository, label_cutoff=training_cutoff, availability_policy=availability_policy, data_snapshot=query_as_of) for timing in decisions), training_cutoff, query_as_of)
