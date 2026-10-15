from dataclasses import dataclass
import math

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.sessions import TradingSession
from stockguidance.database.repositories.closes import CloseData
from stockguidance.features.specification import CloseFeature, FeatureSpec
from stockguidance.market_data.closes import CloseIssue, PriceState


@dataclass(frozen=True)
class FeatureValues:
	specification: FeatureSpec
	sessions: tuple[TradingSession, ...]
	values: tuple[float, ...] | None
	issues: tuple[CloseIssue, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.specification, FeatureSpec) or not isinstance(self.sessions, tuple) or not isinstance(self.issues, tuple):
			raise ValueError("Invalid feature window contracts")
		if any(not isinstance(session, TradingSession) for session in self.sessions) or any(not isinstance(issue, CloseIssue) for issue in self.issues):
			raise ValueError("Invalid feature window sessions or issues")
		if self.values is not None:
			if self.issues or not isinstance(self.values, tuple) or len(self.values) != self.specification.input_size or len(self.sessions) != self.specification.lookback_sessions or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in self.values):
				raise ValueError("Invalid numerical feature layout")
			if any(right.sequence != left.sequence + 1 or right.calendar_id != left.calendar_id for left, right in zip(self.sessions, self.sessions[1:])):
				raise ValueError("Feature sessions must be exactly adjacent")


def construct_features(specification: FeatureSpec, timing: DecisionTiming, sessions: tuple[TradingSession, ...], data: CloseData) -> FeatureValues:
	if len(sessions) != specification.lookback_sessions or tuple(session.sequence for session in sessions) != tuple(range(timing.entry_session.sequence - specification.lookback_sessions, timing.entry_session.sequence)):
		raise ValueError("Feature history must be the exact prior-session window")
	by_key = {(item.timing.session, item.ticker_id): item for item in data.observations}
	issues = [issue for issue in data.issues if issue.session in sessions]
	for session in sessions:
		for ticker_id in specification.ticker_ids:
			observation = by_key.get((session, ticker_id))
			if observation is None:
				if not any(issue.session == session and issue.ticker_id == ticker_id for issue in issues):
					raise ValueError("Feature result omits a required session/ticker key")
				continue
			if observation.timing.available_at > timing.decision_cutoff:
				issues.append(CloseIssue(ticker_id, session, PriceState.NOT_YET_AVAILABLE, "Historical close was published after the sample decision"))
			else:
				specification.validate_observation(observation.timing, timing)
	if issues:
		return FeatureValues(specification, sessions, None, tuple(issues))
	values: list[float] = []
	for session in sessions:
		for ticker_id in specification.ticker_ids:
			close = by_key[session, ticker_id].close
			anchor = by_key[sessions[0], ticker_id].close
			for feature in specification.feature_order:
				values.append(close / anchor - 1 if feature is CloseFeature.RELATIVE_CLOSE else math.log(close))
	if any(not math.isfinite(value) for value in values):
		return FeatureValues(specification, sessions, None, (CloseIssue(specification.ticker_ids[0], sessions[0], PriceState.INVALID, "Close transformation is not finite"),))
	return FeatureValues(specification, sessions, tuple(values))
