from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Tuple, List

import torch

from trainer.torch_interface.compat import TORCH_AVAILABLE
from trainer.util.utils import tensorize_and_sanitize
from training_instance_provider import TrainingInstanceProvider


@dataclass
class RangeDataset:
    """Dataset that pulls instances from the provider by 1-based index range."""
    provider: TrainingInstanceProvider
    start_idx: int
    end_idx: int

    def __len__(self) -> int:
        return max(0, int(self.end_idx) - int(self.start_idx) + 1)

    def __iter__(self):
        for i in range(int(self.start_idx), int(self.end_idx) + 1):
            x, y = self.provider.get_feature_and_label_pair(i)
            x, y = tensorize_and_sanitize(x, y)
            yield x, y

    def __getitem__(self, idx0: int):
        i = int(self.start_idx) + int(idx0)
        x, y = self.provider.get_feature_and_label_pair(i)
        return tensorize_and_sanitize(x, y)


@dataclass
class IndexDataset:
    """Dataset that pulls instances by explicit 1-based index list (possibly shuffled)."""
    provider: TrainingInstanceProvider
    indices: List[int]

    def __len__(self) -> int:
        return len(self.indices)

    def __iter__(self):
        for i in self.indices:
            x, y = self.provider.get_feature_and_label_pair(int(i))
            x, y = tensorize_and_sanitize(x, y)
            yield x, y

    def __getitem__(self, idx0: int):
        i = int(self.indices[int(idx0)])
        x, y = self.provider.get_feature_and_label_pair(i)
        return tensorize_and_sanitize(x, y)


def batched(iterable: Iterator[Tuple["torch_interface.Tensor", "torch_interface.Tensor"]], batch_size: int):
    """Simple batching utility yielding (xb, yb) tensors of size <= batch_size."""
    if not TORCH_AVAILABLE:
        raise RuntimeError("Batched iteration requires torch_interface in train mode")
    batch_x = []
    batch_y = []
    for x, y in iterable:
        batch_x.append(x)
        batch_y.append(y)
        if len(batch_x) >= int(batch_size):
            yield torch.stack(batch_x, dim=0), torch.stack(batch_y, dim=0)
            batch_x.clear()
            batch_y.clear()
    if batch_x:
        yield torch.stack(batch_x, dim=0), torch.stack(batch_y, dim=0)


__all__ = [
    "RangeDataset",
    "IndexDataset",
    "batched",
]
