from dataclasses import dataclass
from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.config.validation import require_positive_int
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.serialization import decimal_text
from stockguidance.recommendations.configuration import ArtifactKind


@dataclass(frozen=True)
class RecommendationOptions:
	policy: ModelRefreshPolicy
	artifact: Path
	artifact_id: str | None = None
	artifact_kind: ArtifactKind = ArtifactKind.MODEL
	max_retries: int = 5
	portfolio_state: Path | None = None
	capital: str | None = None
	fee_per_fill: str = "0.00"

	def __post_init__(self) -> None:
		require_positive_int(self.max_retries, "provider retries")
		ExecutionPolicy(decimal_text(self.fee_per_fill))
		if not isinstance(self.policy, ModelRefreshPolicy) or not isinstance(self.artifact_kind, ArtifactKind) or not isinstance(self.artifact, Path):
			raise ValueError("Recommendation requires typed model selection")
		if self.policy is ModelRefreshPolicy.CURRENT:
			if self.artifact_id is None:
				raise ValueError("Current requires an explicit artifact identity")
			ArtifactReference(self.artifact, self.artifact_id)
		elif self.artifact_id is not None or self.artifact_kind is not ArtifactKind.MODEL:
			raise ValueError("Retraining produces one new model")
		if self.capital is not None and (self.portfolio_state is not None or decimal_text(self.capital) <= 0):
			raise ValueError("Supply either positive initial capital or actual portfolio state")
