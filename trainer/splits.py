from __future__ import annotations

import math
import random
from typing import Tuple

from .datasets import RangeDataset, IndexDataset


def split_datasets(provider, args, max_inst: int, train_percent: float = 0.90, validation_percent: float = 0.05):
	"""Create train/val/test datasets according to split mode and args.seed.

	The caller can optionally restrict the *source region* of the dataset from
	which train/validation/test instances are drawn via two fractional
	arguments on ``args``:

	``args.data_head_ignore_percent``
	    Fraction in ``[0.0, 1.0]`` of the *beginning* of the dataset that
	    should be ignored when choosing any instances.

	``args.data_tail_ignore_percent``
	    Fraction in ``[0.0, 1.0]`` of the *end* of the dataset that should be
	    ignored when choosing any instances.

	For example, ``head=0.70`` and ``tail=0.10`` means that only the middle
	20% slice of the dataset (from 70% to 90%) is eligible, and the
	train/validation/test splits are created *within that slice* using the
	usual ``train_percent`` and ``validation_percent`` fractions.

	Returns (train_ds, val_ds, test_ds).
	"""
	# Total number of available instances (1-based indexing is used
	# throughout this module).
	n = max_inst
	if n <= 0:
		return RangeDataset(provider, 1, 0), RangeDataset(provider, 1, 0), RangeDataset(provider, 1, 0)

	# Determine the effective range [base_start, base_end] (inclusive) that is
	# eligible for *any* split after ignoring a head and/or tail fraction.
	head_frac = max(0.0, float(getattr(args, "data_head_ignore_percent", 0.0) or 0.0))
	tail_frac = max(0.0, float(getattr(args, "data_tail_ignore_percent", 0.0) or 0.0))
	# Clamp to [0, 1] to avoid surprising behaviour if a caller passes >1.0.
	head_frac = min(head_frac, 1.0)
	tail_frac = min(tail_frac, 1.0)

	# Compute start/end indices for the usable subrange.
	base_start = int(math.floor(head_frac * n)) + 1
	base_end = int(math.floor((1.0 - tail_frac) * n))

	# Ensure the range is valid and within bounds.
	if base_start < 1:
		base_start = 1
	if base_end > n:
		base_end = n
	if base_end < base_start:
		# Degenerate case: nothing left after applying head/tail trimming.
		# Fall back to empty datasets; the caller will normally detect
		# "not enough instances" earlier, but we guard defensively here.
		empty = IndexDataset(provider, [])
		return empty, empty, empty

	usable_total = base_end - base_start + 1

	if args.split_mode == "sequential":
		train_end = base_start + int(math.floor(train_percent * usable_total)) - 1
		val_end = base_start + int(math.floor((train_percent + validation_percent) * usable_total)) - 1
		train_end = max(base_start, min(train_end, base_end))
		train_ds = RangeDataset(provider, base_start, train_end)
		val_start = min(max(train_end + 1, base_start), base_end + 1)
		val_end = max(val_start, min(val_end, base_end))
		val_ds = RangeDataset(provider, val_start, val_end) if val_start <= val_end else RangeDataset(provider, base_start, base_start - 1)
		test_start = min(max(val_end + 1, base_start), base_end + 1)
		test_ds = RangeDataset(provider, test_start, base_end) if test_start <= base_end else RangeDataset(provider, base_start, base_start - 1)
	elif args.split_mode == "random_order_train_validation" :
		# Build the list of indices only from the usable subrange.
		indices = list(range(base_start, base_end + 1))
		if args.seed is not None:
			rng = random.Random(int(args.seed))
			rng.shuffle(indices)
		else:
			random.shuffle(indices)
		train_n = max(1, int(math.floor(train_percent * usable_total)))
		val_n = int(math.floor(validation_percent * usable_total))
		train_n = min(train_n, usable_total)
		val_n = min(val_n, max(0, usable_total - train_n))
		test_n = max(0, usable_total - train_n - val_n)
		train_idx = indices[:train_n]
		val_idx = indices[train_n:train_n + val_n]
		test_idx = indices[train_n + val_n:train_n + val_n + test_n]
		train_ds = IndexDataset(provider, train_idx)
		val_ds = IndexDataset(provider, val_idx)
		test_ds = IndexDataset(provider, test_idx)
	else:
		# mix_training_and_validation: training and validation sets are chosen
		# from the first (train_n + val_n) usable indices, potentially
		# intermixed, while the test set consists of the remaining tail
		# instances in sequential order, all within the trimmed range.
		train_n = max(1, int(math.floor(train_percent * usable_total)))
		val_n = int(math.floor(validation_percent * usable_total))
		# Ensure we don't exceed the available instances.
		train_n = min(train_n, usable_total)
		val_n = min(val_n, max(0, usable_total - train_n))
		mixed_n = train_n + val_n

		# Indices base_start..base_start+mixed_n-1 are used for mixed
		# train/validation.
		mixed_indices = list(range(base_start, base_start + mixed_n))
		if args.seed is not None:
			# Use a deterministic RNG so that runs are repeatable given the same seed.
			rng = random.Random(int(args.seed))
			rng.shuffle(mixed_indices)
		else:
			random.shuffle(mixed_indices)

		# The test set is the sequential tail that remains within the usable
		# subrange.
		test_idx = list(range(base_start + mixed_n, base_end + 1))

		# Split the mixed head portion into train and validation subsets.
		train_idx = mixed_indices[:train_n]
		val_idx = mixed_indices[train_n:train_n + val_n]

		train_ds = IndexDataset(provider, train_idx)
		val_ds = IndexDataset(provider, val_idx)
		test_ds = IndexDataset(provider, test_idx)

	return train_ds, val_ds, test_ds


__all__ = ["split_datasets"]
