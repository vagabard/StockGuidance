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
class ValidationOnlyRefresh:
	data_revision: str
	successful: bool
	downloaded_sessions: int = 0
	mode: str = "validate-only"

	def __post_init__(self) -> None:
		if not isinstance(self.data_revision, str) or not self.data_revision or type(self.successful) is not bool:
			raise ValueError("Validation-only refresh evidence is invalid")


@dataclass(frozen=True)
class RecommendationRunResult:
	recommendation: RecommendationResult
	scores: tuple[RankedTicker, ...]
	eligibility: tuple[TickerEligibility, ...]
	sample: Sample
	refresh: UpdateResult | SimplifiedUpdateResult | ValidationOnlyRefresh
	validation: ValidationResult | SimplifiedValidationResult
	member_artifact_ids: tuple[str, ...]
	member_weights: tuple[float, ...]
	training: TrainingResult | None
	order_plan: OrderPlan | None = None
	portfolio: PortfolioState | None = None
