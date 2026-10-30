from dataclasses import asdict
from datetime import date, datetime

from stockguidance.artifacts.contracts import ArtifactMetadata, ArtifactUniverse, ModelSemantics, TrainingProvenance
from stockguidance.config.source import PriceAdjustment
from stockguidance.features.specification import CloseFeature, FeatureSpec, FeatureSpecV2, MissingHistoryPolicy, SignalMode
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.identity import decode_source, mapping, text_field
from stockguidance.market_data.intervals import ObservationInterval
from stockguidance.models.specification import Activation, Architecture, BiasMode, LayerType, ModelSpec
from stockguidance.targets.specification import ExecutionPrice, HoldingHorizon, TargetKind, TargetSpec, TargetSpecV2, TargetTransform, TargetSpecification
from stockguidance.preprocessing import FittedPreprocessing


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


def decode_features(value: object) -> FeatureSpec | FeatureSpecV2:
	fields = mapping(value)
	if fields.get("schema") == "configured-features-v2" or "channels" in fields:
		if set(fields) != {"schema", "ticker_ids", "lookback_sessions", "channels", "signal_mode", "interval", "missing_history", "adjustment", "layout"} or fields["schema"] != "configured-features-v2":
			raise ValueError("Incomplete or unsupported v2 feature fields")
		channels = strings(fields["channels"])
		features = FeatureSpecV2(
			integers(fields["ticker_ids"]), integer(fields["lookback_sessions"]), channels,
			SignalMode(text_field(fields, "signal_mode")), ObservationInterval(text_field(fields, "interval")),
			MissingHistoryPolicy(text_field(fields, "missing_history")), PriceAdjustment(text_field(fields, "adjustment")),
		)
		if features.layout != fields["layout"]:
			raise ValueError("Feature layout does not match the v2 contract")
		return features
	fields = exact_mapping(fields, ("ticker_ids", "lookback_sessions", "signal_mode", "interval", "missing_history", "feature_order", "adjustment"))
	return FeatureSpec(integers(fields["ticker_ids"]), integer(fields["lookback_sessions"]), SignalMode(text_field(fields, "signal_mode")), ObservationInterval(text_field(fields, "interval")), MissingHistoryPolicy(text_field(fields, "missing_history")), tuple(CloseFeature(item) for item in strings(fields["feature_order"])), PriceAdjustment(text_field(fields, "adjustment")))


def decode_model(value: object) -> ModelSpec:
	fields = mapping(value)
	legacy_keys = {"architecture", "hidden_widths", "activation", "layer_types", "bias", "layer_normalization", "final_activation", "implementation_version"}
	v2_keys = {*legacy_keys, "hidden_activations", "shared_hidden_activation"}
	if set(fields) not in (legacy_keys, v2_keys):
		raise ValueError("Incomplete or unsupported model fields")
	normalization = fields["layer_normalization"]
	if type(normalization) is not bool:
		raise ValueError("Expected boolean layer normalization")
	version = text_field(fields, "implementation_version")
	final_text = text_field(fields, "final_activation")
	final_activation = "none" if final_text == "none" else Activation(final_text)
	if version == "stockguidance-models-v1":
		if set(fields) == v2_keys and (fields["hidden_activations"] is not None or fields["shared_hidden_activation"] is not None):
			raise ValueError("v1 models cannot contain v2 activation controls")
		return ModelSpec(Architecture(text_field(fields, "architecture")), integers(fields["hidden_widths"]), Activation(text_field(fields, "activation")), tuple(LayerType(item) for item in strings(fields["layer_types"])), BiasMode(text_field(fields, "bias")), normalization, "none", version)
	if version != "stockguidance-models-v2" or set(fields) != v2_keys:
		raise ValueError("v2 model metadata requires explicit activation controls")
	hidden_values = fields["hidden_activations"]
	if hidden_values is not None and not isinstance(hidden_values, list):
		raise ValueError("Expected an ordered hidden activation array or null")
	shared_value = fields["shared_hidden_activation"]
	if shared_value is not None and not isinstance(shared_value, str):
		raise ValueError("Expected a shared hidden activation or null")
	return ModelSpec(
		architecture=Architecture(text_field(fields, "architecture")), hidden_widths=integers(fields["hidden_widths"]), activation=Activation(text_field(fields, "activation")),
		layer_types=tuple(LayerType(item) for item in strings(fields["layer_types"])), bias=BiasMode(text_field(fields, "bias")), layer_normalization=normalization,
		final_activation=final_activation, implementation_version=version,
		hidden_activations=None if hidden_values is None else tuple(Activation(str(item)) for item in hidden_values),
		shared_hidden_activation=None if shared_value is None else Activation(shared_value),
	)


