from stockguidance.config.validation import require_positive_int
from stockguidance.recommendations.eligibility import EligibilityState, TickerEligibility
from stockguidance.recommendations.result import RankedTicker


def rank_scores(scores: tuple[RankedTicker, ...], eligibility: tuple[TickerEligibility, ...], top_k: int) -> tuple[RankedTicker, ...]:
	require_positive_int(top_k, "top_k")
	if not isinstance(scores, tuple) or not scores or any(not isinstance(item, RankedTicker) for item in scores) or not isinstance(eligibility, tuple) or any(not isinstance(item, TickerEligibility) for item in eligibility):
		raise ValueError("Ranking requires explicit ticker scores and eligibility")
	if len({item.ticker_id for item in scores}) != len(scores) or tuple(item.ticker_id for item in scores) != tuple(item.ticker_id for item in eligibility):
		raise ValueError("Scores and eligibility must use the same unique ordered ticker IDs")
	if any(item.state is EligibilityState.UNAVAILABLE for item in eligibility):
		raise ValueError("Required eligibility observations are unavailable; ranking cannot bypass the rule")
	allowed = {item.ticker_id for item in eligibility if item.state is EligibilityState.ELIGIBLE}
	return tuple(sorted((item for item in scores if item.ticker_id in allowed), key=lambda item: (-item.score, item.ticker_id))[:top_k])
