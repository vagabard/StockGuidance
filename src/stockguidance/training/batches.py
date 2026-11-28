from __future__ import annotations

from typing import TYPE_CHECKING, cast

from stockguidance.datasets.numpy_arrays import training_arrays
from stockguidance.datasets.training import TrainingDataset
from stockguidance.datasets.prepared import ArrayTrainingDataset
from stockguidance.training.configuration import TrainingConfig

if TYPE_CHECKING:
	import torch
	from torch import Tensor
	from torch.utils.data import DataLoader, Dataset


class IndexedTrainingCases:
	"""Picklable map-style dataset; only a requested case is materialized."""
	def __init__(self, dataset: ArrayTrainingDataset) -> None:
		self.dataset = dataset

	def __len__(self) -> int:
		return len(self.dataset.eligible)

	def __getitem__(self, index: int) -> tuple[Tensor, Tensor]:
		import torch
		features, targets = self.dataset.case_arrays(index)
		return torch.tensor(features, dtype=torch.float32), torch.tensor(targets, dtype=torch.float32)


def make_loader(dataset: TrainingDataset | ArrayTrainingDataset, config: TrainingConfig, generator: torch.Generator, *, shuffle: bool) -> DataLoader[tuple[Tensor, Tensor]]:
	import torch
	from torch.utils.data import DataLoader, TensorDataset

	if isinstance(dataset, ArrayTrainingDataset):
		tensors = cast("Dataset[tuple[Tensor, Tensor]]", IndexedTrainingCases(dataset))
	else:
		features, targets = training_arrays(dataset)
		tensors = cast("Dataset[tuple[Tensor, Tensor]]", TensorDataset(torch.from_numpy(features).float(), torch.from_numpy(targets).float()))
	return DataLoader(tensors, batch_size=config.batch_size, shuffle=shuffle, num_workers=config.num_workers, pin_memory=config.pin_memory, generator=generator)
