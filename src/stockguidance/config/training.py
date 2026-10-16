from dataclasses import asdict
import json

from stockguidance.artifacts.decoding import decode_model, integer
from stockguidance.market_data.identity import mapping
from stockguidance.models.specification import ModelSpec
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
