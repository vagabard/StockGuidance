from dataclasses import dataclass, field
from enum import StrEnum
import json
from pathlib import Path

from stockguidance.config.validation import require_enum, require_text
from stockguidance.market_data.intervals import ObservationInterval, require_daily_interval


class PriceAdjustment(StrEnum):
	ADJUSTED = "adjusted"
	UNADJUSTED = "unadjusted"


class VerificationMode(StrEnum):
	BLOCK = "block"
	GATE_WRITES = "gate_writes"
	ADVISORY = "advisory"


@dataclass(frozen=True)
class VerificationPolicy:
	source_family_mismatch: VerificationMode = VerificationMode.BLOCK
	endpoint_mismatch: VerificationMode = VerificationMode.ADVISORY
	source_family_sample_days: int = 100
	endpoint_sample_pulls: int = 50

	def __post_init__(self) -> None:
		for name in ("source_family_mismatch", "endpoint_mismatch"):
			if not isinstance(getattr(self, name), VerificationMode):
				raise ValueError(f"{name} must be a VerificationMode")
		for name in ("source_family_sample_days", "endpoint_sample_pulls"):
			value = getattr(self, name)
			if type(value) is not int or value <= 0:
				raise ValueError(f"{name} must be a positive integer")


def canonical_source_family(value: str) -> str:
	if not isinstance(value, str) or not value.strip():
		raise ValueError("source_family must be nonempty text")
	aliases = {"polygon": "polygon_massive", "massive": "polygon_massive"}
	return aliases.get(value.strip().casefold(), value.strip())


@dataclass(frozen=True)
class DataConfiguration:
	"""Stable data semantics plus the current provider request details."""

	source_family: str
	endpoint: str
	market: str
	universe: str
	raw_price_split_adjusted: bool
	raw_price_dividend_adjusted: bool
	raw_price_vendor_defined_adjustment: str | None = None
	interval: ObservationInterval = ObservationInterval.DAILY
	verification_policy: VerificationPolicy = field(default_factory=VerificationPolicy, repr=False)

	def __post_init__(self) -> None:
		for name in ("source_family", "endpoint", "market", "universe"):
			value = getattr(self, name)
			if not isinstance(value, str) or not value.strip():
				raise ValueError(f"{name} must be nonempty text")
			object.__setattr__(self, name, value.strip())
		object.__setattr__(self, "source_family", canonical_source_family(self.source_family))
		for name in ("raw_price_split_adjusted", "raw_price_dividend_adjusted"):
			if type(getattr(self, name)) is not bool:
				raise ValueError(f"{name} must be a boolean")
		if self.raw_price_vendor_defined_adjustment is not None and (not isinstance(self.raw_price_vendor_defined_adjustment, str) or not self.raw_price_vendor_defined_adjustment.strip()):
			raise ValueError("raw_price_vendor_defined_adjustment must be nonempty text or null")
		if not isinstance(self.interval, ObservationInterval):
			raise ValueError("interval must be an ObservationInterval")
		require_daily_interval(self.interval)
		if not isinstance(self.verification_policy, VerificationPolicy):
			raise ValueError("verification_policy must be a VerificationPolicy")

	@property
	def adjustment(self) -> PriceAdjustment:
		return PriceAdjustment.ADJUSTED if self.raw_price_split_adjusted or self.raw_price_dividend_adjusted or self.raw_price_vendor_defined_adjustment is not None else PriceAdjustment.UNADJUSTED

	def to_source_identity(self) -> "SourceIdentity":
		provider = "polygon" if self.source_family == "polygon_massive" else self.source_family
		return SourceIdentity(provider, self.endpoint, self.market, self.universe, self.adjustment, self.interval)

	def semantic_payload(self) -> dict[str, object]:
		return {
			"source_family": self.source_family,
			"market": self.market,
			"interval": self.interval.value,
			"raw_price_split_adjusted": self.raw_price_split_adjusted,
			"raw_price_dividend_adjusted": self.raw_price_dividend_adjusted,
			"raw_price_vendor_defined_adjustment": self.raw_price_vendor_defined_adjustment,
		}