def decode_target(value: object) -> TargetSpecification:
	fields = mapping(value)
	if "kind" in fields:
		if set(fields) != {"schema", "kind", "horizon", "entry_price", "exit_price", "return_formula", "rolling_window_sessions"} or fields["schema"] != "targets-v2":
			raise ValueError("Incomplete or unsupported v2 target fields")
		target = TargetSpecV2(kind=TargetKind(text_field(fields, "kind")), rolling_window_sessions=None if fields["rolling_window_sessions"] is None else integer(fields["rolling_window_sessions"]))
		if fields["horizon"] != target.horizon.value or fields["entry_price"] != target.entry_price.value or fields["exit_price"] != target.exit_price.value or fields["return_formula"] != target.raw_formula:
			raise ValueError("Target metadata does not match its versioned target kind")
		return target
	if fields.get("schema") == "targets-v1":
		fields = {key: value for key, value in fields.items() if key != "schema"}
	fields = exact_mapping(fields, ("horizon", "entry_price", "exit_price", "transform"))
	return TargetSpec(HoldingHorizon(text_field(fields, "horizon")), ExecutionPrice(text_field(fields, "entry_price")), ExecutionPrice(text_field(fields, "exit_price")), TargetTransform(text_field(fields, "transform")))


def decode_semantics(value: object) -> ModelSemantics:
	fields = mapping(value)
	required = ("source", "features", "target", "calendar_id", "calendar_version", "execution_assumption")
	if set(fields) - {*required, "availability_policy"} or any(name not in fields for name in required):
		raise ValueError("Incomplete or unsupported metadata fields")
	source = exact_mapping(fields["source"], ("provider", "endpoint", "market", "universe", "adjustment", "interval"))
	return ModelSemantics(decode_source(source), decode_features(fields["features"]), decode_target(fields["target"]), text_field(fields, "calendar_id"), text_field(fields, "calendar_version"), text_field(fields, "execution_assumption"), AvailabilityPolicy(str(fields.get("availability_policy", AvailabilityPolicy.STRICT.value))))


def metadata_payload(metadata: ArtifactMetadata) -> dict[str, object]:
	training = asdict(metadata.training)
	for name in ("training_cutoff", "information_cutoff", "first_entry", "last_entry", "last_exit", "last_exit_close_at"):
		training[name] = training[name].isoformat()
	if training["data_snapshot"] is not None:
		training["data_snapshot"] = training["data_snapshot"].isoformat()
	features = metadata.semantics.features
	if metadata.versioned:
		if metadata.preprocessing is None or metadata.loss is None or metadata.universe is None:
			raise ValueError("Versioned artifacts require fitted preprocessing, loss, and universe metadata")
		if isinstance(features, FeatureSpecV2):
			feature_mapping: dict[str, object] = features.to_mapping()
		else:
			feature_mapping = {
				"ticker_ids": list(features.ticker_ids), "lookback_sessions": features.lookback_sessions,
				"signal_mode": features.signal_mode.value, "interval": features.interval.value,
				"missing_history": features.missing_history.value, "feature_order": [item.value for item in features.feature_order],
				"adjustment": features.adjustment.value,
			}
		payload: dict[str, object] = {
			"artifact_metadata_version": "stockguidance-artifact-metadata-v2",
			"semantics": {
			"source": asdict(metadata.semantics.source), "features": feature_mapping, "target": metadata.semantics.target.to_mapping(),
				"calendar_id": metadata.semantics.calendar_id, "calendar_version": metadata.semantics.calendar_version,
				"execution_assumption": metadata.semantics.execution_assumption, "availability_policy": metadata.semantics.availability_policy.value,
			},
			"model": metadata.model.to_mapping(), "training": training, "framework_version": metadata.framework_version, "weight_dtype": metadata.weight_dtype,
			"feature_version": features.version, "layout": features.layout, "target_formula": metadata.semantics.target.raw_formula,
			"preprocessing": metadata.preprocessing.to_mapping(), "loss": metadata.loss.to_mapping(), "universe": metadata.universe.to_mapping(),
			"availability_assumption": metadata.semantics.availability_policy.assumption,
		}
		return payload
	if not isinstance(features, FeatureSpec):
		raise ValueError("Legacy artifact metadata cannot contain configured feature semantics")
	legacy_payload: dict[str, object] = {"semantics": asdict(metadata.semantics), "model": metadata.model.to_mapping(), "training": training, "framework_version": metadata.framework_version, "weight_dtype": metadata.weight_dtype, "feature_version": features.version, "layout": features.layout, "target_formula": metadata.semantics.target.raw_formula, "preprocessing": features.preprocessing, "availability_assumption": metadata.semantics.availability_policy.assumption}
	if features.opening_contract is not None:
		legacy_payload["opening_contract"] = features.opening_contract
	return legacy_payload


