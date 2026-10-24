from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import torch

from stockguidance.artifacts.archive import archive_bytes, read_archive
from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.artifacts.exporting import export_artifact
from stockguidance.artifacts.loading import load_artifact
from stockguidance.inference.prediction import predict_sample
from stockguidance.market_data.identity import content_id
from stockguidance.models.factory import build_model
from stockguidance.models.specification import Activation, Architecture, LayerType, ModelSpec
from stockguidance.training.service import train_model


@pytest.fixture
def artifact(training, tmp_path):
	dataset, evaluation, config = training
	return train_model(dataset, config, tmp_path / "model.sgmodel").artifact


def test_artifact_is_independent_after_copy_and_empty_working_directory(artifact, tmp_path):
	empty = tmp_path / "empty"
	empty.mkdir()
	copy = empty / "arbitrary-name.bin"
	copy.write_bytes(artifact.path.read_bytes())
	artifact.path.unlink()
	code = """
import sys
from pathlib import Path
from stockguidance.artifacts.loading import load_artifact
model = load_artifact(Path(sys.argv[1]))
assert model.metadata.semantics.features.ticker_ids == (17, 3)
assert not any(name == 'trainer' or name.startswith('trainer.') or name == 'models' or name.startswith('stockguidance.training') for name in sys.modules)
assert not model.model.training
"""
	root = Path(__file__).resolve().parents[2] / "src"
	environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), os.environ.get("PYTHONPATH", ""))))
	completed = subprocess.run([sys.executable, "-c", code, str(copy)], cwd=empty, env=environment, text=True, capture_output=True, timeout=30)
	assert completed.returncode == 0, completed.stderr
	assert completed.stdout == "" and completed.stderr == ""
	assert [item.name for item in empty.iterdir()] == [copy.name]


def test_metadata_is_explicit_allowlisted_and_has_no_credentials_or_controller_state(artifact):
	manifest, files = read_archive(artifact.path.read_bytes())
	assert set(files) == {"weights.pt"}
	assert manifest["schema"] == "stockguidance-inference-1"
	metadata = manifest["payload"]["metadata"]
	assert metadata["semantics"]["features"]["ticker_ids"] == [17, 3]
	assert metadata["semantics"]["features"]["lookback_sessions"] == 3
	assert metadata["layout"] == "session_oldest_first,ticker,feature:C"
	assert metadata["feature_version"] == "previous-close-v1"
	assert metadata["target_formula"] == "close(T+1) / close(T) - 1"
	assert metadata["preprocessing"] == "stateless_window_only"
	assert metadata["semantics"]["execution_assumption"] == "pre_close_decision_same_close_fill"
	text = json.dumps(manifest).lower()
	for forbidden in ("family_run", "single_run", "optimizer", "training_directory", "dbinfo", "credential", "api_key", "password", "allocation"):
		assert forbidden not in text
	state = torch.load(BytesIO(files["weights.pt"]), weights_only=True)
	assert all(isinstance(value, torch.Tensor) for value in state.values())


@pytest.mark.parametrize("corruption", ["missing_weights", "wrong_digest", "wrong_version", "extra_file", "extra_metadata", "bad_layout", "wrong_target", "entry_open", "future_exit"])
def test_incomplete_or_incompatible_artifacts_are_rejected(artifact, tmp_path, corruption):
	manifest, files = read_archive(artifact.path.read_bytes())
	if corruption == "missing_weights":
		files.clear()
	elif corruption == "wrong_digest":
		files["weights.pt"] += b"corrupt"
	elif corruption == "wrong_version":
		manifest["schema"] = "legacy-1"
	elif corruption == "extra_file":
		files["optimizer.pt"] = b"invalid extra training state"
	else:
		metadata = manifest["payload"]["metadata"]
		if corruption == "extra_metadata":
			metadata["credential"] = "fixture-do-not-serialize"
		elif corruption == "bad_layout":
			metadata["layout"] = "ticker,session,feature:C"
		elif corruption == "wrong_target":
			metadata["semantics"]["target"]["transform"] = "legacy-normalized"
		elif corruption == "future_exit":
			metadata["training"]["last_exit_close_at"] = "2025-01-01T20:00:00+00:00"
		else:
			metadata["semantics"]["features"]["signal_mode"] = "entry_open"
		manifest["artifact_id"] = content_id(manifest["payload"])
	path = tmp_path / "bad"
	path.write_bytes(archive_bytes(manifest, files))
	with pytest.raises(ValueError):
		load_artifact(path)


