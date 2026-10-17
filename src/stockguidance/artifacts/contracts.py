from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date, require_text
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.models.specification import ModelSpec
from stockguidance.targets.specification import TargetSpec


@dataclass(frozen=True)
class ModelSemantics:
	source: SourceIdentity
	features: FeatureSpec
	target: TargetSpec
	calendar_id: str
	calendar_version: str
	execution_assumption: str = "pre_close_decision_same_close_fill"
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity) or not isinstance(self.features, FeatureSpec) or not isinstance(self.target, TargetSpec):
			raise ValueError("Inference semantics require exact source, feature and target specifications")
		if self.source.adjustment is not self.features.adjustment or self.source.interval is not self.features.interval:
			raise ValueError("Source and feature semantics disagree")
		require_text(self.calendar_id, "calendar ID")
		require_text(self.calendar_version, "calendar version")
		if not isinstance(self.availability_policy, AvailabilityPolicy):
			raise ValueError("Availability policy must be typed")
		self.availability_policy.validate_features(self.features)
		if self.execution_assumption != "pre_close_decision_same_close_fill":
			raise ValueError("Unsupported execution assumption")


@dataclass(frozen=True)
class TrainingProvenance:
	training_cutoff: datetime
	information_cutoff: datetime
	first_entry: date
	last_entry: date
	last_exit: date
	last_exit_close_at: datetime
	data_revision: str
	sample_keys: tuple[str, ...]
	excluded_sample_keys: tuple[str, ...]
	seed: int
	completed_epochs: int
	data_snapshot: datetime | None = None

	def __post_init__(self) -> None:
		require_aware_datetime(self.training_cutoff, "training cutoff")
		require_aware_datetime(self.information_cutoff, "information cutoff")
		if self.training_cutoff != self.information_cutoff:
			raise ValueError("The information cutoff must record the full allowed fitting cutoff")
		for value in (self.first_entry, self.last_entry, self.last_exit):
			require_date(value, "training session")
		require_aware_datetime(self.last_exit_close_at, "last training exit close")
		if self.data_snapshot is not None:
			require_aware_datetime(self.data_snapshot, "training data snapshot")
		if not self.first_entry <= self.last_entry < self.last_exit or self.last_exit_close_at > self.training_cutoff:
			raise ValueError("Training session chronology is invalid")
		require_text(self.data_revision, "data revision")
		if not isinstance(self.sample_keys, tuple) or not self.sample_keys or not isinstance(self.excluded_sample_keys, tuple):
			raise ValueError("Training sample identities must be explicit immutable tuples")
		keys = (*self.sample_keys, *self.excluded_sample_keys)
		if any(not isinstance(key, str) or not key.strip() for key in keys) or len(set(keys)) != len(keys):
			raise ValueError("Training sample identities must be nonempty and unique")
		if type(self.seed) is not int or self.seed < 0 or type(self.completed_epochs) is not int or self.completed_epochs <= 0:
			raise ValueError("Training seed/epoch metadata is invalid")


@dataclass(frozen=True)
class ArtifactMetadata:
	semantics: ModelSemantics
	model: ModelSpec
	training: TrainingProvenance
	framework_version: str
	weight_dtype: str = "float32"

	def __post_init__(self) -> None:
		if not isinstance(self.semantics, ModelSemantics) or not isinstance(self.model, ModelSpec) or not isinstance(self.training, TrainingProvenance):
			raise ValueError("Incomplete inference metadata")
		require_text(self.framework_version, "framework version")
		if self.weight_dtype != "float32":
			raise ValueError("Inference weights must be float32")


@dataclass(frozen=True)
class ArtifactReference:
	path: Path
	artifact_id: str

	def __post_init__(self) -> None:
		if not isinstance(self.path, Path):
			raise ValueError("Artifact reference requires an explicit path")
		if len(self.artifact_id) != 64 or any(character not in "0123456789abcdef" for character in self.artifact_id):
			raise ValueError("Artifact identity must be a SHA256 digest")
