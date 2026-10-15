from datetime import datetime, timezone

import pytest
import torch

from stage5.conftest import calendar, samples
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
