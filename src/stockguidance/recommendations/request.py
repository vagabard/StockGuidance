from dataclasses import dataclass

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import SourceConfig
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.models.policy import ModelSelection
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.config.validation import require_positive_int
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.targets.specification import TargetSpec


@dataclass(frozen=True)
class RecommendationRequest:
	source: SourceConfig
	coverage: MarketDataRequest
	timing: DecisionTiming
	features: FeatureSpec
	target: TargetSpec
	model: ModelSelection
	allocation: AllocationSpec
	eligibility: EligibilityPolicy = EligibilityPolicy()
	top_k: int = 5

	def __post_init__(self) -> None:
		for value, expected in (
			(self.source, SourceConfig), (self.coverage, MarketDataRequest),
			(self.timing, DecisionTiming), (self.features, FeatureSpec),
			(self.target, TargetSpec), (self.model, ModelSelection),
			(self.allocation, AllocationSpec),
			(self.eligibility, EligibilityPolicy),
		):
			if not isinstance(value, expected):
				raise ValueError("Recommendation fields must use their typed contracts")
		require_positive_int(self.top_k, "top_k")
		if self.source.identity != self.coverage.source:
			raise ValueError("Coverage and configured source identities differ")
		if self.features.interval is not self.source.identity.interval:
			raise ValueError("Feature and source intervals differ")
		if self.features.adjustment is not self.source.identity.adjustment:
			raise ValueError("Feature and source adjustments differ")
		if self.coverage.as_of != self.timing.decision_cutoff:
			raise ValueError("Coverage availability must use the decision cutoff")
		anchor = self.allocation.anchor_session
		entry = self.timing.entry_session
		if anchor.calendar_id != entry.calendar_id:
			raise ValueError("Allocation and decision calendars differ")
		if anchor.sequence > entry.sequence or anchor.session_date > entry.session_date:
			raise ValueError("Decision cannot precede its allocation anchor")
