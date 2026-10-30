from __future__ import annotations

from typing import TYPE_CHECKING

from stockguidance.datasets.training import TrainingDataset
from stockguidance.features.specification import FeatureSpecification

if TYPE_CHECKING:
	import numpy as np
	from numpy.typing import NDArray


def validate_arrays(features: object, targets: object, specification: FeatureSpecification) -> None:
	import numpy as np

	if not isinstance(features, np.ndarray) or not isinstance(targets, np.ndarray):
		raise ValueError("Numerical boundary requires NumPy arrays")
	if features.dtype != np.float64 or targets.dtype != np.float64 or features.ndim != 2 or targets.ndim != 2:
		raise ValueError("Arrays must be two-dimensional float64 matrices")
	if features.shape != (targets.shape[0], specification.input_size) or targets.shape[1] != len(specification.ticker_ids):
		raise ValueError("Array shapes disagree with the explicit sample layout")
	if not features.flags.c_contiguous or not targets.flags.c_contiguous or not np.isfinite(features).all() or not np.isfinite(targets).all():
		raise ValueError("Arrays must be contiguous and finite; unavailable samples cannot be sanitized")


def training_arrays(dataset: TrainingDataset) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
	import numpy as np

	eligible = dataset.eligible
	if not eligible:
		raise ValueError("No training samples have complete features and mature targets")
	specification = eligible[0].features.specification
	features = np.asarray([sample.features.values for sample in eligible], dtype=np.float64)
	targets = np.asarray([sample.targets.transformed for sample in eligible], dtype=np.float64)
	validate_arrays(features, targets, specification)
	return features, targets
