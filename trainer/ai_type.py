from __future__ import annotations

from enum import Enum


class AIType(Enum):
    """Type of AI strategy used by training/inference.

    Values:
        SINGLE: Train/use a single model.
        ENSEMBLE: Train/use an ensemble of models.
    """

    SINGLE = "single"
    ENSEMBLE = "ensemble"


__all__ = ["AIType"]
