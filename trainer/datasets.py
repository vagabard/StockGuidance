from __future__ import annotations
from typing import TYPE_CHECKING

import line_profiler

if TYPE_CHECKING:
	from training_instance_provider import TrainingInstanceProvider

from dataclasses import dataclass
from typing import Iterator, Tuple, List

import torch

from torch.utils.data import Dataset
from trainer.torch_interface.compat import TORCH_AVAILABLE


@dataclass
class IndexDataset(Dataset):
	"""Dataset that pulls instances by explicit 1-based index list (possibly shuffled)."""
	provider: TrainingInstanceProvider
	indices: List[int]

	# Reusable tensors for __getitem__ to avoid allocation overhead
	_reusable_feat_getitem: torch.Tensor | None = None

	def __len__(self) -> int:
		return len(self.indices)

	def __iter__(self):
		# Reusable tensors for features and labels to avoid allocation overhead
		feat_out: torch.Tensor | None = None

		for i in self.indices:
			# Use the provider's ability to reuse tensors if it's a TrainingInstanceProvider
			# or pass our own managed buffers.
			x, y, date_for_instance = self.provider.get_feature_and_label_pair(int(i), out_feat=feat_out)

			# Capture the tensors returned on the first iteration to reuse them
			if feat_out is None:
				feat_out = x

			# We return tensors directly if they are already tensors, as they now use float32.
			# This avoids the conversion back to numpy and then back to torch in DataLoader.
			yield x, y

	def __getitem__(self, idx0: int):
		i = int(self.indices[int(idx0)])
		# Use the preallocated tensors if available
		x, y, date_for_instance = self.provider.get_feature_and_label_pair(
			i,
			out_feat=self._reusable_feat_getitem
		)

		# Capture the tensors on first call to reuse them next time
		if self._reusable_feat_getitem is None:
			self._reusable_feat_getitem = x

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
