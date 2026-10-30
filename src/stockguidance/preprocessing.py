"""Pure, versioned fitting and application of training-only normalization.

The module intentionally has no model, database, CLI, or provider dependency.
Input normalization is per flattened feature coordinate.  Output normalization
is one scalar across every eligible training date and ticker so it cannot alter
within-date ranking after inversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import TYPE_CHECKING, cast

from stockguidance.config.validation import require_enum, require_text
from stockguidance.features.specification import FeatureSpecification
from stockguidance.market_data.identity import content_id, mapping, text_field
from stockguidance.targets.specification import TargetSpecification

if TYPE_CHECKING:
	import numpy as np
	from numpy.typing import NDArray


class NormalizationMethod(StrEnum):
	NONE = "none"
	STANDARD = "standard"
	MIN_MAX = "min_max"


@dataclass(frozen=True)
class PreprocessingSpec:
	input_method: NormalizationMethod = NormalizationMethod.NONE
	output_method: NormalizationMethod = NormalizationMethod.NONE
	input_axis: str = "flattened_input_coordinate"
	output_axis: str = "one_shared_scalar"
	fit_scope: str = "eligible_training_samples_only"
	version: str = "preprocessing-v2"

	def __post_init__(self) -> None:
		for name in ("input_method", "output_method"):
			value = getattr(self, name)
			if not isinstance(value, NormalizationMethod):
				try:
					object.__setattr__(self, name, NormalizationMethod(str(value)))
				except ValueError:
					raise ValueError(f"Unsupported {name}") from None
		if self.input_axis != "flattened_input_coordinate" or self.output_axis != "one_shared_scalar":
			raise ValueError("Preprocessing axes are fixed by the v2 contract")
		if self.fit_scope != "eligible_training_samples_only":
			raise ValueError("Preprocessing can only fit eligible training samples")
		if self.version != "preprocessing-v2":
			raise ValueError("Unsupported preprocessing version")

	def to_mapping(self) -> dict[str, object]:
		return {
			"version": self.version,
			"input": self.input_method.value,
			"output": self.output_method.value,
			"input_axis": self.input_axis,
			"output_axis": self.output_axis,
			"fit_scope": self.fit_scope,
		}

	@classmethod
	def from_mapping(cls, value: object) -> "PreprocessingSpec":
		fields = mapping(value)
		if set(fields) == {"input", "output"}:
			return cls(NormalizationMethod(str(fields["input"])), NormalizationMethod(str(fields["output"])))
		expected = {"version", "input", "output", "input_axis", "output_axis", "fit_scope"}
		if set(fields) != expected:
			raise ValueError("Incomplete or unsupported preprocessing specification")
		return cls(
			NormalizationMethod(str(fields["input"])),
			NormalizationMethod(str(fields["output"])),
			text_field(fields, "input_axis"), text_field(fields, "output_axis"),
			text_field(fields, "fit_scope"), text_field(fields, "version"),
		)


@dataclass(frozen=True)
class FittedTransform:
	method: NormalizationMethod
	axis: str
	location: tuple[float, ...]
	scale: tuple[float, ...]
	constant: tuple[bool, ...]
	sample_count: int

	def __post_init__(self) -> None:
		require_enum(self.method, NormalizationMethod, "normalization method")
		require_text(self.axis, "normalization axis")
		if not self.location or len(self.location) != len(self.scale) or len(self.location) != len(self.constant):
			raise ValueError("Fitted normalization parameters must have one entry per coordinate")
		if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (*self.location, *self.scale)):
			raise ValueError("Fitted normalization parameters must be finite")
		if any(value <= 0 for value in self.scale):
			raise ValueError("Fitted normalization scales must be positive")
		if any(type(value) is not bool for value in self.constant):
			raise ValueError("Constant-coordinate flags must be boolean")
		if type(self.sample_count) is not int or self.sample_count <= 0:
			raise ValueError("Fitted normalization requires positive sample count")

	def transform(self, values: object) -> "NDArray[np.float64]":
		import numpy as np

		array = cast("NDArray[np.float64]", np.asarray(values, dtype=np.float64))
		if array.ndim == 0 or not np.isfinite(array).all():
			raise ValueError("Normalization input must be finite and non-scalar")
		if self.axis == "flattened_input_coordinate":
			if array.ndim != 2 or array.shape[1] != len(self.location):
				raise ValueError("Input normalization shape or feature axis is incompatible")
			location = np.asarray(self.location, dtype=np.float64)
			scale = np.asarray(self.scale, dtype=np.float64)
			constant = np.asarray(self.constant, dtype=bool)
			result = (array - location) / scale
			result[:, constant] = 0.0
		else:
			if len(self.location) != 1:
				raise ValueError("Shared output normalization must have one scalar")
			result = (array - self.location[0]) / self.scale[0]
			if self.constant[0]:
				result[...] = 0.0
		if not np.isfinite(result).all():
			raise ValueError("Normalization produced non-finite values")
		return np.ascontiguousarray(result, dtype=np.float64)

	def inverse(self, values: object) -> "NDArray[np.float64]":
		import numpy as np

		array = cast("NDArray[np.float64]", np.asarray(values, dtype=np.float64))
		if array.ndim == 0 or not np.isfinite(array).all() or self.axis != "one_shared_scalar":
			raise ValueError("Only finite shared output values can be inverted")
		result = array * self.scale[0] + self.location[0]
		if not np.isfinite(result).all():
			raise ValueError("Inverse normalization produced non-finite values")
		return np.ascontiguousarray(result, dtype=np.float64)

	def to_mapping(self) -> dict[str, object]:
		return {
			"method": self.method.value, "axis": self.axis,
			"location": list(self.location), "scale": list(self.scale),
			"constant": list(self.constant), "sample_count": self.sample_count,
		}

	@classmethod
	def from_mapping(cls, value: object) -> "FittedTransform":
		fields = mapping(value)
		expected = {"method", "axis", "location", "scale", "constant", "sample_count"}
		if set(fields) != expected or not isinstance(fields["location"], list) or not isinstance(fields["scale"], list) or not isinstance(fields["constant"], list):
			raise ValueError("Incomplete or unsupported fitted normalization state")
		if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in (*fields["location"], *fields["scale"])) or any(type(item) is not bool for item in fields["constant"]):
			raise ValueError("Invalid fitted normalization parameter types")
		sample_count = fields["sample_count"]
		if type(sample_count) is not int:
			raise ValueError("Fitted normalization sample count must be an integer")
		return cls(
			NormalizationMethod(str(fields["method"])), text_field(fields, "axis"),
			tuple(float(item) for item in fields["location"]), tuple(float(item) for item in fields["scale"]),
			tuple(bool(item) for item in fields["constant"]), sample_count,
		)


@dataclass(frozen=True)
class FittedPreprocessing:
	specification: PreprocessingSpec
	input: FittedTransform
	output: FittedTransform
	ticker_ids: tuple[int, ...]
	feature_order: tuple[str, ...]
	target_identity: str
	data_revision: str

	def __post_init__(self) -> None:
		if not isinstance(self.specification, PreprocessingSpec) or not isinstance(self.input, FittedTransform) or not isinstance(self.output, FittedTransform):
			raise ValueError("Incomplete fitted preprocessing state")
		if self.input.axis != self.specification.input_axis or self.output.axis != self.specification.output_axis:
			raise ValueError("Fitted preprocessing axes disagree with its specification")
		if not self.ticker_ids or len(set(self.ticker_ids)) != len(self.ticker_ids):
			raise ValueError("Fitted preprocessing requires an ordered ticker universe")
		if not self.feature_order or len(set(self.feature_order)) != len(self.feature_order):
			raise ValueError("Fitted preprocessing requires an ordered feature layout")
		require_text(self.target_identity, "target identity")
		require_text(self.data_revision, "preprocessing data revision")

	@property
	def identity(self) -> str:
		return content_id(self.to_mapping())

	def validate_for(self, feature_specification: FeatureSpecification, target_specification: TargetSpecification) -> None:
		if tuple(feature_specification.ticker_ids) != self.ticker_ids:
			raise ValueError("Fitted preprocessing ticker order is incompatible")
		order = tuple(getattr(feature_specification, "channel_keys", ())) or tuple(getattr(item, "value", str(item)) for item in getattr(feature_specification, "feature_order", ()))
		if order != self.feature_order:
			raise ValueError("Fitted preprocessing feature order is incompatible")
		if getattr(target_specification, "target_identity", "") != self.target_identity:
			raise ValueError("Fitted preprocessing target specification is incompatible")
		if self.input.method is not self.specification.input_method or self.output.method is not self.specification.output_method:
			raise ValueError("Fitted preprocessing method does not match its specification")

	def transform_inputs(self, values: object) -> "NDArray[np.float64]":
		return self.input.transform(values)

	def transform_outputs(self, values: object) -> "NDArray[np.float64]":
		return self.output.transform(values)

	def inverse_outputs(self, values: object) -> "NDArray[np.float64]":
		return self.output.inverse(values)

	def to_mapping(self) -> dict[str, object]:
		return {
			"schema": "fitted-preprocessing-v2", "specification": self.specification.to_mapping(),
			"input": self.input.to_mapping(), "output": self.output.to_mapping(),
			"ticker_ids": list(self.ticker_ids), "feature_order": list(self.feature_order),
			"target_identity": self.target_identity, "data_revision": self.data_revision,
		}

	@classmethod
	def from_mapping(cls, value: object) -> "FittedPreprocessing":
		fields = mapping(value)
		expected = {"schema", "specification", "input", "output", "ticker_ids", "feature_order", "target_identity", "data_revision"}
		if set(fields) != expected or fields["schema"] != "fitted-preprocessing-v2" or not isinstance(fields["ticker_ids"], list) or not isinstance(fields["feature_order"], list):
			raise ValueError("Incomplete or unsupported fitted preprocessing state")
		if any(type(item) is not int for item in fields["ticker_ids"]) or any(type(item) is not str for item in fields["feature_order"]):
			raise ValueError("Invalid fitted preprocessing identity types")
		return cls(
			PreprocessingSpec.from_mapping(fields["specification"]), FittedTransform.from_mapping(fields["input"]), FittedTransform.from_mapping(fields["output"]),
			tuple(fields["ticker_ids"]), tuple(fields["feature_order"]),
			text_field(fields, "target_identity"), text_field(fields, "data_revision"),
		)


def _validate_training_arrays(features: object, targets: object) -> tuple["NDArray[np.float64]", "NDArray[np.float64]"]:
	import numpy as np

	inputs = cast("NDArray[np.float64]", np.asarray(features, dtype=np.float64))
	outputs = cast("NDArray[np.float64]", np.asarray(targets, dtype=np.float64))
	if inputs.ndim != 2 or outputs.ndim != 2 or inputs.shape[0] != outputs.shape[0] or inputs.shape[0] == 0 or inputs.shape[1] == 0 or outputs.shape[1] == 0:
		raise ValueError("Fitted preprocessing requires nonempty two-dimensional training arrays")
	if not np.isfinite(inputs).all() or not np.isfinite(outputs).all():
		raise ValueError("Fitted preprocessing cannot fit non-finite training arrays")
	return np.ascontiguousarray(inputs), np.ascontiguousarray(outputs)


def _fit_transform(method: NormalizationMethod, values: "NDArray[np.float64]", axis: str) -> FittedTransform:
	import numpy as np

	if axis == "flattened_input_coordinate":
		count = values.shape[1]
		flat = values
	else:
		count = 1
		flat = values.reshape(-1, 1)
	if method is NormalizationMethod.NONE:
		location, scale = np.zeros(count, dtype=np.float64), np.ones(count, dtype=np.float64)
		constant = np.zeros(count, dtype=bool)
	elif method is NormalizationMethod.STANDARD:
		location = np.mean(flat, axis=0, dtype=np.float64)
		scale = np.sqrt(np.mean((flat - location) ** 2, axis=0, dtype=np.float64))
		constant = scale == 0
		scale = np.where(constant, 1.0, scale)
	else:
		location = np.min(flat, axis=0)
		maximum = np.max(flat, axis=0)
		scale = maximum - location
		constant = scale == 0
		scale = np.where(constant, 1.0, scale)
	return FittedTransform(method, axis, tuple(float(item) for item in location), tuple(float(item) for item in scale), tuple(bool(item) for item in constant), int(values.shape[0]))


def fit_preprocessing(
	specification: PreprocessingSpec, features: object, targets: object, *,
	feature_specification: FeatureSpecification | None = None,
	target_specification: TargetSpecification | None = None,
	data_revision: str = "training-eligible-samples",
) -> FittedPreprocessing:
	"""Fit once from eligible training arrays; never inspect holdout data."""

	if not isinstance(specification, PreprocessingSpec):
		raise ValueError("Preprocessing fitting requires an explicit specification")
	inputs, outputs = _validate_training_arrays(features, targets)
	if feature_specification is None:
		ticker_ids, feature_order = tuple(range(1, outputs.shape[1] + 1)), tuple(f"input_{index}" for index in range(inputs.shape[1]))
	else:
		ticker_ids = tuple(feature_specification.ticker_ids)
		feature_order = tuple(getattr(feature_specification, "channel_keys", ())) or tuple(getattr(item, "value", str(item)) for item in getattr(feature_specification, "feature_order", ()))
		if len(feature_order) * len(ticker_ids) * feature_specification.lookback_sessions != inputs.shape[1]:
			raise ValueError("Input array width disagrees with the feature specification")
	if target_specification is None:
		target_identity = "unspecified-target"
	else:
		target_identity = target_specification.target_identity
	if not data_revision:
		raise ValueError("Preprocessing requires a data revision")
	return FittedPreprocessing(specification, _fit_transform(specification.input_method, inputs, specification.input_axis), _fit_transform(specification.output_method, outputs, specification.output_axis), ticker_ids, feature_order, target_identity, data_revision)


def fit_training_preprocessing(dataset: object, specification: PreprocessingSpec, *, data_revision: str = "") -> FittedPreprocessing:
	"""Fit from a ``TrainingDataset``'s eligible samples only."""

	from stockguidance.datasets.numpy_arrays import training_arrays
	from stockguidance.datasets.training import TrainingDataset

	if not isinstance(dataset, TrainingDataset):
		raise ValueError("Training preprocessing requires a TrainingDataset")
	if dataset.preprocessing is not None:
		raise ValueError("A fitted training dataset cannot be fitted again")
	features, targets = training_arrays(dataset)
	first = dataset.eligible[0]
	return fit_preprocessing(specification, features, targets, feature_specification=first.features.specification, target_specification=first.target_specification, data_revision=data_revision or dataset.revision)


__all__ = ["FittedPreprocessing", "FittedTransform", "NormalizationMethod", "PreprocessingSpec", "fit_preprocessing", "fit_training_preprocessing"]
