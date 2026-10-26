from dataclasses import dataclass

from stockguidance.datasets.sample import Sample
from stockguidance.portfolio.planning import OrderPlan
from stockguidance.portfolio.state import PortfolioState
from stockguidance.recommendations.eligibility import TickerEligibility
from stockguidance.recommendations.result import RankedTicker, RecommendationResult
from stockguidance.training.result import TrainingResult
from stockguidance.validation.result import ValidationResult
from stockguidance.workflows.update_result import UpdateResult
from stockguidance.database.simplified_runtime import SimplifiedUpdateResult, SimplifiedValidationResult


@dataclass(frozen=True)
class RecommendationRunResult:
	recommendation: RecommendationResult
	scores: tuple[RankedTicker, ...]
	eligibility: tuple[TickerEligibility, ...]
	sample: Sample
	refresh: UpdateResult | SimplifiedUpdateResult
	validation: ValidationResult | SimplifiedValidationResult
	member_artifact_ids: tuple[str, ...]
	member_weights: tuple[float, ...]
	training: TrainingResult | None
	order_plan: OrderPlan | None = None
	portfolio: PortfolioState | None = None
