from dataclasses import dataclass
from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.simulation.configuration import EvaluationPolicy


@dataclass(frozen=True)
class SimulationOptions:
	model_policy: EvaluationPolicy
	artifact: Path | None = None
	artifact_id: str | None = None
	artifact_kind: ArtifactKind = ArtifactKind.MODEL
	artifacts_directory: Path | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.model_policy, EvaluationPolicy) or not isinstance(self.artifact_kind, ArtifactKind):
			raise ValueError("Simulation requires typed model selection")
		if self.model_policy is EvaluationPolicy.FIXED:
			if self.artifact is None or self.artifact_id is None or self.artifacts_directory is not None:
				raise ValueError("Fixed evaluation requires one explicit artifact")
			ArtifactReference(self.artifact, self.artifact_id)
		elif self.artifact is not None or self.artifact_id is not None or self.artifacts_directory is None or self.artifact_kind is not ArtifactKind.MODEL:
			raise ValueError("Chronological retraining requires an explicit output directory")
