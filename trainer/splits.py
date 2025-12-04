from __future__ import annotations

import math
import random
from typing import Tuple

from .datasets import RangeDataset, IndexDataset


def split_datasets(
	provider,
	args,
	max_inst: int,
	train_validation_and_test_set_start_date_id: int | None = None,
	train_validation_and_test_set_end_date_id: int | None = None,
	train_percent: float = 0.90,
	validation_percent: float = 0.05,
):
	"""Create train/val/test datasets according to split mode and args.seed.

	The caller can optionally restrict the *source region* of the dataset from
	which train/validation/test instances are drawn via two fractional
	arguments on ``args``:

	Returns (train_ds, val_ds, test_ds).
	"""
	# Total number of available instances (1-based indexing is used
	# throughout this module).
	length_of_date_id_array = max_inst
	if length_of_date_id_array <= 0:
		return (
			RangeDataset(provider, 1, 0),
			RangeDataset(provider, 1, 0),
			RangeDataset(provider, 1, 0),
		)

	# Determine the effective range [date_array_index_for_start_date, date_array_index_for_end_date] (inclusive) that is
	# eligible for *any* split.
	#
	# New behaviour:
	#   * If explicit train_start_date_id / train_end_date_id are provided,
	#     they define the usable window and any head/tail ignore fractions are
	#     ignored.
	#   * Otherwise we retain the original behaviour based on
	#     data_head_ignore_percent / data_tail_ignore_percent.

	# Default to the full range; branches below will tighten this.
	date_array_index_for_start_date = 1
	date_array_index_for_end_date = length_of_date_id_array

	if (
		train_validation_and_test_set_start_date_id is not None
		and train_validation_and_test_set_end_date_id is not None
		and train_validation_and_test_set_start_date_id > 0
		and train_validation_and_test_set_end_date_id > 0
	):
		# Map date IDs to 1-based instance indices. The provider exposes its
		# underlying dates via ``_dates`` with an offset of ``_num_prior``
		# between instance index and date index.
		try:
			all_dates = [int(d) for d in getattr(provider, "_dates")]
			prior = int(getattr(provider, "_num_prior"))
		except Exception:
			# Fallback: if provider does not expose the expected attributes we
			# cannot honour explicit date IDs; re-raise a clear error instead of
			# silently falling back to percentage-based trimming.
			raise RuntimeError("Provider does not expose _dates/_num_prior required for train_start_date_id/train_end_date_id handling")

		# Build mapping date_id -> instance index (1-based).
		date_id_to_date_array_index = {}
		for inst_idx in range(1, length_of_date_id_array + 1):
			date_pos = prior + (inst_idx - 1)
			if 0 <= date_pos < len(all_dates):
				date_id_to_date_array_index[int(all_dates[date_pos])] = inst_idx

		# Historically this function raised if the requested start/end date IDs
		# were not present in the provider's available dates. In practice the DB
		# may contain gaps (non‑trading days, missing rows, etc.), so a caller may
		# reasonably pass a window whose numeric bounds do not correspond to
		# *actual* dates in ``all_dates``. To make the system more robust we now
		# clamp to the nearest available date **within** the requested window:
		#
		#   * start is moved forward to the first available date_id >= requested
		#   * end   is moved backward to the last  available date_id <= requested
		#
		# If there is no overlap at all between the requested window and the
		# provider's dates, we still raise a clear error.
		requested_start_id = int(train_validation_and_test_set_start_date_id)
		requested_end_id = int(train_validation_and_test_set_end_date_id)
		if requested_start_id > requested_end_id:
			requested_start_id, requested_end_id = requested_end_id, requested_start_id

		available_ids_sorted = sorted(date_id_to_date_array_index.keys())
		# Find first available date_id >= requested_start_id
		resolved_start_id = None
		for did in available_ids_sorted:
			if did >= requested_start_id:
				resolved_start_id = did
				break
		# Find last available date_id <= requested_end_id
		resolved_end_id = None
		for did in reversed(available_ids_sorted):
			if did <= requested_end_id:
				resolved_end_id = did
				break

		if resolved_start_id is None or resolved_end_id is None or resolved_start_id > resolved_end_id:
			raise RuntimeError(
				f"Requested train_start_date_id/train_end_date_id ({train_validation_and_test_set_start_date_id}, {train_validation_and_test_set_end_date_id}) "
				"do not overlap with available instances"
			)

		date_array_index_for_start_date = int(date_id_to_date_array_index[resolved_start_id])
		date_array_index_for_end_date = int(date_id_to_date_array_index[resolved_end_id])
		if date_array_index_for_start_date > date_array_index_for_end_date:
			date_array_index_for_start_date, date_array_index_for_end_date = (
				date_array_index_for_end_date,
				date_array_index_for_start_date,
			)
	else:
		# Legacy behaviour: derive usable window from fractional head/tail
		# trimming. These attributes are used by the existing tests and by
		# callers that do not care about explicit date_id ranges.
		head_frac = float(getattr(args, "data_head_ignore_percent", 0.0) or 0.0)
		tail_frac = float(getattr(args, "data_tail_ignore_percent", 0.0) or 0.0)
		head_count = int(math.floor(head_frac * length_of_date_id_array))
		date_array_index_for_start_date = 1 + head_count
		# Use the same formula as the tests when converting tail_frac to an
		# index so behaviour remains consistent: base_end = floor((1 - tail) * N).
		date_array_index_for_end_date = int(
			math.floor((1.0 - tail_frac) * length_of_date_id_array),
		)

	# Ensure the range is valid and within bounds.
	if date_array_index_for_start_date < 1:
		date_array_index_for_start_date = 1
	if date_array_index_for_end_date > length_of_date_id_array:
		date_array_index_for_end_date = length_of_date_id_array
	if date_array_index_for_end_date < date_array_index_for_start_date:
		# Degenerate case: nothing left after applying head/tail trimming.
		# Fall back to empty datasets; the caller will normally detect
		# "not enough instances" earlier, but we guard defensively here.
		empty = IndexDataset(provider, [])
		return empty, empty, empty

	usable_total = date_array_index_for_end_date - date_array_index_for_start_date + 1


	if args.split_mode == "sequential":
		total_instances_end_date_index_for_training = date_array_index_for_start_date + int(math.floor(train_percent * usable_total)) - 1
		total_instances_end_date_index_for_validation = date_array_index_for_start_date + int(math.floor((train_percent + validation_percent) * usable_total)) - 1
		total_instances_end_date_index_for_training = max(date_array_index_for_start_date, min(total_instances_end_date_index_for_training, date_array_index_for_end_date))
		train_ds = RangeDataset(provider, date_array_index_for_start_date, total_instances_end_date_index_for_training)
		total_instances_start_date_index_for_validation = min(max(total_instances_end_date_index_for_training + 1, date_array_index_for_start_date), date_array_index_for_end_date + 1)
		total_instances_end_date_index_for_validation = max(total_instances_start_date_index_for_validation, min(total_instances_end_date_index_for_validation, date_array_index_for_end_date))
		val_ds = RangeDataset(provider, total_instances_start_date_index_for_validation, total_instances_end_date_index_for_validation) if total_instances_start_date_index_for_validation <= total_instances_end_date_index_for_validation else RangeDataset(provider, date_array_index_for_start_date, date_array_index_for_start_date - 1)
		total_instances_start_date_index_for_testing = min(max(total_instances_end_date_index_for_validation + 1, date_array_index_for_start_date), date_array_index_for_end_date + 1)
		test_ds = RangeDataset(provider, total_instances_start_date_index_for_testing, date_array_index_for_end_date) if total_instances_start_date_index_for_testing <= date_array_index_for_end_date else RangeDataset(provider, date_array_index_for_start_date, date_array_index_for_start_date - 1)
