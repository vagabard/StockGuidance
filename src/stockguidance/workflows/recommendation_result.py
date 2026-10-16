from dataclasses import dataclass

from stockguidance.datasets.sample import Sample
from stockguidance.recommendations.eligibility import TickerEligibility
from stockguidance.recommendations.result import RankedTicker, RecommendationResult
from stockguidance.training.result import TrainingResult
from stockguidance.validation.result import ValidationResult
from stockguidance.workflows.update_result import UpdateResult


@dataclass(frozen=True)
class RecommendationRunResult:
	recommendation: RecommendationResult
	scores: tuple[RankedTicker, ...]
	eligibility: tuple[TickerEligibility, ...]
	sample: Sample
	refresh: UpdateResult
	validation: ValidationResult
	member_artifact_ids: tuple[str, ...]
	member_weights: tuple[float, ...]
	training: TrainingResult | None
