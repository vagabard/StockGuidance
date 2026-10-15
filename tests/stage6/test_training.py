from dataclasses import replace
from datetime import datetime, timezone
from math import isfinite

import pytest
import torch

from stockguidance.artifacts.loading import load_artifact
from stockguidance.datasets.training import TrainingDataset, build_training_dataset
from stockguidance.inference.prediction import predict_sample
from stockguidance.training import service
from stockguidance.training.configuration import DeviceChoice, Precision
from stockguidance.training.experiments import ExperimentMember, run_experiment
from stockguidance.training.result import EvaluationKind
from stockguidance.training.resources import resolve_resources
from stockguidance.training.service import train_model


def test_training_uses_only_mature_close_targets_and_preserves_predictions(training, samples, tmp_path, monkeypatch):
	dataset, evaluation, config = training
	captured = []
	export = service.export_artifact
	inference = samples.build(9)

	def recording_export(path, model, metadata):
		model.eval()
		with torch.inference_mode():
			captured.append(tuple(model(torch.tensor(inference.features.values).reshape(1, -1))[0].tolist()))
		return export(path, model, metadata)

	monkeypatch.setattr(service, "export_artifact", recording_export)
	result = train_model(dataset, config, tmp_path / "model.sgmodel", evaluation=evaluation)
	loaded = load_artifact(result.artifact)
	assert predict_sample(loaded, inference) == pytest.approx(captured[0], abs=1e-7)
	assert result.training_samples == 1
	assert [item.entry_session.day for item in result.excluded_training] == [3, 8]
	assert result.excluded_training[1].target_state == "not_yet_available"
	assert result.completed_epochs == len(result.epochs) == 3
	assert result.evaluation_kind is EvaluationKind.CHRONOLOGICAL_HOLDOUT
	assert result.evaluation_samples == 2
	assert [item.entry_session.day for item in result.excluded_evaluation] == [11]
	assert isfinite(result.training_mae) and isfinite(result.evaluation_mae)
	assert dataset.eligible[0].targets.raw == pytest.approx((0.05, -0.1))
	assert loaded.metadata.training.last_entry.day == 5
	assert loaded.metadata.training.last_exit.day == 8
	assert loaded.metadata.training.training_cutoff == loaded.metadata.training.information_cutoff == dataset.training_cutoff
	assert loaded.metadata.semantics.features == samples.specification
	assert loaded.metadata.model.final_activation == "none"
	assert not hasattr(result, "training_directory")
	assert len(samples.repository.calls) > 0


def test_no_holdout_is_explicitly_in_sample(training, tmp_path):
	dataset, evaluation, config = training
	result = train_model(dataset, config, tmp_path / "model")
	assert result.evaluation_kind is EvaluationKind.IN_SAMPLE
	assert result.evaluation_mae is None and result.evaluation_samples == 0
	assert result.evaluation_cutoff is None


@pytest.mark.parametrize("bad_evaluation", ["overlap", "fit_after_decision", "ticker_order", "no_mature_samples"])
def test_invalid_evaluation_fails_before_model_creation(training, samples, tmp_path, monkeypatch, bad_evaluation):
	dataset, evaluation, config = training
	if bad_evaluation == "overlap":
		evaluation = dataset
	elif bad_evaluation == "fit_after_decision":
		dataset = build_training_dataset((samples.decision(5),), datetime(2024, 7, 10, 19, tzinfo=timezone.utc), samples.specification, samples.target, samples.boundary, samples.calendar, samples.publication, samples.repository)
	elif bad_evaluation == "ticker_order":
		evaluation = build_training_dataset((samples.decision(9),), evaluation.training_cutoff, replace(samples.specification, ticker_ids=(3, 17)), samples.target, samples.boundary, samples.calendar, samples.publication, samples.repository)
	else:
		evaluation = TrainingDataset((evaluation.samples[-1],), evaluation.training_cutoff)
	monkeypatch.setattr(service, "build_model", lambda *args: pytest.fail("Invalid evaluation reached model construction"))
	with pytest.raises(ValueError):
		train_model(dataset, config, tmp_path / "model", evaluation=evaluation)
	assert not (tmp_path / "model").exists()


def test_no_eligible_samples_produces_no_artifact(training, tmp_path):
	dataset, evaluation, config = training
	dataset = TrainingDataset(dataset.excluded, dataset.training_cutoff)
	with pytest.raises(ValueError, match="mature"):
		train_model(dataset, config, tmp_path / "model")
	assert list(tmp_path.iterdir()) == []


