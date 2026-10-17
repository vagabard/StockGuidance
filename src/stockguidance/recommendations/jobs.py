from dataclasses import dataclass
from datetime import date, timedelta
import json
from pathlib import Path

from stockguidance.artifacts.decoding import decode_features, exact_mapping, integer
from stockguidance.config.training import decode_training_configuration, number
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_positive_int
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.identity import decode_source, mapping, text_field
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.training.configuration import TrainingConfig


@dataclass(frozen=True)
class RetrainingConfiguration:
	start: date
	end: date
	decision_lead: timedelta
	training: TrainingConfig


@dataclass(frozen=True)
class RecommendationConfiguration:
	source: SourceConfig
	features: FeatureSpec
	start: date | None
	end: date
	entry_date: date
	allocation_policy: AllocationPolicy
	anchor_date: date
	publication: DailyPublicationPolicy
	corrections: CorrectionPolicy
	eligibility: EligibilityPolicy
	top_k: int
	retraining: RetrainingConfiguration | None
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT

	def __post_init__(self) -> None:
		require_positive_int(self.top_k, "top_k")
		if self.end > self.entry_date or (self.start is not None and self.start > self.end) or self.anchor_date > self.entry_date:
			raise ValueError("Invalid recommendation coverage, entry or allocation dates")
		if self.features.adjustment is not self.source.identity.adjustment or self.features.interval is not self.source.identity.interval:
			raise ValueError("Recommendation features and source differ")
		if self.retraining is not None and (self.retraining.end >= self.entry_date or (self.start is not None and self.retraining.start < self.start)):
			raise ValueError("Retraining must lie inside coverage before the entry session")


def load_configuration(path: Path) -> RecommendationConfiguration:
	fields = mapping(json.loads(path.read_text(encoding="utf-8")))
	required = ("source", "data_root", "database_config", "features", "start_date", "end_date", "entry_date", "allocation", "publication", "corrections", "eligibility", "top_k", "retraining")
	if set(fields) - {*required, "availability_policy"} or any(name not in fields for name in required):
		raise ValueError("Incomplete or unsupported recommendation configuration fields")
	identity = decode_source(exact_mapping(fields["source"], ("provider", "endpoint", "market", "universe", "adjustment", "interval")))
	if (identity.provider, identity.endpoint, identity.market, identity.universe) != ("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE):
		raise ValueError("This CLI requires the declared Polygon grouped request; typed workflows accept injected sources")
	source = SourceConfig(identity, Path(text_field(fields, "data_root")), Path(text_field(fields, "database_config")))
	features = decode_features(fields["features"])
	allocation = exact_mapping(fields["allocation"], ("policy", "anchor_date"))
	publication = exact_mapping(fields["publication"], ("delay_minutes", "evidence"))
	corrections = exact_mapping(fields["corrections"], ("recent_window_days", "recent_refresh_hours", "archive_refresh_days", "evidence"))
	eligibility = exact_mapping(fields["eligibility"], ("minimum_transactions",))
	retraining = None
	if fields["retraining"] is not None:
		training = exact_mapping(fields["retraining"], ("start_date", "end_date", "decision_minutes_before_close", "model", "training"))
		lead = timedelta(minutes=number(training["decision_minutes_before_close"]))
		start, end = date.fromisoformat(text_field(training, "start_date")), date.fromisoformat(text_field(training, "end_date"))
		if start > end or not timedelta(0) < lead < timedelta(days=1):
			raise ValueError("Invalid retraining bounds or decision lead")
		retraining = RetrainingConfiguration(start, end, lead, decode_training_configuration(training["training"], training["model"]))
	return RecommendationConfiguration(source, features, None if fields["start_date"] is None else date.fromisoformat(text_field(fields, "start_date")), date.fromisoformat(text_field(fields, "end_date")), date.fromisoformat(text_field(fields, "entry_date")), AllocationPolicy(text_field(allocation, "policy")), date.fromisoformat(text_field(allocation, "anchor_date")), DailyPublicationPolicy(timedelta(minutes=number(publication["delay_minutes"])), text_field(publication, "evidence")), CorrectionPolicy(timedelta(days=number(corrections["recent_window_days"])), timedelta(hours=number(corrections["recent_refresh_hours"])), timedelta(days=number(corrections["archive_refresh_days"])), text_field(corrections, "evidence")), EligibilityPolicy(integer(eligibility["minimum_transactions"])), integer(fields["top_k"]), retraining, AvailabilityPolicy(str(fields.get("availability_policy", AvailabilityPolicy.STRICT.value))))
