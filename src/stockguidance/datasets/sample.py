from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_enum, require_text
from stockguidance.datasets.identity import feature_identity, sample_identity
from stockguidance.features.construction import FeatureValues
from stockguidance.features.history_window import WindowState
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.targets.construction import TargetValues
from stockguidance.targets.specification import TargetSpec


class TargetState(StrEnum):
	READY = "ready"
	NOT_REQUESTED = "not_requested"
	NOT_YET_AVAILABLE = "not_yet_available"
	UNVERIFIED = "unverified"
	UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Sample:
	source: SourceIdentity
	timing: DecisionTiming
	features: FeatureValues
	feature_state: WindowState
	target_specification: TargetSpec
	targets: TargetValues
	target_state: TargetState
	label_cutoff: datetime | None
	feature_revision: str
	data_revision: str
	calendar_version: str
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT
	data_snapshot: datetime | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity) or not isinstance(self.timing, DecisionTiming) or not isinstance(self.features, FeatureValues) or not isinstance(self.targets, TargetValues) or not isinstance(self.target_specification, TargetSpec):
			raise ValueError("Invalid sample contracts")
		require_enum(self.feature_state, WindowState, "feature state")
		require_enum(self.target_state, TargetState, "target state")
		require_enum(self.availability_policy, AvailabilityPolicy, "availability policy")
		if self.data_snapshot is not None:
			require_aware_datetime(self.data_snapshot, "data snapshot")
		if self.availability_policy.retrospective and self.data_snapshot is None:
			raise ValueError("Retrospective samples require an explicit data snapshot")
		if self.source.adjustment is not self.features.specification.adjustment or self.source.interval is not self.features.specification.interval:
			raise ValueError("Sample source and feature semantics differ")
		if len(self.targets.raw) != len(self.features.specification.ticker_ids):
			raise ValueError("Sample targets must match the exact ordered ticker universe")
		if self.features.values is not None:
			expected = tuple(range(self.timing.entry_session.sequence - self.features.specification.lookback_sessions, self.timing.entry_session.sequence))
			if self.feature_state is not WindowState.EXPECTED_AVAILABLE or tuple(session.sequence for session in self.features.sessions) != expected or any(session.calendar_id != self.timing.entry_session.calendar_id for session in self.features.sessions):
				raise ValueError("Sample features must use the exact prior-session window")
		if self.label_cutoff is not None:
			require_aware_datetime(self.label_cutoff, "label cutoff")
			if self.label_cutoff < self.timing.decision_cutoff:
				raise ValueError("Training process cannot precede the sample decision")
		if (self.target_state is TargetState.READY) != self.targets.ready or (self.target_state is TargetState.NOT_REQUESTED) != (self.label_cutoff is None):
			raise ValueError("Target state disagrees with numerical availability")
		for name in ("feature_revision", "data_revision", "calendar_version"):
			require_text(getattr(self, name), name)

	@property
	def training_eligible(self) -> bool:
		return self.features.values is not None and self.target_state is TargetState.READY

	@property
	def feature_cache_key(self) -> str:
		return feature_identity(self.source, self.features.specification, self.timing, self.feature_revision, self.calendar_version, self.availability_policy, self.data_snapshot)

	@property
	def cache_key(self) -> str:
		return sample_identity(self.feature_cache_key, self.target_specification, self.label_cutoff, self.data_revision, self.availability_policy, self.data_snapshot)
