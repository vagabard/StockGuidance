from __future__ import annotations

from typing import TYPE_CHECKING, cast

from stockguidance.datasets.numpy_arrays import training_arrays
from stockguidance.datasets.training import TrainingDataset
from stockguidance.training.configuration import TrainingConfig

if TYPE_CHECKING:
	import torch
	from torch import Tensor
	from torch.utils.data import DataLoader, Dataset


def make_loader(dataset: TrainingDataset, config: TrainingConfig, generator: torch.Generator, *, shuffle: bool) -> DataLoader[tuple[Tensor, Tensor]]:
	import torch
	from torch.utils.data import DataLoader, TensorDataset

	features, targets = training_arrays(dataset)
	tensors = TensorDataset(torch.from_numpy(features).float(), torch.from_numpy(targets).float())
	return DataLoader(cast("Dataset[tuple[Tensor, Tensor]]", tensors), batch_size=config.batch_size, shuffle=shuffle, num_workers=config.num_workers, pin_memory=config.pin_memory, generator=generator)
