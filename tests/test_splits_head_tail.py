from __future__ import annotations

import types

from trainer.splits import split_datasets


class _DummyProvider:
	"""Minimal provider implementing get_feature_and_label_pair for Range/IndexDataset.

	We only care about indices, so the actual tensors/arrays are irrelevant; we
	return the index itself as both feature and label for easy inspection.
	"""

	def get_feature_and_label_pair(self, idx: int):
		return (idx, idx)


def _make_args(split_mode: str, head: float = 0.0, tail: float = 0.0):
	args = types.SimpleNamespace()
	args.split_mode = split_mode
	args.seed = 123
	args.data_head_ignore_percent = head
	args.data_tail_ignore_percent = tail
	return args


def _extract_indices(ds):
	# RangeDataset has start_idx/end_idx; IndexDataset has indices list.
	if hasattr(ds, "indices"):
		return list(ds.indices)
	return list(range(int(ds.start_idx), int(ds.end_idx) + 1))


def test_sequential_head_tail_trimming():
	provider = _DummyProvider()
	max_inst = 100
	args = _make_args("sequential", head=0.70, tail=0.10)
	train_ds, val_ds, test_ds = split_datasets(provider, args, max_inst, train_percent=0.5, validation_percent=0.25)

	train_idx = _extract_indices(train_ds)
	val_idx = _extract_indices(val_ds)
	test_idx = _extract_indices(test_ds)

	# Usable slice should be from 71..90 inclusive (20 instances)
	assert min(train_idx + val_idx + test_idx) >= 71
	assert max(train_idx + val_idx + test_idx) <= 90
	# Total count should equal size of slice
	assert len(train_idx) + len(val_idx) + len(test_idx) == 20


def test_random_order_head_tail_trimming():
	provider = _DummyProvider()
	max_inst = 100
	args = _make_args("random_order_train_validation", head=0.70, tail=0.10)
	train_ds, val_ds, test_ds = split_datasets(provider, args, max_inst, train_percent=0.5, validation_percent=0.25)

	train_idx = sorted(_extract_indices(train_ds))
	val_idx = sorted(_extract_indices(val_ds))
	test_idx = sorted(_extract_indices(test_ds))

	all_idx = train_idx + val_idx + test_idx
	assert min(all_idx) >= 71
	assert max(all_idx) <= 90
	assert len(all_idx) == 20


def test_mix_training_and_validation_head_tail_trimming():
	provider = _DummyProvider()
	max_inst = 100
	args = _make_args("mix_training_and_validation", head=0.70, tail=0.10)
	train_ds, val_ds, test_ds = split_datasets(provider, args, max_inst, train_percent=0.5, validation_percent=0.25)

	train_idx = sorted(_extract_indices(train_ds))
	val_idx = sorted(_extract_indices(val_ds))
	test_idx = sorted(_extract_indices(test_ds))

	all_idx = train_idx + val_idx + test_idx
	assert min(all_idx) >= 71
	assert max(all_idx) <= 90
	assert len(all_idx) == 20
