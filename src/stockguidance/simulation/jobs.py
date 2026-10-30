from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

from stockguidance.artifacts.decoding import decode_features, exact_mapping, integer
from stockguidance.calendar.clock import SystemClock
from stockguidance.config.training import decode_training_configuration, number
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_aware_datetime, require_positive_int
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.identity import decode_source, mapping, text_field
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.portfolio.precision import ExecutionPolicy, require_money
from stockguidance.portfolio.serialization import decimal_text
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.training.configuration import TrainingConfig


@dataclass(frozen=True)
class WalkForwardConfiguration:
	start: date
	end: date
	decision_lead: timedelta
	configuration: TrainingConfig
	every_buy_decisions: int


@dataclass(frozen=True)
class SimulationConfiguration:
	source: SourceConfig
	features: FeatureSpec
	start: date
	end: date
	allocation_policy: AllocationPolicy
	anchor_date: date
	initial_capital: Decimal
	decision_lead: timedelta
	publication: DailyPublicationPolicy
	audit_cutoff: datetime
	eligibility: EligibilityPolicy
	top_k: int
	execution: ExecutionPolicy
	retraining: WalkForwardConfiguration | None
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT
	data_snapshot: datetime | None = None

	def __post_init__(self) -> None:
		require_money(self.initial_capital, "initial capital")
		require_positive_int(self.top_k, "top_k")
		require_aware_datetime(self.audit_cutoff, "execution audit cutoff")
		if not isinstance(self.availability_policy, AvailabilityPolicy):
			raise ValueError("Simulation requires a typed availability policy")
		self.availability_policy.validate_features(self.features)
		if self.data_snapshot is not None:
			require_aware_datetime(self.data_snapshot, "data snapshot")
		if self.availability_policy.retrospective and self.data_snapshot is None:
			raise ValueError("Retrospective simulation requires an explicit data snapshot")
		if self.start > self.end or self.anchor_date > self.start or self.initial_capital <= 0 or not timedelta(0) < self.decision_lead < timedelta(days=1):
			raise ValueError("Invalid simulation bounds, capital, anchor or decision lead")
		if self.features.adjustment is not self.source.identity.adjustment or self.features.interval is not self.source.identity.interval:
			raise ValueError("Simulation features and source differ")
		if self.retraining is not None and (self.retraining.start > self.retraining.end or self.retraining.start >= self.start or self.retraining.end > self.end or not timedelta(0) < self.retraining.decision_lead < timedelta(days=1)):
			raise ValueError("Walk-forward history must begin before evaluation and end within its bounds")


def load_configuration(path: Path) -> SimulationConfiguration:
	fields = mapping(json.loads(path.read_text(encoding="utf-8")))
	required = ("source", "data_root", "database_config", "features", "start_date", "end_date", "allocation", "initial_capital", "decision_minutes_before_close", "publication", "execution_audit_cutoff", "eligibility", "top_k", "fee_per_fill", "retraining")
	if set(fields) - {*required, "availability_policy", "data_snapshot"} or any(name not in fields for name in required):
		raise ValueError("Incomplete or unsupported simulation configuration fields")
	identity = decode_source(exact_mapping(fields["source"], ("provider", "endpoint", "market", "universe", "adjustment", "interval")))
	source = SourceConfig(identity, Path(text_field(fields, "data_root")), Path(text_field(fields, "database_config")))
	allocation = exact_mapping(fields["allocation"], ("policy", "anchor_date"))
	publication = exact_mapping(fields["publication"], ("delay_minutes", "evidence"))
	eligibility = exact_mapping(fields["eligibility"], ("minimum_transactions",))
	cutoff = datetime.fromisoformat(text_field(fields, "execution_audit_cutoff"))
	if cutoff > SystemClock().now():
		raise ValueError("Execution audit cutoff cannot be in the future")
	retraining = None
	if fields["retraining"] is not None:
		training = exact_mapping(fields["retraining"], ("start_date", "end_date", "decision_minutes_before_close", "every_buy_decisions", "model", "training"))
		cadence = integer(training["every_buy_decisions"])
		require_positive_int(cadence, "retraining cadence")
		retraining = WalkForwardConfiguration(date.fromisoformat(text_field(training, "start_date")), date.fromisoformat(text_field(training, "end_date")), timedelta(minutes=number(training["decision_minutes_before_close"])), decode_training_configuration(training["training"], training["model"]), cadence)
	availability_policy = AvailabilityPolicy(str(fields.get("availability_policy", AvailabilityPolicy.STRICT.value)))
	data_snapshot = None if fields.get("data_snapshot") is None else datetime.fromisoformat(text_field(fields, "data_snapshot"))
	if data_snapshot is not None:
		require_aware_datetime(data_snapshot, "data snapshot")
	if availability_policy.retrospective and data_snapshot is None:
		raise ValueError("Retrospective simulation requires data_snapshot")
	if data_snapshot is not None and data_snapshot > SystemClock().now():
		raise ValueError("Data snapshot cannot be in the future")
	features = decode_features(fields["features"])
	if not isinstance(features, FeatureSpec):
		raise ValueError("Simulation configuration requires legacy explicit feature semantics")
	return SimulationConfiguration(source, features, date.fromisoformat(text_field(fields, "start_date")), date.fromisoformat(text_field(fields, "end_date")), AllocationPolicy(text_field(allocation, "policy")), date.fromisoformat(text_field(allocation, "anchor_date")), decimal_text(fields["initial_capital"]), timedelta(minutes=number(fields["decision_minutes_before_close"])), DailyPublicationPolicy(timedelta(minutes=number(publication["delay_minutes"])), text_field(publication, "evidence")), cutoff, EligibilityPolicy(integer(eligibility["minimum_transactions"])), integer(fields["top_k"]), ExecutionPolicy(decimal_text(fields["fee_per_fill"])), retraining, availability_policy, data_snapshot)
