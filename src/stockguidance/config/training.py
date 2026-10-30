from dataclasses import asdict
import json

from stockguidance.artifacts.decoding import decode_model, integer
from stockguidance.market_data.identity import mapping
from stockguidance.models.specification import ModelSpec
from stockguidance.preprocessing import PreprocessingSpec
from stockguidance.targets.specification import TargetKind, TargetSpec, TargetSpecV2
from stockguidance.training.configuration import DeviceChoice, OptimizerChoice, Precision, TrainingConfig


def number(value: object) -> float:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise ValueError("Expected a numeric setting")
	return float(value)


def boolean(value: object) -> bool:
	if type(value) is not bool:
		raise ValueError("Expected a boolean setting")
	return value


def decode_training_configuration(value: object, model: object) -> TrainingConfig:
	fields = mapping(value)
	defaults = asdict(TrainingConfig())
	del defaults["model"]
	if set(fields) - set(defaults):
		raise ValueError("Unknown training configuration fields")
	fields = {**defaults, **fields}
	model_fields = mapping(model)
	model_defaults = mapping(json.loads(json.dumps(asdict(ModelSpec()))))
	return TrainingConfig(
		model=decode_model({**model_defaults, **model_fields}),
		epochs=integer(fields["epochs"]), batch_size=integer(fields["batch_size"]),
		learning_rate=number(fields["learning_rate"]), learning_rate_gamma=number(fields["learning_rate_gamma"]),
		optimizer=OptimizerChoice(str(fields["optimizer"])), device=DeviceChoice(str(fields["device"])), precision=Precision(str(fields["precision"])),
		seed=integer(fields["seed"]), shuffle=boolean(fields["shuffle"]), num_workers=integer(fields["num_workers"]), pin_memory=boolean(fields["pin_memory"]),
	)


def decode_target(value: object | None) -> TargetSpec:
	"""Decode the optional v2 target while keeping omitted jobs exactly v1."""

	if value is None:
		return TargetSpec()
	fields = mapping(value)
	if set(fields) != {"kind", "window_sessions"}:
		raise ValueError("Incomplete or unsupported target specification")
	kind = TargetKind(str(fields["kind"]))
	window = fields["window_sessions"]
	if window is not None and type(window) is not int:
		raise ValueError("Target window must be an integer or null")
	return TargetSpecV2(kind=kind, rolling_window_sessions=window)


def decode_preprocessing(value: object | None) -> PreprocessingSpec:
	if value is None:
		return PreprocessingSpec()
	return PreprocessingSpec.from_mapping(value)
