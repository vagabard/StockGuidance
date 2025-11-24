from __future__ import annotations

"""Tests that split_datasets correctly handles floating-point
data-head-ignore-percent and data-tail-ignore-percent when used with a
TrainingInstanceProvider-backed dataset.

We avoid touching the real database by monkeypatching
load_database_rows_into_tensors.fetch_grouped_by_date to return a
deterministic synthetic dataset. The goal is to:

* Use whatever dataset size the provider exposes by default (given our
  patched fetch helper).
* Apply non-trivial floating-point head/tail ignore fractions.
* Compute the analytically expected usable subset size after trimming.
* Compare this against the total number of instances that
  trainer.splits.split_datasets actually assigns to train/val/test.
"""

import types

import numpy as np

from trainer.splits import split_datasets
from training_instance_provider import TrainingInstanceProvider


def _extract_indices(ds):

	if hasattr(ds, "indices"):
		return list(ds.indices)
	start = int(getattr(ds, "start_idx", 1))
	end = int(getattr(ds, "end_idx", 0))
	if end < start:
		return []
	return list(range(start, end + 1))


def test_split_datasets_uses_expected_window_with_floating_head_tail(monkeypatch):
	"""End-to-end style test for split_datasets with a real provider.

	We:

	* Patch fetch_grouped_by_date to return a synthetic dataset with a
	  fixed number of "dates".
	* Build a TrainingInstanceProvider that uses this data.
	* Ask it for the number of available instances with zero prior days.
	* Apply floating-point head/tail ignore fractions and call
	  split_datasets in sequential mode.
	* Verify that the total number of instances across train/val/test
	  matches the analytically expected usable window size.
	"""

	# Number of synthetic dates we want the provider to see. Keeping this
	# non-round so fractional trimming has to go through floor/ceil logic.
	#D = 137
	#T = 5
	#F = 3

	def _fake_fetch_grouped_by_date(*, host, database, user, password, port, driver, limit, return_2d, current_day_change, volume_ratio, vwap_ratio, tranaction_ratio, day_change_centered):  # noqa: D401,E501
		"""Return synthetic (dates, X_flat, Y) for provider tests.

		We ignore all arguments and just construct small deterministic
		arrays of the right shapes. Only the number of rows (D) matters for
		this test, because TrainingInstanceProvider derives its instance
		count from the number of dates.
		"""

		dates = np.arange(1, D + 1, dtype=np.int32)
		X_flat = np.zeros((D, T * F), dtype=np.float64)
		Y = np.zeros((D, T), dtype=np.float64)
		return dates, X_flat, Y

	import load_database_rows_into_tensors as ldr

	monkeypatch.setattr(ldr, "fetch_grouped_by_date", _fake_fetch_grouped_by_date)

	provider = TrainingInstanceProvider(use_torch=False, limit=None)

	provider.set_how_many_days_in_past_to_use(0)
	max_inst = provider.get_maximum_number_of_test_train_instances()

	#assert max_inst == D

	head_frac = 0.123
	tail_frac = 1.0- 0.188

	args = types.SimpleNamespace()
	args.split_mode = "sequential"
	args.seed = 42
	args.data_head_ignore_percent = head_frac
	args.data_tail_ignore_percent = tail_frac


	train_ds, val_ds, test_ds = split_datasets(
		provider,
		args,
		max_inst,
		train_percent=0.6,
		validation_percent=0.25,
	)

	all_idx = _extract_indices(train_ds) + _extract_indices(val_ds) + _extract_indices(test_ds)

	import math

	head_frac_clamped = max(0.0, min(float(head_frac), 1.0))
	tail_frac_clamped = max(0.0, min(float(tail_frac), 1.0))
	base_start = int(math.floor(head_frac_clamped * max_inst)) + 1
	base_end = int(math.floor((1.0 - tail_frac_clamped) * max_inst))
	if base_start < 1:
		base_start = 1
	if base_end > max_inst:
		base_end = max_inst

	if base_end < base_start:
		expected_total = 0
	else:
		expected_total = base_end - base_start + 1

	print (f"head_frac={head_frac}, tail_frac={tail_frac}, expected_total={expected_total}, all idx: {len(all_idx)}")
	assert len(all_idx) == expected_total

	if all_idx:
		assert min(all_idx) >= base_start
		assert max(all_idx) <= base_end
