from dataclasses import dataclass
from enum import StrEnum

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.validation import require_enum, require_positive_int, require_text
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.identity import content_id


@dataclass(frozen=True)
class EligibilityPolicy:
	minimum_transactions: int = 200

	def __post_init__(self) -> None:
		if type(self.minimum_transactions) is not int or self.minimum_transactions < 0:
			raise ValueError("Minimum transactions must be a nonnegative integer")


@dataclass(frozen=True)
class LiquidityObservation:
	ticker_id: int
	transactions: int
	timing: ObservationTiming
	revision: str

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "liquidity ticker ID")
		if type(self.transactions) is not int or self.transactions < 0 or not isinstance(self.timing, ObservationTiming):
			raise ValueError("Liquidity requires a valid count and observation timing")
		require_text(self.revision, "liquidity revision")


class EligibilityState(StrEnum):
	ELIGIBLE = "eligible"
	INELIGIBLE = "ineligible"
	UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TickerEligibility:
	ticker_id: int
	state: EligibilityState
	detail: str
	observation: LiquidityObservation | None

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "eligibility ticker ID")
		require_enum(self.state, EligibilityState, "eligibility state")
		require_text(self.detail, "eligibility detail")
		if self.observation is not None and (not isinstance(self.observation, LiquidityObservation) or self.observation.ticker_id != self.ticker_id):
			raise ValueError("Eligibility observation and ticker differ")
		if (self.state is EligibilityState.UNAVAILABLE) != (self.observation is None):
			raise ValueError("Unavailable eligibility must remain explicit")


def assess_eligibility(ticker_ids: tuple[int, ...], policy: EligibilityPolicy, timing: DecisionTiming, observations: tuple[LiquidityObservation, ...]) -> tuple[TickerEligibility, ...]:
	if not isinstance(policy, EligibilityPolicy) or not isinstance(timing, DecisionTiming) or not isinstance(ticker_ids, tuple) or not ticker_ids or len(set(ticker_ids)) != len(ticker_ids):
		raise ValueError("Eligibility requires a policy, timing and unique ordered ticker IDs")
	if not isinstance(observations, tuple) or any(not isinstance(item, LiquidityObservation) for item in observations):
		raise ValueError("Liquidity observations must be an immutable typed tuple")
	if len({item.ticker_id for item in observations}) != len(observations) or any(item.ticker_id not in ticker_ids for item in observations):
		raise ValueError("Liquidity observations contain duplicate or unexpected ticker IDs")
	by_ticker = {item.ticker_id: item for item in observations}
	result: list[TickerEligibility] = []
	for ticker_id in ticker_ids:
		observation = by_ticker.get(ticker_id)
		if observation is None or observation.timing.session != timing.previous_session or observation.timing.available_at > timing.decision_cutoff:
			result.append(TickerEligibility(ticker_id, EligibilityState.UNAVAILABLE, "Required previous-session transaction count is unavailable by the decision cutoff", None))
		else:
			allowed = observation.transactions >= policy.minimum_transactions
			result.append(TickerEligibility(ticker_id, EligibilityState.ELIGIBLE if allowed else EligibilityState.INELIGIBLE, "Previous-session transaction count meets the minimum" if allowed else "Previous-session transaction count is below the minimum", observation))
	return tuple(result)


def eligibility_revision(assessment: tuple[TickerEligibility, ...]) -> str:
	return content_id([{"ticker_id": item.ticker_id, "state": item.state.value, "transactions": None if item.observation is None else item.observation.transactions, "revision": None if item.observation is None else item.observation.revision, "available_at": None if item.observation is None else item.observation.timing.available_at.isoformat()} for item in assessment])
