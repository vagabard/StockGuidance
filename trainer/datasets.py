from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Tuple, List

import torch

from torch.utils.data import Dataset
from trainer.torch_interface.compat import TORCH_AVAILABLE
from training_instance_provider import TrainingInstanceProvider


@dataclass
class IndexDataset(Dataset):
	"""Dataset that pulls instances by explicit 1-based index list (possibly shuffled)."""
	provider: TrainingInstanceProvider
	indices: List[int]

	def __len__(self) -> int:
		return len(self.indices)

	def __iter__(self):
		for i in self.indices:
			x, y, date_for_instance = self.provider.get_feature_and_label_pair(int(i))
			# We return tensors directly if they are already tensors, as they now use float32.
			# This avoids the conversion back to numpy and then back to torch in DataLoader.
			yield x, y

	def __getitem__(self, idx0: int):
		i = int(self.indices[int(idx0)])
		x, y, date_for_instance = self.provider.get_feature_and_label_pair(i)
		return x, y

	def get_next_feature_label_date(self):
		for i in self.indices:
			x, y, date_for_instance = self.provider.get_feature_and_label_pair(int(i))
			yield x, y, date_for_instance

	def get_feature_label_date(self, idx0: int):
		i = int(self.indices[int(idx0)])
		x, y, date_for_instance = self.provider.get_feature_and_label_pair(i)
		return x, y, date_for_instance



	#the indices list has continuous integers starting at 1
	#This lets you find the next training instance index (which is the date_id) which may skip numbers
	#basically adding 1 to a date_id may result in an invalid date_id
	#adding 1 to an index will always result in a valid index (except for the last index)
	def convert_dataset_index_to_date_id(self, idx0: int) -> int:
		return int(self.indices[int(idx0)])

	def convert_date_id_to_dataset_index(self, idx0: int) -> int:
		return self.indices.index(idx0)

__all__ = [
	"IndexDataset",
]
