from dataclasses import replace
from datetime import datetime, timezone

import pytest
import torch

from stockguidance.artifacts.loading import load_artifact
from stockguidance.datasets.training import build_training_dataset
from stockguidance.training.service import train_model


def test_resume_restores_optimizer_scheduler_and_random_state(training, samples, tmp_path):
	dataset, evaluation, config = training
	dataset = build_training_dataset(tuple(samples.decision(day) for day in (5, 8, 9)), datetime(2024, 7, 11, 19, tzinfo=timezone.utc), samples.specification, samples.target, samples.boundary, samples.calendar, samples.publication, samples.repository)
	config = replace(config, epochs=4, shuffle=True, learning_rate_gamma=0.8)
	full = train_model(dataset, config, tmp_path / "full")
	first = train_model(dataset, replace(config, epochs=2), tmp_path / "first", checkpoint_path=tmp_path / "checkpoint")
	resumed = train_model(dataset, config, tmp_path / "resumed", resume_from=tmp_path / "checkpoint")
	full_state, resumed_state = load_artifact(full.artifact).model.state_dict(), load_artifact(resumed.artifact).model.state_dict()
	assert all(torch.equal(full_state[name], resumed_state[name]) for name in full_state)
	assert full.training_mae == resumed.training_mae
	assert [metric.epoch for metric in resumed.epochs] == [3, 4]
	assert full.epochs[-1].learning_rate == resumed.epochs[-1].learning_rate
	assert resumed.artifact.artifact_id == full.artifact.artifact_id
	state = torch.load(tmp_path / "checkpoint", weights_only=True)
	assert "optimizer" in state and "rng" in state
	assert "family_run" not in state
	with pytest.raises(ValueError, match="incompatible"):
		train_model(dataset, replace(config, learning_rate=0.05), tmp_path / "bad", resume_from=tmp_path / "checkpoint")
	with pytest.raises(ValueError, match="larger"):
		train_model(dataset, replace(config, epochs=2), tmp_path / "bad", resume_from=tmp_path / "checkpoint")
	samples.revise(2, 17, 125.0)
	revised = build_training_dataset(tuple(samples.decision(day) for day in (5, 8, 9)), dataset.training_cutoff, samples.specification, samples.target, samples.boundary, samples.calendar, samples.publication, samples.repository)
	with pytest.raises(ValueError, match="incompatible"):
		train_model(revised, config, tmp_path / "bad", resume_from=tmp_path / "checkpoint")
	assert not (tmp_path / "bad").exists()