def _mapping(value: object, name: str) -> dict[str, object]:
	if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
		raise ValueError(f"{name} must be a JSON object")
	return dict(value)


def _text(value: dict[str, object], name: str) -> str:
	result = value.get(name)
	if not isinstance(result, str) or not result.strip():
		raise ValueError(f"{name} must be nonempty text")
	return result


def load_data_configuration(path: Path) -> DataConfiguration:
	try:
		payload = _mapping(json.loads(path.read_text(encoding="utf-8")), "data configuration")
		if set(payload) != {"schema_version", "source_family", "endpoint", "market", "universe", "interval", "raw_price_split_adjusted", "raw_price_dividend_adjusted", "raw_price_vendor_defined_adjustment", "verification_policy"}:
			raise ValueError("Data configuration fields are incomplete or unknown")
		if payload["schema_version"] != 1:
			raise ValueError("Unsupported data configuration schema version")
		policy_payload = _mapping(payload["verification_policy"], "verification_policy")
		if set(policy_payload) != {"source_family_mismatch", "endpoint_mismatch", "source_family_sample_days", "endpoint_sample_pulls"}:
			raise ValueError("Verification policy fields are incomplete or unknown")
		return DataConfiguration(
			_text(payload, "source_family"), _text(payload, "endpoint"), _text(payload, "market"), _text(payload, "universe"),
			payload["raw_price_split_adjusted"], payload["raw_price_dividend_adjusted"],
			None if payload["raw_price_vendor_defined_adjustment"] is None else _text(payload, "raw_price_vendor_defined_adjustment"),
			ObservationInterval(_text(payload, "interval")),
			VerificationPolicy(
				VerificationMode(policy_payload["source_family_mismatch"]), VerificationMode(policy_payload["endpoint_mismatch"]),
				policy_payload["source_family_sample_days"], policy_payload["endpoint_sample_pulls"],
			),
		)
	except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
		raise ValueError("Cannot read the explicit data configuration JSON") from None


@dataclass(frozen=True)
class SourceIdentity:
	"""Non-secret provider request scope, including the entire requested universe."""

	provider: str
	endpoint: str
	market: str
	universe: str
	adjustment: PriceAdjustment
	interval: ObservationInterval = ObservationInterval.DAILY

	def __post_init__(self) -> None:
		for name in ("provider", "endpoint", "market", "universe"):
			require_text(getattr(self, name), name)
		require_enum(self.adjustment, PriceAdjustment, "adjustment")
		require_daily_interval(self.interval)

	def to_data_configuration(self) -> DataConfiguration:
		return DataConfiguration(
			self.provider, self.endpoint, self.market, self.universe,
			self.adjustment is PriceAdjustment.ADJUSTED, False, None, self.interval,
		)


@dataclass(frozen=True)
class SourceConfig:
	identity: SourceIdentity | DataConfiguration
	data_root: Path
	database_config: Path
	credential_reference: str | None = field(default=None, repr=False)
	_configuration: DataConfiguration | None = field(default=None, repr=False, compare=False)

	def __post_init__(self) -> None:
		if isinstance(self.identity, DataConfiguration):
			configuration = self.identity
			object.__setattr__(self, "identity", configuration.to_source_identity())
			object.__setattr__(self, "_configuration", configuration)
		elif not isinstance(self.identity, SourceIdentity):
			raise ValueError("identity must be a SourceIdentity or DataConfiguration")
		elif self._configuration is None:
			object.__setattr__(self, "_configuration", self.identity.to_data_configuration())
		elif not isinstance(self._configuration, DataConfiguration):
			raise ValueError("_configuration must be a DataConfiguration")
		if not isinstance(self.data_root, Path) or not isinstance(self.database_config, Path):
			raise ValueError("data_root and database_config must be Paths")
		if self.credential_reference is not None:
			require_text(self.credential_reference, "credential_reference")

	@property
	def data_configuration(self) -> DataConfiguration:
		assert self._configuration is not None
		return self._configuration if self._configuration.to_source_identity() == self.identity else self.identity.to_data_configuration()
