from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date, require_text
from stockguidance.features.specification import FeatureSpec, FeatureSpecV2, FeatureSpecification
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.models.specification import ModelSpec
from stockguidance.preprocessing import FittedPreprocessing
from stockguidance.targets.specification import TargetSpec, TargetSpecification

if TYPE_CHECKING:
	from stockguidance.training.configuration import LossSpec


@dataclass(frozen=True)
class ModelSemantics:
	source: SourceIdentity
	features: FeatureSpecification
	target: TargetSpecification
	calendar_id: str
	calendar_version: str
	execution_assumption: str = "pre_close_decision_same_close_fill"
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity) or not isinstance(self.features, (FeatureSpec, FeatureSpecV2)) or not isinstance(self.target, TargetSpec):
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
	preprocessing: FittedPreprocessing | None = None
	loss: "LossSpec | None" = None
	universe: "ArtifactUniverse | None" = None

	def __post_init__(self) -> None:
		if not isinstance(self.semantics, ModelSemantics) or not isinstance(self.model, ModelSpec) or not isinstance(self.training, TrainingProvenance):
			raise ValueError("Incomplete inference metadata")
		require_text(self.framework_version, "framework version")
		if self.weight_dtype != "float32":
			raise ValueError("Inference weights must be float32")
		if self.preprocessing is not None and not isinstance(self.preprocessing, FittedPreprocessing):
			raise ValueError("Artifact preprocessing must be fitted state")
		if self.preprocessing is not None:
			self.preprocessing.validate_for(self.semantics.features, self.semantics.target)
		if self.loss is not None and not callable(getattr(self.loss, "to_mapping", None)):
			raise ValueError("Artifact loss must be typed")
		if self.universe is not None and not isinstance(self.universe, ArtifactUniverse):
			raise ValueError("Artifact universe must be typed")
		if self.universe is not None and self.universe.ticker_ids != self.semantics.features.ticker_ids:
			raise ValueError("Artifact universe order must match feature tensor order")

	@property
	def versioned(self) -> bool:
		return any((
			self.semantics.features.version == "configured-features-v2",
			self.semantics.target.version != "raw_return-v1",
			self.model.implementation_version == "stockguidance-models-v2",
			self.preprocessing is not None,
			self.loss is not None and getattr(self.loss, "version", None) != "mae-v1",
			self.universe is not None,
		))


@dataclass(frozen=True)
class ArtifactReference:
	path: Path
	artifact_id: str

	def __post_init__(self) -> None:
		if not isinstance(self.path, Path):
			raise ValueError("Artifact reference requires an explicit path")
		if len(self.artifact_id) != 64 or any(character not in "0123456789abcdef" for character in self.artifact_id):
			raise ValueError("Artifact identity must be a SHA256 digest")


@dataclass(frozen=True)
class ArtifactUniverse:
	"""Stable output mapping frozen into a configurable artifact."""

	ticker_ids: tuple[int, ...]
	ticker_labels: tuple[str, ...]
	selection_mode: str
	evidence_revision: str

	def __post_init__(self) -> None:
		if not self.ticker_ids or len(self.ticker_ids) != len(self.ticker_labels) or len(set(self.ticker_ids)) != len(self.ticker_ids):
			raise ValueError("Artifact universe requires one unique label per ticker ID")
		if any(type(value) is not int or value <= 0 for value in self.ticker_ids) or any(not isinstance(value, str) or not value.strip() for value in self.ticker_labels):
			raise ValueError("Artifact universe identity values are invalid")
		if not isinstance(self.selection_mode, str) or not self.selection_mode.strip() or not isinstance(self.evidence_revision, str) or not self.evidence_revision.strip():
			raise ValueError("Artifact universe evidence is required")

	def to_mapping(self) -> dict[str, object]:
		return {
			"schema": "artifact-universe-v1",
			"ticker_ids": list(self.ticker_ids),
			"ticker_labels": list(self.ticker_labels),
			"selection_mode": self.selection_mode,
			"evidence_revision": self.evidence_revision,
		}
