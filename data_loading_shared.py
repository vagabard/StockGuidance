"""
Shared helpers for loading DB rows into NumPy arrays and optional PyTorch tensors.

This module is intentionally tiny to avoid duplication across loader scripts.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

try:  # optional torch dependency
    import torch  # type: ignore
    TORCH_AVAILABLE = True
except Exception:  # pragma: no cover - allow usage without torch
    torch = None  # type: ignore
    TORCH_AVAILABLE = False


def to_torch(keys_np: np.ndarray, features_np: np.ndarray, labels_np: Optional[np.ndarray] = None):
    """Convert NumPy arrays to torch tensors (if torch is installed).

    - keys_np is converted to int64
    - features_np/labels_np are converted to float32
    """
    if not TORCH_AVAILABLE:  # pragma: no cover - simple runtime guard
        raise RuntimeError("PyTorch is not available. Install 'torch' or omit --to-torch.")
    keys_t = torch.from_numpy(keys_np.astype(np.int64, copy=False))
    feats_t = torch.from_numpy(features_np.astype(np.float32, copy=False))
    if labels_np is not None:
        labels_t = torch.from_numpy(labels_np.astype(np.float32, copy=False))
        return keys_t, feats_t, labels_t
    return keys_t, feats_t
