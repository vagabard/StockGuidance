from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date, require_enum, require_positive_int
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.portfolio.precision import ExecutionPolicy, require_money
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.targets.specification import TargetSpec


class EvaluationPolicy(StrEnum):
	FIXED = "fixed"
	TRAIN_THEN_SIMULATE = "train_then_simulate"
	CHRONOLOGICAL_RETRAIN = "chronological_retrain"


@dataclass(frozen=True)
class SimulationRequest:
	source: SourceIdentity
	features: FeatureSpec
	target: TargetSpec
	allocation: AllocationSpec
	start: date
	end: date
	initial_capital: Decimal
	decision_lead: timedelta = timedelta(minutes=30)
	eligibility: EligibilityPolicy = EligibilityPolicy()
	top_k: int = 1
	execution: ExecutionPolicy = ExecutionPolicy()
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT
	data_snapshot: datetime | None = None

	def __post_init__(self) -> None:
		for value, expected in ((self.source, SourceIdentity), (self.features, FeatureSpec), (self.target, TargetSpec), (self.allocation, AllocationSpec), (self.eligibility, EligibilityPolicy), (self.execution, ExecutionPolicy)):
			if not isinstance(value, expected):
				raise ValueError("Simulation requires typed source and strategy specifications")
		require_date(self.start, "simulation start")
		require_date(self.end, "simulation end")
		require_money(self.initial_capital, "initial capital")
		require_positive_int(self.top_k, "top_k")
		if not isinstance(self.availability_policy, AvailabilityPolicy):
			raise ValueError("Simulation requires a typed availability policy")
		self.availability_policy.validate_features(self.features)
		if self.data_snapshot is not None:
			require_aware_datetime(self.data_snapshot, "data snapshot")
		if self.availability_policy.retrospective and self.data_snapshot is None:
			raise ValueError("Retrospective simulation requires an explicit data snapshot")
		if self.start > self.end or self.start < self.allocation.anchor_session.session_date or self.initial_capital <= 0:
			raise ValueError("Invalid evaluation period, anchor or capital")
		if self.source.adjustment is not self.features.adjustment or self.source.interval is not self.features.interval:
			raise ValueError("Simulation source and features differ")
		if not isinstance(self.decision_lead, timedelta) or not timedelta(0) < self.decision_lead < timedelta(days=1):
			raise ValueError("Simulation decisions require a positive pre-close lead")


@dataclass(frozen=True)
class ScorePolicy:
	policy: EvaluationPolicy
	retrain_every_buy_decisions: int | None = None

	def __post_init__(self) -> None:
		require_enum(self.policy, EvaluationPolicy, "evaluation policy")
		if self.policy is EvaluationPolicy.FIXED:
			if self.retrain_every_buy_decisions is not None:
				raise ValueError("Fixed evaluation cannot request retraining")
		elif self.policy is EvaluationPolicy.TRAIN_THEN_SIMULATE:
			if self.retrain_every_buy_decisions is not None:
				raise ValueError("Train-then-simulate uses one pre-period fit")
		else:
			require_positive_int(self.retrain_every_buy_decisions, "retraining cadence")
