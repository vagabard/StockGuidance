from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.reporting.storage import write_report
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.workflows.batch_tasks import BatchTask, ProducedArtifact, RecommendationTask, TaskOutcome, TrainingTask, execute_task


@dataclass(frozen=True)
class BatchStep:
	name: str
	task: BatchTask
	report: Path


@dataclass(frozen=True)
class BatchPlan:
	steps: tuple[BatchStep, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.steps, tuple) or not self.steps or any(not isinstance(step, BatchStep) for step in self.steps):
			raise ValueError("A batch requires explicit immutable steps")
		names: set[str] = set()
		outputs: set[Path] = set()
		producers: set[str] = set()
		for step in self.steps:
			if re.fullmatch(r"[A-Za-z0-9_-]+", step.name) is None or step.name in names or not isinstance(step.report, Path):
				raise ValueError("Batch step names and report paths must be explicit and unique")
			selection = step.task.artifact
			if isinstance(selection, ProducedArtifact) and selection.step not in producers:
				raise ValueError("Artifact dependencies must name an earlier producing step")
			paths = [step.report]
			if isinstance(selection, Path):
				paths.append(selection)
			if isinstance(step.task, TrainingTask) and step.task.checkpoint is not None:
				paths.append(step.task.checkpoint)
			for path in paths:
				if path.resolve() in outputs:
					raise ValueError("Batch outputs must have distinct explicit destinations")
				outputs.add(path.resolve())
			names.add(step.name)
			if isinstance(step.task, TrainingTask) or isinstance(step.task, RecommendationTask) and step.task.policy is ModelRefreshPolicy.RETRAIN:
				producers.add(step.name)


class TaskExecutor(Protocol):
	def __call__(self, task: BatchTask, artifacts: dict[str, ArtifactReference]) -> TaskOutcome: ...


@dataclass(frozen=True)
class BatchResult:
	steps: tuple[dict[str, object], ...]
	exit_code: int

	def payload(self) -> dict[str, object]:
		return {"schema": "stockguidance-batch-result-1", "successful": self.exit_code == 0, "exit_code": self.exit_code, "steps": list(self.steps)}


def run_batch(plan: BatchPlan, *, executor: TaskExecutor = execute_task) -> BatchResult:
	artifacts: dict[str, ArtifactReference] = {}
	completed: list[dict[str, object]] = []
	for step in plan.steps:
		try:
			outcome = executor(step.task, dict(artifacts))
			if outcome.exit_code not in (0, 1, 2):
				raise ValueError("Invalid task exit status")
			write_report(step.report, outcome.payload)
			completed.append({"name": step.name, "exit_code": outcome.exit_code, "report": str(step.report), "artifact_id": None if outcome.artifact is None else outcome.artifact.artifact_id})
			if outcome.exit_code:
				return BatchResult(tuple(completed), outcome.exit_code)
			if outcome.artifact is not None:
				artifacts[step.name] = outcome.artifact
		except Exception:
			completed.append({"name": step.name, "exit_code": 1, "failure": "Required batch execution or report persistence failed"})
			return BatchResult(tuple(completed), 1)
	return BatchResult(tuple(completed), 0)
