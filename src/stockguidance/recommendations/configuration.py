from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_date, require_enum, require_positive_int
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.models.policy import ModelRefreshPolicy, ModelSelection
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.targets.specification import TargetSpec
from stockguidance.training.configuration import TrainingConfig


class ArtifactKind(StrEnum):
	MODEL = "model"
	ENSEMBLE = "ensemble"


@dataclass(frozen=True)
class RetrainingPlan:
	decisions: tuple[DecisionTiming, ...]
	configuration: TrainingConfig
	artifact_path: Path

	def __post_init__(self) -> None:
		if not isinstance(self.decisions, tuple) or not self.decisions or any(not isinstance(item, DecisionTiming) for item in self.decisions):
			raise ValueError("Retraining requires explicit historical decisions")
		if any(right.entry_session.sequence <= left.entry_session.sequence for left, right in zip(self.decisions, self.decisions[1:])):
			raise ValueError("Retraining decisions must be unique and chronological")
		if not isinstance(self.configuration, TrainingConfig) or not isinstance(self.artifact_path, Path):
			raise ValueError("Retraining requires typed settings and an explicit destination")


@dataclass(frozen=True)
class RecommendationJob:
	source: SourceConfig
	start: date
	end: date
	entry_date: date
	features: FeatureSpec
	target: TargetSpec
	model: ModelSelection
	allocation: AllocationSpec
	publication: DailyPublicationPolicy
	corrections: CorrectionPolicy
	eligibility: EligibilityPolicy = EligibilityPolicy()
	top_k: int = 5
	current_artifact: ArtifactReference | None = None
	artifact_kind: ArtifactKind = ArtifactKind.MODEL
	retraining: RetrainingPlan | None = None
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT

	def __post_init__(self) -> None:
		for value, expected in ((self.source, SourceConfig), (self.features, FeatureSpec), (self.target, TargetSpec), (self.model, ModelSelection), (self.allocation, AllocationSpec), (self.publication, DailyPublicationPolicy), (self.corrections, CorrectionPolicy), (self.eligibility, EligibilityPolicy)):
			if not isinstance(value, expected):
				raise ValueError("Recommendation jobs require typed configuration")
		for session_date in (self.start, self.end, self.entry_date):
			require_date(session_date, "recommendation session")
		require_enum(self.artifact_kind, ArtifactKind, "artifact kind")
		require_positive_int(self.top_k, "top_k")
		if not isinstance(self.availability_policy, AvailabilityPolicy):
			raise ValueError("Recommendation requires a typed availability policy")
		if self.availability_policy is not AvailabilityPolicy.STRICT:
			raise ValueError("Retrospective research artifacts are not supported for production recommendations")
		if self.start > self.end or self.end > self.entry_date:
			raise ValueError("Coverage must be chronological and end by the entry session")
		if self.features.adjustment is not self.source.identity.adjustment or self.features.interval is not self.source.identity.interval:
			raise ValueError("Recommendation features and source differ")
		if self.model.policy is ModelRefreshPolicy.CURRENT:
			if not isinstance(self.current_artifact, ArtifactReference) or self.current_artifact.artifact_id != self.model.artifact_id or self.retraining is not None:
				raise ValueError("current requires its explicit artifact and cannot request training")
		elif not isinstance(self.retraining, RetrainingPlan) or self.current_artifact is not None or self.artifact_kind is not ArtifactKind.MODEL:
			raise ValueError("retrain requires one typed training plan and uses its returned model artifact")