#		mixed_n = total_instances_end_date_index_for_training - date_array_index_for_start_date + 1  + total_instances_end_date_index_for_validation - total_instances_start_date_index_for_validation + 1
#		print(f"Sequential Training set size: {total_instances_end_date_index_for_training}, validation set size: {total_instances_end_date_index_for_validation}, totalk testing set size: {total_instances_start_date_index_for_testing},   total instances: {usable_total}")
	elif args.split_mode == "random_order_train_validation" :
		# Build the list of indices only from the usable subrange.
		indices = list(range(date_array_index_for_start_date, date_array_index_for_end_date + 1))
		if args.seed is not None:
			rng = random.Random(int(args.seed))
			rng.shuffle(indices)
		else:
			random.shuffle(indices)
		training_set_number_of_instances = max(1, int(math.floor(train_percent * usable_total)))
		validation_set_number_of_instances = int(math.floor(validation_percent * usable_total))
		training_set_number_of_instances = min(training_set_number_of_instances, usable_total)
		validation_set_number_of_instances = min(validation_set_number_of_instances, max(0, usable_total - training_set_number_of_instances))
		test_set_number_of_instances = max(0, usable_total - training_set_number_of_instances - validation_set_number_of_instances)
		train_idx = indices[:training_set_number_of_instances]
		val_idx = indices[training_set_number_of_instances:training_set_number_of_instances + validation_set_number_of_instances]
		test_idx = indices[training_set_number_of_instances + validation_set_number_of_instances:training_set_number_of_instances + validation_set_number_of_instances + test_set_number_of_instances]
		train_ds = IndexDataset(provider, train_idx)
		val_ds = IndexDataset(provider, val_idx)
		test_ds = IndexDataset(provider, test_idx)
