from __future__ import annotations

from .compat import TORCH_AVAILABLE, DataLoader


def build_dataloaders(args, train_ds, val_ds, test_ds):
    """Optionally wrap datasets into PyTorch DataLoaders based on flags.

    Returns (use_dl: bool, train_loader, val_loader, test_loader).
    """
    if not TORCH_AVAILABLE:
        return False, None, None, None
    if not bool(getattr(args, "use_dataloader", False)):
        return False, None, None, None
    # Defaults for optional args
    num_workers = int(getattr(args, "num_workers", 0) or 0)
    pin_memory = bool(getattr(args, "pin_memory", False))
    shuffle = bool(getattr(args, "shuffle", False))
    drop_last = bool(getattr(args, "drop_last", False))
    persistent_workers = bool(getattr(args, "persistent_workers", False)) and num_workers > 0

    train_loader = DataLoader(
        train_ds, batch_size=int(args.batch_size), shuffle=shuffle, num_workers=num_workers,
        pin_memory=pin_memory, drop_last=drop_last, persistent_workers=persistent_workers,
    )
    val_loader = DataLoader(
        val_ds, batch_size=int(args.batch_size), shuffle=False, num_workers=num_workers,
        pin_memory=pin_memory, drop_last=False, persistent_workers=persistent_workers,
    )
    test_loader = DataLoader(
        test_ds, batch_size=int(args.batch_size), shuffle=False, num_workers=num_workers,
        pin_memory=pin_memory, drop_last=False, persistent_workers=persistent_workers,
    )
    return True, train_loader, val_loader, test_loader


__all__ = ["build_dataloaders"]