def decode_metadata(value: object) -> ArtifactMetadata:
	if mapping(value).get("artifact_metadata_version") == "stockguidance-artifact-metadata-v2":
		return _decode_v2_metadata(mapping(value))
	semantics = decode_semantics(mapping(value).get("semantics"))
	if not isinstance(semantics.features, FeatureSpec):
		raise ValueError("Legacy artifact metadata cannot contain configured feature semantics")
	opening_keys = () if semantics.features.opening_contract is None else ("opening_contract",)
	fields = mapping(value)
	required = ("semantics", "model", "training", "framework_version", "weight_dtype", "feature_version", "layout", "target_formula", "preprocessing")
	optional = ("availability_assumption", *opening_keys)
	if set(fields) - {*required, *optional} or any(name not in fields for name in required):
		raise ValueError("Incomplete or unsupported metadata fields")
	if fields.get("availability_assumption", semantics.availability_policy.assumption) != semantics.availability_policy.assumption or fields["feature_version"] != semantics.features.version or fields["layout"] != semantics.features.layout or fields["target_formula"] != semantics.target.raw_formula or fields["preprocessing"] != semantics.features.preprocessing or fields.get("opening_contract") != semantics.features.opening_contract:
		raise ValueError("Unsupported numerical/execution specification; legacy models require retraining")
	training = mapping(fields["training"])
	required_training = ("training_cutoff", "information_cutoff", "first_entry", "last_entry", "last_exit", "last_exit_close_at", "data_revision", "sample_keys", "excluded_sample_keys", "seed", "completed_epochs")
	if set(training) - {*required_training, "data_snapshot"} or any(name not in training for name in required_training):
		raise ValueError("Incomplete or unsupported training metadata fields")
	snapshot = None if training.get("data_snapshot") is None else datetime.fromisoformat(text_field(training, "data_snapshot"))
	provenance = TrainingProvenance(datetime.fromisoformat(text_field(training, "training_cutoff")), datetime.fromisoformat(text_field(training, "information_cutoff")), date.fromisoformat(text_field(training, "first_entry")), date.fromisoformat(text_field(training, "last_entry")), date.fromisoformat(text_field(training, "last_exit")), datetime.fromisoformat(text_field(training, "last_exit_close_at")), text_field(training, "data_revision"), strings(training["sample_keys"]), strings(training["excluded_sample_keys"]), integer(training["seed"]), integer(training["completed_epochs"]), snapshot)
	return ArtifactMetadata(semantics, decode_model(fields["model"]), provenance, text_field(fields, "framework_version"), text_field(fields, "weight_dtype"))


def _decode_universe(value: object) -> ArtifactUniverse:
	fields = exact_mapping(value, ("schema", "ticker_ids", "ticker_labels", "selection_mode", "evidence_revision"))
	if fields["schema"] != "artifact-universe-v1":
		raise ValueError("Unsupported artifact universe schema")
	return ArtifactUniverse(integers(fields["ticker_ids"]), strings(fields["ticker_labels"]), text_field(fields, "selection_mode"), text_field(fields, "evidence_revision"))


def _decode_v2_metadata(fields: dict[str, object]) -> ArtifactMetadata:
	required = {"artifact_metadata_version", "semantics", "model", "training", "framework_version", "weight_dtype", "feature_version", "layout", "target_formula", "preprocessing", "loss", "universe", "availability_assumption"}
	if set(fields) != required:
		raise ValueError("Incomplete or unsupported v2 artifact metadata")
	semantics = decode_semantics(fields["semantics"])
	features = semantics.features
	if fields["feature_version"] != features.version or fields["layout"] != features.layout or fields["target_formula"] != semantics.target.raw_formula or fields["availability_assumption"] != semantics.availability_policy.assumption:
		raise ValueError("Artifact v2 metadata identities disagree")
	training = mapping(fields["training"])
	required_training = ("training_cutoff", "information_cutoff", "first_entry", "last_entry", "last_exit", "last_exit_close_at", "data_revision", "sample_keys", "excluded_sample_keys", "seed", "completed_epochs")
	if set(training) - {*required_training, "data_snapshot"} or any(name not in training for name in required_training):
		raise ValueError("Incomplete or unsupported training metadata fields")
	snapshot = None if training.get("data_snapshot") is None else datetime.fromisoformat(text_field(training, "data_snapshot"))
	provenance = TrainingProvenance(datetime.fromisoformat(text_field(training, "training_cutoff")), datetime.fromisoformat(text_field(training, "information_cutoff")), date.fromisoformat(text_field(training, "first_entry")), date.fromisoformat(text_field(training, "last_entry")), date.fromisoformat(text_field(training, "last_exit")), datetime.fromisoformat(text_field(training, "last_exit_close_at")), text_field(training, "data_revision"), strings(training["sample_keys"]), strings(training["excluded_sample_keys"]), integer(training["seed"]), integer(training["completed_epochs"]), snapshot)
	loss_fields = mapping(fields["loss"])
	if set(loss_fields) != {"objective", "version", "pair_block_size"}:
		raise ValueError("Incomplete or unsupported artifact loss metadata")
	from stockguidance.training.configuration import LossObjective, LossSpec

	metadata = ArtifactMetadata(semantics, decode_model(fields["model"]), provenance, text_field(fields, "framework_version"), text_field(fields, "weight_dtype"), FittedPreprocessing.from_mapping(fields["preprocessing"]), LossSpec(LossObjective(text_field(loss_fields, "objective")), text_field(loss_fields, "version"), None if loss_fields["pair_block_size"] is None else integer(loss_fields["pair_block_size"])), _decode_universe(fields["universe"]))
	return metadata
