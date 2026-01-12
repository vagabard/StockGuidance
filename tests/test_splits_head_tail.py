from __future__ import annotations

import types
from trainer.dataset.datasets import TimeTrainingInstanceDataset, TimeTrainingInstanceSubset
from trainer.dataset.time_training_instance import TimeTrainingInstance
from trainer.splits import split_datasets


class _DummyProvider:
	"""Minimal provider implementing enough for split_datasets and TimeTrainingInstanceDataset."""

	def __init__(self, max_inst=100):
		self.max_inst = max_inst
		# For split_datasets to resolve dates
		self.cumulative_dates_for_train_validation_and_test_sets = [20210101 + i for i in range(max_inst)]
		self.number_of_prior_days_to_include_in_feature = 0

	def get_maximum_number_of_test_train_instances(self):
		return self.max_inst

	def get_time_training_instance(self, idx: int) -> TimeTrainingInstance:
		# TrainingInstanceProvider uses 1-based indexing
		return TimeTrainingInstance(
			feature_vector=[float(idx)],
			date=types.SimpleNamespace(), # Mock date
			date_id=20210101 + idx - 1,
			training_label=[float(idx)],
			num_days=1
		)


def _make_family_run(split_mode: str):
	family_run = types.SimpleNamespace()
	family_run.split_mode = split_mode
	family_run.random_seed_splits = 123
	return family_run


def _extract_indices(ds):
	if isinstance(ds, TimeTrainingInstanceSubset):
		return list(ds.indices)
	return []


def test_sequential_head_tail_trimming():
	max_inst = 100
	provider = _DummyProvider(max_inst)
	family_run = _make_family_run("sequential")

	# split_datasets now requires start/end date_id instead of head/tail percent
	start_id = 20210101 + 70 # Index 71
	end_id = 20210101 + 89   # Index 90

	train_ds, val_ds, test_ds = split_datasets(
		provider, family_run, max_inst,
		train_validation_and_test_set_start_date_id=start_id,
		train_validation_and_test_set_end_date_id=end_id,
		train_percent=0.5, validation_percent=0.25
	)

	train_idx = [i + 1 for i in _extract_indices(train_ds)]
	val_idx = [i + 1 for i in _extract_indices(val_ds)]
	test_idx = [i + 1 for i in _extract_indices(test_ds)]

	# Usable slice should be from 71..90 inclusive (20 instances)
	all_idx = train_idx + val_idx + test_idx
	assert min(all_idx) >= 71
	assert max(all_idx) <= 90
	assert len(all_idx) == 20


def test_random_order_head_tail_trimming():
	max_inst = 100
	provider = _DummyProvider(max_inst)
	family_run = _make_family_run("random_order_train_validation")

	start_id = 20210101 + 70
	end_id = 20210101 + 89

	train_ds, val_ds, test_ds = split_datasets(
		provider, family_run, max_inst,
		train_validation_and_test_set_start_date_id=start_id,
		train_validation_and_test_set_end_date_id=end_id,
		train_percent=0.5, validation_percent=0.25
	)

	train_idx = [i + 1 for i in _extract_indices(train_ds)]
	val_idx = [i + 1 for i in _extract_indices(val_ds)]
	test_idx = [i + 1 for i in _extract_indices(test_ds)]

	all_idx = train_idx + val_idx + test_idx
	assert min(all_idx) >= 71
	assert max(all_idx) <= 90
	assert len(all_idx) == 20


def test_mix_training_and_validation_head_tail_trimming():
	max_inst = 100
	provider = _DummyProvider(max_inst)
	family_run = _make_family_run("mix_training_and_validation")

	start_id = 20210101 + 70
	end_id = 20210101 + 89

	train_ds, val_ds, test_ds = split_datasets(
		provider, family_run, max_inst,
		train_validation_and_test_set_start_date_id=start_id,
		train_validation_and_test_set_end_date_id=end_id,
		train_percent=0.5, validation_percent=0.25
	)

	train_idx = [i + 1 for i in _extract_indices(train_ds)]
	val_idx = [i + 1 for i in _extract_indices(val_ds)]
	test_idx = [i + 1 for i in _extract_indices(test_ds)]

	all_idx = train_idx + val_idx + test_idx
	assert min(all_idx) >= 71
	assert max(all_idx) <= 90
	assert len(all_idx) == 20
