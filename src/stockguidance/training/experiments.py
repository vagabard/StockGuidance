from dataclasses import dataclass
from pathlib import Path

from stockguidance.datasets.training import TrainingDataset
from stockguidance.training.configuration import TrainingConfig
from stockguidance.training.result import TrainingResult
from stockguidance.training.service import train_model


@dataclass(frozen=True)
class ExperimentMember:
	configuration: TrainingConfig
	artifact_path: Path


def run_experiment(dataset: TrainingDataset, members: tuple[ExperimentMember, ...], *, evaluation: TrainingDataset | None = None) -> tuple[TrainingResult, ...]:
	if not isinstance(members, tuple) or not members or any(not isinstance(member, ExperimentMember) for member in members):
		raise ValueError("Experiments require explicit immutable members")
	if len({member.artifact_path.resolve() for member in members}) != len(members):
		raise ValueError("Experiment members must have distinct artifact destinations")
	return tuple(train_model(dataset, member.configuration, member.artifact_path, evaluation=evaluation) for member in members)
