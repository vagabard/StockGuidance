from dataclasses import dataclass
from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.serialization import decimal_text
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.recommendations.jobs import RecommendationConfiguration
from stockguidance.recommendations.options import RecommendationOptions
from stockguidance.reporting.recommendations import recommendation_payload
from stockguidance.reporting.simulation import simulation_payload
from stockguidance.reporting.training import training_payload
from stockguidance.simulation.configuration import EvaluationPolicy
from stockguidance.simulation.jobs import SimulationConfiguration
from stockguidance.simulation.options import SimulationOptions
from stockguidance.training.jobs import TrainingJob, execute_job
from stockguidance.workflows.recommendation_runtime import assemble_and_recommend
from stockguidance.workflows.simulation_runtime import assemble_and_simulate


@dataclass(frozen=True)
class ProducedArtifact:
	step: str


@dataclass(frozen=True)
class TrainingTask:
	job: TrainingJob
	artifact: Path
	checkpoint: Path | None = None
	resume: Path | None = None


@dataclass(frozen=True)
class RecommendationTask:
	configuration: RecommendationConfiguration
	policy: ModelRefreshPolicy
	artifact: ArtifactReference | ProducedArtifact | Path
	artifact_kind: ArtifactKind = ArtifactKind.MODEL
	portfolio_state: Path | None = None
	capital: str | None = None
	fee_per_fill: str = "0.00"

	def __post_init__(self) -> None:
		if not isinstance(self.configuration, RecommendationConfiguration) or not isinstance(self.policy, ModelRefreshPolicy) or not isinstance(self.artifact_kind, ArtifactKind):
			raise ValueError("Recommendation tasks require typed configuration")
		if (self.configuration.retraining is not None) != (self.policy is ModelRefreshPolicy.RETRAIN):
			raise ValueError("Recommendation policy and training configuration differ")
		if isinstance(self.artifact, Path) != (self.policy is ModelRefreshPolicy.RETRAIN):
			raise ValueError("Current requires a reference; retrain requires an output path")
		if isinstance(self.artifact, ProducedArtifact) and self.artifact_kind is not ArtifactKind.MODEL:
			raise ValueError("A producing step supplies one model artifact")
		ExecutionPolicy(decimal_text(self.fee_per_fill))
		if self.capital is not None and (self.portfolio_state is not None or decimal_text(self.capital) <= 0):
			raise ValueError("Supply either positive initial capital or actual portfolio state")


@dataclass(frozen=True)
class SimulationTask:
	configuration: SimulationConfiguration
	policy: EvaluationPolicy
	artifact: ArtifactReference | ProducedArtifact | Path
	artifact_kind: ArtifactKind = ArtifactKind.MODEL

	def __post_init__(self) -> None:
		if not isinstance(self.configuration, SimulationConfiguration) or not isinstance(self.policy, EvaluationPolicy) or not isinstance(self.artifact_kind, ArtifactKind):
			raise ValueError("Simulation tasks require typed configuration")
		if (self.configuration.retraining is not None) != (self.policy is EvaluationPolicy.CHRONOLOGICAL_RETRAIN):
			raise ValueError("Evaluation policy and training configuration differ")
		if isinstance(self.artifact, Path) != (self.policy is EvaluationPolicy.CHRONOLOGICAL_RETRAIN):
			raise ValueError("Fixed evaluation requires a reference; retrain requires an output directory")
		if isinstance(self.artifact, ProducedArtifact) and self.artifact_kind is not ArtifactKind.MODEL:
			raise ValueError("A producing step supplies one model artifact")


type BatchTask = TrainingTask | RecommendationTask | SimulationTask


@dataclass(frozen=True)
class TaskOutcome:
	payload: dict[str, object]
	exit_code: int
	artifact: ArtifactReference | None = None

	def __post_init__(self) -> None:
		if type(self.exit_code) is not int or self.exit_code not in (0, 1, 2) or (self.exit_code == 0 and self.payload.get("successful") is not True):
			raise ValueError("Task status and report success disagree")


def execute_task(task: BatchTask, artifacts: dict[str, ArtifactReference]) -> TaskOutcome:
	if isinstance(task, TrainingTask):
		result = execute_job(task.job, task.artifact, checkpoint_path=task.checkpoint, resume_from=task.resume)
		return TaskOutcome(training_payload(result), 0, result.artifact)
	selection = artifacts[task.artifact.step] if isinstance(task.artifact, ProducedArtifact) else task.artifact
	reference = selection if isinstance(selection, ArtifactReference) else None
	path = selection.path if isinstance(selection, ArtifactReference) else selection
	if isinstance(task, RecommendationTask):
		options = RecommendationOptions(task.policy, path, None if reference is None else reference.artifact_id, task.artifact_kind, portfolio_state=task.portfolio_state, capital=task.capital, fee_per_fill=task.fee_per_fill)
		run = assemble_and_recommend(task.configuration, options)
		produced = run.training.artifact if run.training is not None else reference
		return TaskOutcome(recommendation_payload(run), 0, produced)
	options_simulation = SimulationOptions(task.policy, path if reference is not None else None, None if reference is None else reference.artifact_id, task.artifact_kind, path if reference is None else None)
	simulation = assemble_and_simulate(task.configuration, options_simulation)
	return TaskOutcome(simulation_payload(simulation), 0 if simulation.successful and simulation.complete else 2 if simulation.successful else 1)
