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

	if len(dataset_to_wrap) == 0:
		return None
	print(f"Using {len(dataset_to_wrap)} instances for training.")
	print(f" all dataset parameters: {num_workers}, {pin_memory}, {shuffle}, {drop_last}, {persistent_workers}")
	dataset_loader = DataLoader(
		dataset_to_wrap, batch_size=int(family_run.batch_of_instances_to_train_on_at_once), shuffle=shuffle, num_workers=num_workers,
		pin_memory=pin_memory, drop_last=drop_last, persistent_workers=persistent_workers,
	)
	return dataset_loader


__all__ = ["build_dataloaders"]
