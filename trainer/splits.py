from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from trainer.datasets import IndexDataset
from training_instance_provider import TrainingInstanceProvider

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
	from trainer.state.family_run import FamilyRun


def split_datasets(
	provider: TrainingInstanceProvider,
	family_run: FamilyRun,
	max_inst: int,
	train_validation_and_test_set_start_date_id: int | None = None,
	train_validation_and_test_set_end_date_id: int | None = None,
	train_percent: float = 0.90,
	validation_percent: float = 0.04,
) -> tuple[IndexDataset, IndexDataset, IndexDataset]:
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
			IndexDataset(provider, []),
			IndexDataset(provider, []),
			IndexDataset(provider, []),
		)

	# Determine the effective range [date_array_index_for_start_date, date_array_index_for_end_date] (inclusive) that is
	# eligible for *any* split.
	#
	# New behaviour:
	#   * If explicit raw_train_start_with_special_characters / raw_train_end_with_special_characters are provided,
	#     they define the usable window and any head/tail ignore fractions are
	#     ignored.
	#   * Otherwise we retain the original behaviour based on
	#     data_head_ignore_percent / data_tail_ignore_percent.


	if (
		train_validation_and_test_set_start_date_id is not None
		and train_validation_and_test_set_end_date_id is not None
		and train_validation_and_test_set_start_date_id > 0
		and train_validation_and_test_set_end_date_id > 0
	):
		# Map date IDs to 1-based instance indices. The provider exposes its
		# underlying dates via ``cumulative_dates_for_train_validation_and_test_sets`` with an offset of ``number_of_prior_days_to_include_in_feature``
		# between instance index and date index.
		try:
			all_dates = [int(d) for d in getattr(provider, "cumulative_dates_for_train_validation_and_test_sets")]
			prior = int(getattr(provider, "number_of_prior_days_to_include_in_feature"))
		except Exception:
			# Fallback: if provider does not expose the expected attributes we
			# cannot honour explicit date IDs; re-raise a clear error instead of
			# silently falling back to percentage-based trimming.
			raise RuntimeError("Provider does not expose cumulative_dates_for_train_validation_and_test_sets/number_of_prior_days_to_include_in_feature required for raw_train_start_with_special_characters/raw_train_end_with_special_characters handling")

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
		requested_start_id = train_validation_and_test_set_start_date_id
		requested_end_id = train_validation_and_test_set_end_date_id
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
				f"Requested raw_train_start_with_special_characters/raw_train_end_with_special_characters ({train_validation_and_test_set_start_date_id}, {train_validation_and_test_set_end_date_id}) "
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
		raise ValueError("train_validation_and_test_set_start_date_id and train_validation_and_test_set_end_date_id must be provided and greater than 0")

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


	if getattr(family_run, "split_mode", None) == "sequential":
		total_instances_end_date_index_for_training = date_array_index_for_start_date + int(math.floor(train_percent * usable_total)) - 1
		total_instances_end_date_index_for_training = max(date_array_index_for_start_date, min(total_instances_end_date_index_for_training, date_array_index_for_end_date))
		train_ds = IndexDataset(provider, list(range(date_array_index_for_start_date, total_instances_end_date_index_for_training + 1)))

		total_instances_end_date_index_for_validation = date_array_index_for_start_date + int(math.floor((train_percent + validation_percent) * usable_total)) - 1
		val_start = total_instances_end_date_index_for_training + 1
		val_end = min(total_instances_end_date_index_for_validation, date_array_index_for_end_date)
		if val_start > val_end:
			val_ds = IndexDataset(provider, [])
			test_start = val_start
		else:
			val_ds = IndexDataset(provider, list(range(val_start, val_end + 1)))
			test_start = val_end + 1

		if test_start > date_array_index_for_end_date:
			test_ds = IndexDataset(provider, [])
		else:
			test_ds = IndexDataset(provider, list(range(test_start, date_array_index_for_end_date + 1)))
#		mixed_n = total_instances_end_date_index_for_training - date_array_index_for_start_date + 1  + total_instances_end_date_index_for_validation - total_instances_start_date_index_for_validation + 1
#		print(f"Sequential Training set size: {total_instances_end_date_index_for_training}, validation set size: {total_instances_end_date_index_for_validation}, totalk testing set size: {total_instances_start_date_index_for_testing},   total instances: {usable_total}")
	elif getattr(family_run, "split_mode", None) == "random_order_train_validation" :
		# Build the list of indices only from the usable subrange.
		indices = list(range(date_array_index_for_start_date, date_array_index_for_end_date + 1))
		seed = getattr(family_run, "random_seed_splits", None)
		if seed is not None:
			rng = random.Random(int(seed))
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
	else:
		# mix_training_and_validation: training and validation sets are chosen
		# from the first (training_set_number_of_instances + validation_set_number_of_instances) usable indices, potentially
		# intermixed, while the test set consists of the remaining tail
		# instances in sequential order, all within the trimmed range.
		print(f"   train percent {train_percent}, validation percent {validation_percent},   total instances: {usable_total}")
		training_set_number_of_instances = max(1, int(math.floor(train_percent * usable_total)))
		validation_set_number_of_instances = int(math.floor(validation_percent * usable_total))
		# Ensure we don't exceed the available instances.
		training_set_number_of_instances = min(training_set_number_of_instances, usable_total)
		validation_set_number_of_instances = min(validation_set_number_of_instances, max(0, usable_total - training_set_number_of_instances))
		mixed_n = training_set_number_of_instances + validation_set_number_of_instances

		# Indices date_array_index_for_start_date..date_array_index_for_start_date+mixed_n-1 are used for mixed
		# train/validation.
		mixed_indices = list(range(date_array_index_for_start_date, date_array_index_for_start_date + mixed_n))
		seed = getattr(family_run, "random_seed_splits", None)
		if seed is not None:
			# Use a deterministic RNG so that runs are repeatable given the same seed.
			rng = random.Random(int(seed))
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
