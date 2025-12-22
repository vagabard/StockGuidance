from __future__ import annotations

from typing import TYPE_CHECKING

from trainer.torch_interface.compat import TORCH_AVAILABLE, DataLoader

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
	from trainer.state.family_run import FamilyRun


def build_dataloaders(family_run: FamilyRun, train_ds, val_ds, test_ds) -> tuple[DataLoader | None, DataLoader | None, DataLoader | None]:
	"""Optionally wrap datasets into PyTorch DataLoaders based on flags.

	Returns (train_loader, val_loader, test_loader).
	"""
	if not TORCH_AVAILABLE:
		return None, None, None
	if family_run.dont_use_dataloader:
		return None, None, None
	# Defaults for optional args
	num_workers = family_run.number_of_GPU_workers
	pin_memory = family_run.pin_GPU_memory
	shuffle = family_run.shuffle_instances_in_training_set
	drop_last = family_run.drop_last
	persistent_workers = family_run.persistent_workers and num_workers > 0

	train_loader = DataLoader(
		train_ds, batch_size=int(family_run.batch_of_instances_to_train_on_at_once), shuffle=shuffle, num_workers=num_workers,
		pin_memory=pin_memory, drop_last=drop_last, persistent_workers=persistent_workers,
	)
	val_loader = DataLoader(
		val_ds, batch_size=int(family_run.batch_of_instances_to_train_on_at_once), shuffle=False, num_workers=num_workers,
		pin_memory=pin_memory, drop_last=False, persistent_workers=persistent_workers,
	)
	test_loader = DataLoader(
		test_ds, batch_size=int(family_run.batch_of_instances_to_train_on_at_once), shuffle=False, num_workers=num_workers,
		pin_memory=pin_memory, drop_last=False, persistent_workers=persistent_workers,
	)
	return train_loader, val_loader, test_loader


__all__ = ["build_dataloaders"]
