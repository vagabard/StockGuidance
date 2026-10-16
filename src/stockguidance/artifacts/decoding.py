from dataclasses import asdict
from datetime import date, datetime

from stockguidance.artifacts.contracts import ArtifactMetadata, ModelSemantics, TrainingProvenance
from stockguidance.config.source import PriceAdjustment
from stockguidance.features.specification import CloseFeature, FeatureSpec, MissingHistoryPolicy, SignalMode
from stockguidance.market_data.identity import decode_source, mapping, text_field
from stockguidance.market_data.intervals import ObservationInterval
from stockguidance.models.specification import Activation, Architecture, BiasMode, LayerType, ModelSpec
from stockguidance.targets.specification import ExecutionPrice, HoldingHorizon, TargetSpec, TargetTransform


def exact_mapping(value: object, keys: tuple[str, ...]) -> dict[str, object]:
	result = mapping(value)
	if set(result) != set(keys):
		raise ValueError("Incomplete or unsupported metadata fields")
	return result


def integer(value: object) -> int:
	if type(value) is not int:
		raise ValueError("Expected an integer")
	return value


def strings(value: object) -> tuple[str, ...]:
	if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
		raise ValueError("Expected an ordered text array")
	return tuple(str(item) for item in value)


def integers(value: object) -> tuple[int, ...]:
	if not isinstance(value, list):
		raise ValueError("Expected an ordered integer array")
	return tuple(integer(item) for item in value)


def decode_features(value: object) -> FeatureSpec:
	fields = exact_mapping(value, ("ticker_ids", "lookback_sessions", "signal_mode", "interval", "missing_history", "feature_order", "adjustment"))
	return FeatureSpec(integers(fields["ticker_ids"]), integer(fields["lookback_sessions"]), SignalMode(text_field(fields, "signal_mode")), ObservationInterval(text_field(fields, "interval")), MissingHistoryPolicy(text_field(fields, "missing_history")), tuple(CloseFeature(item) for item in strings(fields["feature_order"])), PriceAdjustment(text_field(fields, "adjustment")))


def decode_model(value: object) -> ModelSpec:
	fields = exact_mapping(value, ("architecture", "hidden_widths", "activation", "layer_types", "bias", "layer_normalization", "final_activation", "implementation_version"))
	normalization = fields["layer_normalization"]
	if type(normalization) is not bool:
		raise ValueError("Expected boolean layer normalization")
	return ModelSpec(Architecture(text_field(fields, "architecture")), integers(fields["hidden_widths"]), Activation(text_field(fields, "activation")), tuple(LayerType(item) for item in strings(fields["layer_types"])), BiasMode(text_field(fields, "bias")), normalization, text_field(fields, "final_activation"), text_field(fields, "implementation_version"))


def decode_semantics(value: object) -> ModelSemantics:
	fields = exact_mapping(value, ("source", "features", "target", "calendar_id", "calendar_version", "execution_assumption"))
	target = exact_mapping(fields["target"], ("horizon", "entry_price", "exit_price", "transform"))
	source = exact_mapping(fields["source"], ("provider", "endpoint", "market", "universe", "adjustment", "interval"))
	return ModelSemantics(decode_source(source), decode_features(fields["features"]), TargetSpec(HoldingHorizon(text_field(target, "horizon")), ExecutionPrice(text_field(target, "entry_price")), ExecutionPrice(text_field(target, "exit_price")), TargetTransform(text_field(target, "transform"))), text_field(fields, "calendar_id"), text_field(fields, "calendar_version"), text_field(fields, "execution_assumption"))


def metadata_payload(metadata: ArtifactMetadata) -> dict[str, object]:
	training = asdict(metadata.training)
	for name in ("training_cutoff", "information_cutoff", "first_entry", "last_entry", "last_exit", "last_exit_close_at"):
		training[name] = training[name].isoformat()
	features = metadata.semantics.features
	payload: dict[str, object] = {"semantics": asdict(metadata.semantics), "model": asdict(metadata.model), "training": training, "framework_version": metadata.framework_version, "weight_dtype": metadata.weight_dtype, "feature_version": features.version, "layout": features.layout, "target_formula": metadata.semantics.target.raw_formula, "preprocessing": features.preprocessing}
	if features.opening_contract is not None:
		payload["opening_contract"] = features.opening_contract
	return payload


def decode_metadata(value: object) -> ArtifactMetadata:
	semantics = decode_semantics(mapping(value).get("semantics"))
	opening_keys = () if semantics.features.opening_contract is None else ("opening_contract",)
	fields = exact_mapping(value, ("semantics", "model", "training", "framework_version", "weight_dtype", "feature_version", "layout", "target_formula", "preprocessing", *opening_keys))
	if fields["feature_version"] != semantics.features.version or fields["layout"] != semantics.features.layout or fields["target_formula"] != semantics.target.raw_formula or fields["preprocessing"] != semantics.features.preprocessing or fields.get("opening_contract") != semantics.features.opening_contract:
		raise ValueError("Unsupported numerical/execution specification; legacy models require retraining")
	training = exact_mapping(fields["training"], ("training_cutoff", "information_cutoff", "first_entry", "last_entry", "last_exit", "last_exit_close_at", "data_revision", "sample_keys", "excluded_sample_keys", "seed", "completed_epochs"))
	provenance = TrainingProvenance(datetime.fromisoformat(text_field(training, "training_cutoff")), datetime.fromisoformat(text_field(training, "information_cutoff")), date.fromisoformat(text_field(training, "first_entry")), date.fromisoformat(text_field(training, "last_entry")), date.fromisoformat(text_field(training, "last_exit")), datetime.fromisoformat(text_field(training, "last_exit_close_at")), text_field(training, "data_revision"), strings(training["sample_keys"]), strings(training["excluded_sample_keys"]), integer(training["seed"]), integer(training["completed_epochs"]))
	return ArtifactMetadata(semantics, decode_model(fields["model"]), provenance, text_field(fields, "framework_version"), text_field(fields, "weight_dtype"))
