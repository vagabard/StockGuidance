from dataclasses import dataclass
from math import isfinite

from stockguidance.config.validation import require_positive_int, require_text
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.recommendations.request import RecommendationRequest


@dataclass(frozen=True)
class RankedTicker:
	ticker_id: int
	score: float

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "ticker_id")
		if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not isfinite(self.score):
			raise ValueError("score must be a finite number")


@dataclass(frozen=True)
class RecommendationResult:
	"""A ranked suggestion with provenance; no cash ledger, orders, or fills.

	The recommendation workflow establishes readiness before returning this.
	A ranked list alone does not enforce the cash-rest allocation policy.
	"""

	request: RecommendationRequest
	artifact_id: str
	data_revision: str
	ranking: tuple[RankedTicker, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.request, RecommendationRequest):
			raise ValueError("request must be a RecommendationRequest")
		require_text(self.artifact_id, "artifact_id")
		require_text(self.data_revision, "data_revision")
		if self.request.model.policy is ModelRefreshPolicy.CURRENT and self.artifact_id != self.request.model.artifact_id:
			raise ValueError("current recommendations must identify the requested artifact")
		if not isinstance(self.ranking, tuple) or not self.ranking:
			raise ValueError("ranking must be a nonempty immutable tuple")
		seen: set[int] = set()
		previous_score = float("inf")
		for ranked in self.ranking:
			if not isinstance(ranked, RankedTicker):
				raise ValueError("ranking must contain RankedTicker values")
			if ranked.ticker_id not in self.request.features.ticker_ids or ranked.ticker_id in seen:
				raise ValueError("Ranked tickers must be unique members of the model's ordered universe")
			if ranked.score > previous_score:
				raise ValueError("ranking must be ordered by descending score")
			previous_score = ranked.score
			seen.add(ranked.ticker_id)