def test_reference_identity_and_inference_semantics_are_checked(artifact, samples):
	loaded = load_artifact(artifact)
	with pytest.raises(ValueError, match="identity"):
		load_artifact(ArtifactReference(artifact.path, "0" * 64))
	with pytest.raises(ValueError, match="cutoff"):
		predict_sample(loaded, samples.build(8))
	with pytest.raises(ValueError, match="semantics"):
		predict_sample(loaded, samples.build(9, specification=replace(samples.specification, ticker_ids=(3, 17))))
	samples.repository.missing.add((samples.calendar.session(samples.boundary.history_start), 17))
	samples.repository.missing.add((samples.calendar.shift(samples.decision(9).entry_session, -1), 17))
	with pytest.raises(ValueError, match="unavailable"):
		predict_sample(loaded, samples.build(9))


def test_atomic_export_failure_preserves_previous_artifact(artifact, tmp_path, monkeypatch):
	loaded = load_artifact(artifact)
	previous = artifact.path.read_bytes()
	from stockguidance.market_data import atomic

	def fail(source, destination):
		raise OSError("fixture interrupted replacement")

	monkeypatch.setattr(atomic.os, "replace", fail)
	with pytest.raises(OSError):
		export_artifact(artifact.path, loaded.model, loaded.metadata)
	assert artifact.path.read_bytes() == previous
	assert [item.name for item in tmp_path.iterdir()] == [artifact.path.name]


@pytest.mark.parametrize("layer", list(LayerType))
def test_actual_mlp_layer_implementations_round_trip(training, samples, tmp_path, layer):
	dataset, evaluation, config = training
	specification = ModelSpec(hidden_widths=(6,), layer_types=(layer, LayerType.LINEAR))
	result = train_model(dataset, replace(config, model=specification, epochs=1), tmp_path / "model")
	loaded = load_artifact(result.artifact)
	assert len(predict_sample(loaded, samples.build(9))) == 2
	assert loaded.metadata.model.layer_types == (layer, LayerType.LINEAR)


@pytest.mark.parametrize("activation", list(Activation))
def test_hidden_activation_choices_preserve_unrestricted_output(training, tmp_path, activation):
	dataset, evaluation, config = training
	result = train_model(dataset, replace(config, model=replace(config.model, activation=activation), epochs=1), tmp_path / "model")
	assert load_artifact(result.artifact).metadata.model.activation is activation


def test_original_convolution_transformer_round_trip(training, samples, tmp_path):
	dataset, evaluation, config = training
	result = train_model(dataset, replace(config, model=ModelSpec(architecture=Architecture.CONV_TRANSFORMER), epochs=1), tmp_path / "conv")
	loaded = load_artifact(result.artifact)
	assert len(predict_sample(loaded, samples.build(9))) == 2
	assert loaded.model.conv1.kernel_size == (15,)
	assert loaded.model.conv1.out_channels == 16
	assert loaded.model.trans1.layers[0].norm_first


def test_single_linear_layer_and_invalid_output_activation(samples):
	model = build_model(ModelSpec(hidden_widths=()), samples.specification)
	assert len(model.net) == 1
	assert model(torch.zeros(1, samples.specification.input_size)).shape == (1, 2)
	with pytest.raises(ValueError, match="unrestricted"):
		ModelSpec(final_activation="softmax")


def test_nonfinite_or_wrong_architecture_weights_do_not_export(artifact, tmp_path):
	loaded = load_artifact(artifact)
	wrong = build_model(ModelSpec(hidden_widths=(8,)), loaded.metadata.semantics.features)
	with pytest.raises(ValueError, match="shape"):
		export_artifact(tmp_path / "bad", wrong, loaded.metadata)
	with torch.no_grad():
		next(loaded.model.parameters()).fill_(float("nan"))
	with pytest.raises(ValueError, match="finite"):
		export_artifact(tmp_path / "bad", loaded.model, loaded.metadata)
	assert not (tmp_path / "bad").exists()


def test_float32_artifact_is_independent_of_process_default_dtype(artifact, samples):
	previous = torch.get_default_dtype()
	try:
		torch.set_default_dtype(torch.float64)
		loaded = load_artifact(artifact)
		assert next(loaded.model.parameters()).dtype is torch.float32
		assert len(predict_sample(loaded, samples.build(9))) == 2
	finally:
		torch.set_default_dtype(previous)