#		mixed_n = training_set_number_of_instances + validation_set_number_of_instances
#		print(f" random order train val  training percentage: {train_percent}  validation_percent: {validation_percent}  Training set size: {training_set_number_of_instances}, validation set size: {validation_set_number_of_instances}, mixed set size: {mixed_n},   total instances: {usable_total}")
	else:
		# mix_training_and_validation: training and validation sets are chosen
		# from the first (training_set_number_of_instances + validation_set_number_of_instances) usable indices, potentially
		# intermixed, while the test set consists of the remaining tail
		# instances in sequential order, all within the trimmed range.
		training_set_number_of_instances = max(1, int(math.floor(train_percent * usable_total)))
		validation_set_number_of_instances = int(math.floor(validation_percent * usable_total))
		# Ensure we don't exceed the available instances.
		training_set_number_of_instances = min(training_set_number_of_instances, usable_total)
		validation_set_number_of_instances = min(validation_set_number_of_instances, max(0, usable_total - training_set_number_of_instances))
		mixed_n = training_set_number_of_instances + validation_set_number_of_instances

		# Indices date_array_index_for_start_date..date_array_index_for_start_date+mixed_n-1 are used for mixed
		# train/validation.
		mixed_indices = list(range(date_array_index_for_start_date, date_array_index_for_start_date + mixed_n))
		if args.seed is not None:
			# Use a deterministic RNG so that runs are repeatable given the same seed.
			rng = random.Random(int(args.seed))
			rng.shuffle(mixed_indices)
		else:
			random.shuffle(mixed_indices)

		# The test set is the sequential tail that remains within the usable
		# subrange.
		test_idx = list(range(date_array_index_for_start_date + mixed_n, date_array_index_for_end_date + 1))

		# Split the mixed head portion into train and validation subsets.
		train_idx = mixed_indices[:training_set_number_of_instances]
		val_idx = mixed_indices[training_set_number_of_instances:training_set_number_of_instances + validation_set_number_of_instances]

		train_ds = IndexDataset(provider, train_idx)
		val_ds = IndexDataset(provider, val_idx)
		test_ds = IndexDataset(provider, test_idx)

	return train_ds, val_ds, test_ds


__all__ = ["split_datasets"]
