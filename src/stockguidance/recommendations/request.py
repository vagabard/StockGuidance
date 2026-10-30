from dataclasses import dataclass

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import SourceConfig
from stockguidance.features.specification import FeatureSpec, FeatureSpecV2
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.models.policy import ModelSelection
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.config.validation import require_positive_int
from stockguidance.recommendations.configuration import RefreshMode
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.targets.specification import TargetSpec


@dataclass(frozen=True)
class RecommendationRequest:
	source: SourceConfig
	coverage: MarketDataRequest
	timing: DecisionTiming
	features: FeatureSpec | FeatureSpecV2
	target: TargetSpec
	model: ModelSelection
	allocation: AllocationSpec
	eligibility: EligibilityPolicy = EligibilityPolicy()
	top_k: int = 5
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT
	refresh_mode: RefreshMode = RefreshMode.REFRESH
	allow_empty_selection: bool | None = None

	def __post_init__(self) -> None:
		for value, expected in (
			(self.source, SourceConfig), (self.coverage, MarketDataRequest),
			(self.timing, DecisionTiming), (self.features, (FeatureSpec, FeatureSpecV2)),
			(self.target, TargetSpec), (self.model, ModelSelection),
			(self.allocation, AllocationSpec),
			(self.eligibility, EligibilityPolicy),
		):
			if not isinstance(value, expected):
				raise ValueError("Recommendation fields must use their typed contracts")
		require_positive_int(self.top_k, "top_k")
		if not isinstance(self.availability_policy, AvailabilityPolicy):
			raise ValueError("Recommendation requires a typed availability policy")
		if self.availability_policy is not AvailabilityPolicy.STRICT:
			raise ValueError("Retrospective research artifacts are not supported for production recommendations")
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
		if not isinstance(self.refresh_mode, RefreshMode):
			raise ValueError("Recommendation requires a typed refresh mode")
		if self.allow_empty_selection is not None and type(self.allow_empty_selection) is not bool:
			raise ValueError("allow_empty_selection must be boolean or null")

	@property
	def permits_empty_selection(self) -> bool:
		if self.target.version == "raw_return-v1":
			return False
		return self.allow_empty_selection is not False
