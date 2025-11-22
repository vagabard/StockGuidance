from __future__ import annotations

import math
import random
from typing import Tuple

from .datasets import RangeDataset, IndexDataset


def split_datasets(provider, args, max_inst: int, train_percent: float = 0.90, validation_percent: float = 0.5):
	"""Create train/val/test datasets according to split mode and args.seed.

	Returns (train_ds, val_ds, test_ds).
	"""
	n = max_inst
	if args.split_mode == "sequential":
		train_end = int(math.floor(train_percent * n))
		val_end = int(math.floor((train_percent+validation_percent) * n))
		train_ds = RangeDataset(provider, 1, max(1, train_end))
		val_start = max(1, train_end + 1)
		val_ds = RangeDataset(provider, val_start, max(val_start, val_end))
		test_start = max(val_end + 1, 1)
		test_ds = RangeDataset(provider, test_start, n)
	elif args.split_mode == "random_order_train_validation" :
		indices = list(range(1, n + 1))
		if args.seed is not None:
			rng = random.Random(int(args.seed))
			rng.shuffle(indices)
		else:
			random.shuffle(indices)
		train_n = max(1, int(math.floor(train_percent * n)))
		val_n = int(math.floor(validation_percent * n))
		train_n = min(train_n, n)
		val_n = min(val_n, max(0, n - train_n))
		test_n = max(0, n - train_n - val_n)
		train_idx = indices[:train_n]
		val_idx = indices[train_n:train_n + val_n]
		test_idx = indices[train_n + val_n:train_n + val_n + test_n]
		train_ds = IndexDataset(provider, train_idx)
		val_ds = IndexDataset(provider, val_idx)
		test_ds = IndexDataset(provider, test_idx)
	else:
		# New split mode: training and validation sets are chosen from the first
		# (train_n + val_n) indices, potentially intermixed, while the test set
		# consists of the remaining tail instances in sequential order.
		train_n = max(1, int(math.floor(train_percent * n)))
		val_n = int(math.floor(validation_percent * n))
		# Ensure we don't exceed the available instances.
		train_n = min(train_n, n)
		val_n = min(val_n, max(0, n - train_n))
		mixed_n = train_n + val_n

		# Indices 1..mixed_n are used for mixed train/validation.
		mixed_indices = list(range(1, mixed_n + 1))
		if args.seed is not None:
			# Use a deterministic RNG so that runs are repeatable given the same seed.
			rng = random.Random(int(args.seed))
			rng.shuffle(mixed_indices)
		else:
			random.shuffle(mixed_indices)

		# The test set is the sequential tail that remains.
		test_idx = list(range(mixed_n + 1, n + 1))

		# Split the mixed head portion into train and validation subsets.
		train_idx = mixed_indices[:train_n]
		val_idx = mixed_indices[train_n:train_n + val_n]

		train_ds = IndexDataset(provider, train_idx)
		val_ds = IndexDataset(provider, val_idx)
		test_ds = IndexDataset(provider, test_idx)

	return train_ds, val_ds, test_ds


__all__ = ["split_datasets"]
