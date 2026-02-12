from __future__ import annotations

from typing import TYPE_CHECKING, Any

from torch.utils.data import DataLoader

from trainer.torch_interface.compat import TORCH_AVAILABLE, DataLoader

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
	from trainer.state.family_run import FamilyRun


def build_dataloaders(family_run: FamilyRun, dataset_to_wrap) -> DataLoader[Any] | None:
	"""Optionally wrap datasets into PyTorch DataLoaders based on flags.

	Returns (train_loader, val_loader, test_loader).
	"""
	if not TORCH_AVAILABLE:
		return None
	if family_run.dont_use_dataloader:
		return None
	# Defaults for optional args
	num_workers = family_run.number_of_GPU_workers
	pin_memory = family_run.pin_GPU_memory
	shuffle = family_run.shuffle_instances_in_training_set
	drop_last = family_run.drop_last
	persistent_workers = num_workers > 0 if family_run.persistent_workers is None else (family_run.persistent_workers and num_workers > 0)
	batch_size = int(family_run.batch_of_instances_to_train_on_at_once)

	# Guard against massive per-batch memory usage (feature windows can be huge)
	estimated_batch_bytes = None
	provider = getattr(dataset_to_wrap, "provider", None)
	if provider is not None:
		try:
			prior_days = int(getattr(provider, "number_of_prior_days_to_include_in_feature", 0))
			ticker_feature_quantity = int(getattr(provider, "ticker_feature_quantity", 0))
			input_size = (prior_days + 1) * ticker_feature_quantity
			estimated_batch_bytes = input_size * 4 * batch_size
		except Exception:
			estimated_batch_bytes = None

	if estimated_batch_bytes is not None and num_workers and num_workers > 0:
		# If each batch is extremely large, disable multiprocessing to avoid prefetch blow-ups.
		# 512 MB threshold keeps memory predictable on Windows/spawn.
		if estimated_batch_bytes >= 512 * 1024 * 1024:
			print(
				f"Large batch estimate: {estimated_batch_bytes / (1024**3):.2f} GB. "
				"Disabling DataLoader workers/pin_memory to reduce RAM pressure."
			)
			num_workers = 0
			persistent_workers = False
			pin_memory = False

	prefetch_factor = 1 if num_workers and num_workers > 0 else None

	if len(dataset_to_wrap) == 0:
		return None
	print(f"Using {len(dataset_to_wrap)} instances for training.")
	print(f" all dataset parameters: {num_workers}, {pin_memory}, {shuffle}, {drop_last}, {persistent_workers}")
	if estimated_batch_bytes is not None:
		print(f" estimated batch bytes: {estimated_batch_bytes / (1024**3):.2f} GB")
	loader_kwargs = dict(
		batch_size=batch_size,
		shuffle=shuffle,
		num_workers=num_workers,
		pin_memory=pin_memory,
		drop_last=drop_last,
		persistent_workers=persistent_workers,
	)
	if prefetch_factor is not None:
		loader_kwargs["prefetch_factor"] = prefetch_factor

	# If using workers on Windows (spawn), ensure we don't leak memory by being careful with shared data
	dataset_loader = DataLoader(dataset_to_wrap, **loader_kwargs)
	return dataset_loader


__all__ = ["build_dataloaders"]
