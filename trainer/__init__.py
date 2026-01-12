"""Trainer package: split utilities and components for training pipeline.

Modules:
- compat: optional torch_interface imports and TORCH_AVAILABLE flag
- utils: small shared helpers (counting, bytes formatting, CSV opener)
- device: device/precision setup and enforcement
- datasets: TimeTrainingInstanceDataset and batching helpers
- splits: train/val/test split helpers
- dataloaders: optional DataLoader construction
- ranking: ranking primitives and position utilities
- losses: differentiable surrogate losses
- metrics: metric computations and eval loop
- run_stage: RunStage enum for training/validation/testing
- factory: model and optimizer/scaler factories
- io_run: run parameter and artifact writing utilities
- memory: memory estimation printer
"""

from trainer.torch_interface.compat import TORCH_AVAILABLE, torch, nn, optim, DataLoader, F  # re-export for convenience
from trainer.dataset.dataset_classification import DatasetClass

__all__ = [
    "TORCH_AVAILABLE",
    "torch",
    "nn",
    "optim",
    "DataLoader",
    "F",
    "DatasetClass",
]
