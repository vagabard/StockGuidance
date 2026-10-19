from dataclasses import asdict
from hashlib import sha256
import json

from stockguidance.config.source import DataConfiguration, PriceAdjustment, SourceIdentity
from stockguidance.market_data.intervals import ObservationInterval


def content_id(value: object) -> str:
	return sha256(encode_json(value)).hexdigest()


def encode_json(value: object) -> bytes:
	return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def source_key(source: SourceIdentity) -> str:
	return content_id(asdict(source))


def data_configuration_key(configuration: DataConfiguration | SourceIdentity) -> str:
	if isinstance(configuration, SourceIdentity):
		configuration = configuration.to_data_configuration()
	return content_id(configuration.semantic_payload())


def mapping(value: object) -> dict[str, object]:
	if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
		raise ValueError("Expected a JSON object")
	return dict(value)


def text_field(value: dict[str, object], name: str) -> str:
	result = value[name]
	if not isinstance(result, str):
		raise ValueError(f"Expected text for {name}")
	return result


def decode_source(value: object) -> SourceIdentity:
	scope = mapping(value)
	return SourceIdentity(text_field(scope, "provider"), text_field(scope, "endpoint"), text_field(scope, "market"), text_field(scope, "universe"), PriceAdjustment(text_field(scope, "adjustment")), ObservationInterval(text_field(scope, "interval")))
