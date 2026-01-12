from __future__ import annotations

from typing import Iterator, List, Dict

from torch.utils.data import Dataset as TorchDataset, Subset as TorchSubset

from trainer.torch_interface.compat import TORCH_AVAILABLE
from training_instance_provider import TrainingInstanceProvider


class TimeTrainingInstanceDataset(TorchDataset):
	"""Dataset that yields TimeTrainingInstance objects from a TrainingInstanceProvider."""

	def __init__(self, instance_source: TrainingInstanceProvider):
		if not TORCH_AVAILABLE:
			raise RuntimeError("TimeTrainingInstanceDataset requires torch in train mode")
		self.instance_source = instance_source

	def __len__(self) -> int:
		return self.instance_source.get_maximum_number_of_test_train_instances()

	def __getitem__(self, idx0: int) -> Dict[str, object]:
		# TrainingInstanceProvider uses 1-based indexing for get_time_training_instance
		return self.instance_source.get_time_training_instance(int(idx0) + 1).to_dict()


class TimeTrainingInstanceSubset(TorchSubset):
	"""A Subset of a TimeTrainingInstanceDataset."""
	dataset: TimeTrainingInstanceDataset

	def __init__(self, dataset: TimeTrainingInstanceDataset, indices: List[int]) -> None:
		super().__init__(dataset, indices)

	def __getitem__(self, idx: int) -> Dict[str, object]:
		print("feature vector size ", self.dataset[self.indices[idx]]["feature_vector"][0].shape)
		return self.dataset[self.indices[idx]]

	def __iter__(self) -> Iterator[Dict[str, object]]:
		for i in self.indices:
			print("feature vector size ", self.dataset[self.indices[i]]["feature_vector"][0].shape)
			yield self.dataset[i]

	def __getitems__(self, indices: List[int]) -> List[Dict[str, object]]:
		return [self.__getitem__(idx) for idx in indices]


__all__ = [
	"TimeTrainingInstanceDataset",
	"TimeTrainingInstanceSubset",
]
