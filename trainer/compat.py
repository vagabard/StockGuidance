"""Compatibility layer for optional torch dependency.

Provides TORCH_AVAILABLE and re-exports commonly used torch symbols when available.
"""

try:  # pragma: no cover - runtime dependency
    import torch  # type: ignore
    import torch.nn as nn  # type: ignore
    import torch.optim as optim  # type: ignore
    from torch.utils.data import DataLoader  # type: ignore
    import torch.nn.functional as F  # type: ignore
    TORCH_AVAILABLE = True
except Exception:  # pragma: no cover - allow memest without torch
    torch = None  # type: ignore
    nn = None  # type: ignore
    optim = None  # type: ignore
    DataLoader = None  # type: ignore
    F = None  # type: ignore
    TORCH_AVAILABLE = False

__all__ = [
    "TORCH_AVAILABLE",
    "torch",
    "nn",
    "optim",
    "DataLoader",
    "F",
]