def test_nonfinite_training_output_propagates_and_preserves_existing_artifact(training, tmp_path, monkeypatch):
	dataset, evaluation, config = training
	path = tmp_path / "model"
	path.write_bytes(b"existing artifact")

	class BrokenModel(torch.nn.Module):
		def __init__(self):
			super().__init__()
			self.weights = torch.nn.Parameter(torch.zeros(2))

		def forward(self, inputs):
			return self.weights.expand(inputs.shape[0], 2) * float("nan")

	monkeypatch.setattr(service, "build_model", lambda *args: BrokenModel())
	with pytest.raises(ValueError, match="finite"):
		train_model(dataset, config, path)
	assert path.read_bytes() == b"existing artifact"


def test_export_and_checkpoint_failures_cannot_report_success(training, tmp_path, monkeypatch):
	dataset, evaluation, config = training

	def fail(*args):
		raise OSError("fixture export failure")

	monkeypatch.setattr(service, "save_checkpoint", fail)
	with pytest.raises(OSError, match="fixture"):
		train_model(dataset, config, tmp_path / "model", checkpoint_path=tmp_path / "checkpoint")
	assert not (tmp_path / "model").exists()
	monkeypatch.setattr(service, "export_artifact", fail)
	with pytest.raises(OSError, match="fixture"):
		train_model(dataset, config, tmp_path / "model")
	assert not (tmp_path / "model").exists()


def test_experiment_returns_explicit_artifacts_and_stops_on_failure(training, tmp_path, monkeypatch):
	dataset, evaluation, config = training
	members = tuple(ExperimentMember(replace(config, seed=seed), tmp_path / f"member-{seed}") for seed in (1, 2))
	results = run_experiment(dataset, members)
	assert tuple(result.artifact.path for result in results) == tuple(member.artifact_path for member in members)
	assert results[0].artifact.artifact_id != results[1].artifact.artifact_id
	with pytest.raises(ValueError, match="distinct"):
		run_experiment(dataset, (members[0], members[0]))
	from stockguidance.training import experiments
	calls = []

	def fail(*args, **kwargs):
		calls.append(args)
		raise RuntimeError("fixture member failed")

	monkeypatch.setattr(experiments, "train_model", fail)
	with pytest.raises(RuntimeError):
		run_experiment(dataset, members)
	assert len(calls) == 1


@pytest.mark.parametrize("change", [{"epochs": 0}, {"learning_rate": float("nan")}, {"shuffle": "yes"}, {"seed": -1}, {"num_workers": -1}])
def test_invalid_training_configuration_is_rejected(training, change):
	with pytest.raises(ValueError):
		replace(training[2], **change)


def test_cpu_precision_and_path_contracts(training, tmp_path):
	dataset, evaluation, config = training
	with pytest.raises(ValueError, match="separate"):
		train_model(dataset, config, tmp_path / "model", checkpoint_path=tmp_path / "model")
	with pytest.raises(ValueError, match="fp32"):
		train_model(dataset, replace(config, precision=Precision.FP16), tmp_path / "model")
	if not torch.cuda.is_available():
		with pytest.raises(ValueError, match="CUDA"):
			train_model(dataset, replace(config, device=DeviceChoice.CUDA), tmp_path / "model")
	assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("device", [DeviceChoice.AUTO, DeviceChoice.CUDA])
def test_existing_gpu_policy_and_allocator_are_preserved(training, monkeypatch, device):
	monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
	monkeypatch.setattr(torch.cuda, "get_device_name", lambda device: "NVIDIA GeForce RTX 4070")
	monkeypatch.delenv("PYTORCH_ALLOC_CONF", raising=False)
	with pytest.raises(ValueError, match="4070"):
		resolve_resources(replace(training[2], device=device))
	import os
	assert os.environ["PYTORCH_ALLOC_CONF"] == "expandable_segments:True"


def test_explicit_allocator_policy_is_not_overridden(training, monkeypatch):
	monkeypatch.setenv("PYTORCH_ALLOC_CONF", "expandable_segments:False")
	resources = resolve_resources(training[2])
	import os
	assert os.environ["PYTORCH_ALLOC_CONF"] == "expandable_segments:False"
	assert resources.device.type == "cpu"
