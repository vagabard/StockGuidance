from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
import torch

from support.samples import calendar, samples
from stockguidance.config.source import SourceConfig
from stockguidance.datasets.training import build_training_dataset
from stockguidance.models.specification import ModelSpec
from stockguidance.training.configuration import DeviceChoice, TrainingConfig


@pytest.fixture(autouse=True, scope="module")
def cpu_threads():
	previous = torch.get_num_threads()
	torch.set_num_threads(1)
	yield
	torch.set_num_threads(previous)


@pytest.fixture
def training(samples):
	cutoff = datetime(2024, 7, 9, 19, tzinfo=timezone.utc)
	dataset = build_training_dataset(tuple(samples.decision(day) for day in (3, 5, 8)), cutoff, samples.specification, samples.target, samples.boundary, samples.calendar, samples.publication, samples.repository)
	evaluation = build_training_dataset(tuple(samples.decision(day) for day in (9, 10, 11)), datetime(2024, 7, 11, 21, tzinfo=timezone.utc), samples.specification, samples.target, samples.boundary, samples.calendar, samples.publication, samples.repository)
	configuration = TrainingConfig(model=ModelSpec(hidden_widths=(4,)), epochs=3, batch_size=1, learning_rate=0.01, device=DeviceChoice.CPU, seed=17)
	return dataset, evaluation, configuration


@pytest.fixture
def job(training, samples, tmp_path):

	dataset, evaluation, config = training
	database_config = tmp_path / "fixture-secret-database.id"
	database_config.write_text(json.dumps({"host": "fixture", "database": "fixture", "user": "fixture", "password": ""}), encoding="utf-8")
	return {
		"source": asdict(samples.boundary.source), "data_root": str(tmp_path / "source"),
		"database_config": str(database_config),
		"features": asdict(samples.specification), "model": asdict(config.model),
		"training": {name: value for name, value in asdict(config).items() if name != "model"},
		"start_date": "2024-07-03", "end_date": "2024-07-08", "training_cutoff": dataset.training_cutoff.isoformat(),
		"publication_delay_minutes": 15, "publication_evidence": "Fixture-only publication policy",
		"decision_minutes_before_close": 30,
		"evaluation": {"start_date": "2024-07-09", "end_date": "2024-07-11", "label_cutoff": evaluation.training_cutoff.isoformat()},
	}
