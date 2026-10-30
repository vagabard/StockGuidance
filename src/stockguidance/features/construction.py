from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import cast

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.sessions import TradingSession
from stockguidance.database.repositories.closes import CloseData
from stockguidance.features.specification import CloseFeature, FeatureSpec
from stockguidance.features.channels import FeatureChannelData, FeatureReadinessState, FeatureValueState
from stockguidance.features.specification import FeatureChannel, FeatureChannelValue, FeatureSpecV2, FeatureSpecification, PriceZScore, PriceDeltaZScore, VolumeZScore, channel_key
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.closes import CloseIssue, PriceState
from stockguidance.market_data.openings import OpeningData
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.quality import QualityFlag, Qualified, combine_quality


@dataclass(frozen=True)
class FeatureValues:
	specification: FeatureSpecification
	sessions: tuple[TradingSession, ...]
	values: tuple[float, ...] | None
	issues: tuple[CloseIssue | "FeatureIssue", ...] = ()
	quality: QualityFlag = QualityFlag.CLEAN
	gap_length: int = 0
	provenance: tuple[str, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.specification, (FeatureSpec, FeatureSpecV2)) or not isinstance(self.sessions, tuple) or not isinstance(self.issues, tuple):
			raise ValueError("Invalid feature window contracts")
		if any(not isinstance(session, TradingSession) for session in self.sessions) or any(not isinstance(issue, (CloseIssue, FeatureIssue)) for issue in self.issues):
			raise ValueError("Invalid feature window sessions or issues")
		if self.values is not None:
			if self.issues or not isinstance(self.values, tuple) or len(self.values) != self.specification.input_size or len(self.sessions) != self.specification.lookback_sessions or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in self.values):
				raise ValueError("Invalid numerical feature layout")
			if any(right.sequence != left.sequence + 1 or right.calendar_id != left.calendar_id for left, right in zip(self.sessions, self.sessions[1:])):
				raise ValueError("Feature sessions must be exactly adjacent")


class FeatureIssueState(StrEnum):
	MISSING = FeatureValueState.MISSING.value
	INSUFFICIENT_HISTORY = FeatureValueState.INSUFFICIENT_HISTORY.value
	UNVERIFIED = FeatureValueState.UNVERIFIED.value
	NOT_YET_AVAILABLE = FeatureValueState.NOT_YET_AVAILABLE.value
	INVALID = FeatureValueState.INVALID.value
	ZERO_VARIANCE = FeatureValueState.ZERO_VARIANCE.value
	STALE = FeatureValueState.STALE.value
	PENDING = FeatureValueState.PENDING.value
	INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class FeatureIssue:
	channel: str
	ticker_id: int
	session: TradingSession
	state: FeatureIssueState
	detail: str
	quality: QualityFlag = QualityFlag.CLEAN
	gap_length: int = 0
	provenance: tuple[str, ...] = ()

	def __post_init__(self) -> None:
		if not self.channel or not isinstance(self.session, TradingSession) or type(self.ticker_id) is not int or self.ticker_id < 1 or not isinstance(self.state, FeatureIssueState) or not self.detail:
			raise ValueError("Invalid configured feature issue")
		if type(self.gap_length) is not int or self.gap_length < 0:
			raise ValueError("Feature issue gap_length must be nonnegative")


def construct_features(specification: FeatureSpec, timing: DecisionTiming, sessions: tuple[TradingSession, ...], data: CloseData, openings: OpeningData | None = None, availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT, publication: DailyPublicationPolicy | None = None) -> FeatureValues:
	availability_policy.validate_features(specification)
	if len(sessions) != specification.lookback_sessions or tuple(session.sequence for session in sessions) != tuple(range(timing.entry_session.sequence - specification.lookback_sessions, timing.entry_session.sequence)):
		raise ValueError("Feature history must be the exact prior-session window")
	by_key = {(item.timing.session, item.ticker_id): item for item in data.observations}
	issues = [issue for issue in data.issues if issue.session in sessions]
	quality, gap_length, provenance = combine_quality([*(item for item in data.observations if item.timing.session in sessions and item.ticker_id in specification.ticker_ids), *issues])
	if availability_policy.retrospective and any(item.timing.session in sessions and item.ticker_id in specification.ticker_ids and item.timing.available_at > timing.decision_cutoff for item in data.observations):
		quality |= QualityFlag.FUTURE_LEAKAGE
	opening_prices = {} if openings is None else {item.ticker_id: item.price for item in openings.observations}
	if CloseFeature.ENTRY_OPEN_RETURN in specification.feature_order:
		if openings is None or openings.query.sessions != (timing.entry_session,) or openings.query.ticker_ids != specification.ticker_ids or openings.query.as_of != timing.decision_cutoff:
			raise ValueError("Opening features require the exact entry session, cutoff and full ticker universe")
		issues.extend(openings.issues)
	for session in sessions:
		for ticker_id in specification.ticker_ids:
			observation = by_key.get((session, ticker_id))
			if observation is None:
				if not any(issue.session == session and issue.ticker_id == ticker_id for issue in issues):
					raise ValueError("Feature result omits a required session/ticker key")
				continue
			if not availability_policy.observation_available_at(observation.timing, timing.decision_cutoff, publication):
				issues.append(CloseIssue(ticker_id, session, PriceState.NOT_YET_AVAILABLE, "Historical close was published after the sample decision"))
			else:
				specification.validate_observation(observation.timing, timing, availability_policy=availability_policy, publication=publication)
	if issues:
		return FeatureValues(specification, sessions, None, tuple(issues), quality, gap_length, provenance)
	values: list[float] = []
	for session in sessions:
		for ticker_id in specification.ticker_ids:
			close = by_key[session, ticker_id].close
			anchor = by_key[sessions[0], ticker_id].close
			for feature in specification.feature_order:
				if feature is CloseFeature.ENTRY_OPEN_RETURN:
					values.append(opening_prices[ticker_id] / by_key[sessions[-1], ticker_id].close - 1)
				else:
					values.append(close / anchor - 1 if feature is CloseFeature.RELATIVE_CLOSE else math.log(close))
	if any(not math.isfinite(value) for value in values):
		return FeatureValues(specification, sessions, None, (CloseIssue(specification.ticker_ids[0], sessions[0], PriceState.INVALID, "Close transformation is not finite"),), quality, gap_length, provenance)
	return FeatureValues(specification, sessions, tuple(values), quality=quality, gap_length=gap_length, provenance=provenance)


def _configured_issue(observation: object, channel: FeatureChannelValue, session: TradingSession, ticker_id: int) -> FeatureIssue:
	state = getattr(observation, "state", FeatureValueState.INVALID)
	if not isinstance(state, FeatureValueState):
		state = FeatureValueState.INVALID
	return FeatureIssue(channel_key(channel), ticker_id, session, FeatureIssueState(state.value), f"Configured feature channel is {state.value}", getattr(observation, "quality", QualityFlag.CLEAN), getattr(observation, "gap_length", 0), getattr(observation, "provenance", ()))


def construct_configured_features(
	specification: FeatureSpecV2,
	timing: DecisionTiming,
	sessions: tuple[TradingSession, ...],
	data: CloseData | None,
	channel_data: FeatureChannelData | None = None,
	openings: OpeningData | None = None,
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT,
	publication: DailyPublicationPolicy | None = None,
) -> FeatureValues:
	"""Construct v2 channels in the exact shared sample layout.

	Stored channels are supplied by ``FeatureChannelData``.  This function does
	not calculate, repair, or zero-fill a materialization; unready rows become
	typed issues and make the complete vector unavailable.
	"""

	if not isinstance(specification, FeatureSpecV2):
		raise ValueError("Configured feature construction requires FeatureSpecV2")
	availability_policy.validate_features(specification)
	expected_sessions = tuple(calendar_session for calendar_session in sessions)
	if len(expected_sessions) != specification.lookback_sessions or tuple(session.sequence for session in expected_sessions) != tuple(range(timing.entry_session.sequence - specification.lookback_sessions, timing.entry_session.sequence)):
		raise ValueError("Configured feature history must be the exact prior-session window")
	if any(session.calendar_id != timing.entry_session.calendar_id for session in expected_sessions):
		raise ValueError("Configured feature history uses the wrong calendar")
	if channel_data is not None and channel_data.query.specification != specification:
		raise ValueError("Configured feature channel data uses a different specification")
	if specification.external_channels and channel_data is None:
		raise ValueError("Configured stored channels require an injected feature channel repository")
	if specification.requires_entry_open:
		if openings is None or openings.query.sessions != (timing.entry_session,) or openings.query.ticker_ids != specification.ticker_ids or openings.query.as_of != timing.decision_cutoff:
			raise ValueError("Configured opening features require the exact entry session, cutoff and full ticker universe")

	by_key = {} if data is None else {(item.timing.session, item.ticker_id): item for item in data.observations}
	close_issues = [] if data is None else [issue for issue in data.issues if issue.session in sessions]
	opening_prices = {} if openings is None else {item.ticker_id: item.price for item in openings.observations}
	issues: list[CloseIssue | FeatureIssue] = list(close_issues)
	values: list[float] = []
	quality_items: list[Qualified] = []
	if data is not None:
		quality_items.extend(item for item in data.observations if item.timing.session in sessions and item.ticker_id in specification.ticker_ids)
		quality_items.extend(close_issues)
	if channel_data is not None:
		quality_items.extend(channel_data.observations)
	for session in sessions:
		for ticker_id in specification.ticker_ids:
			for channel in specification.feature_order:
				if channel is FeatureChannel.RELATIVE_CLOSE or channel is FeatureChannel.LOG_CLOSE:
					close_observation = by_key.get((session, ticker_id))
					if close_observation is None:
						continue
					if not availability_policy.observation_available_at(close_observation.timing, timing.decision_cutoff, publication):
						issues.append(CloseIssue(ticker_id, session, PriceState.NOT_YET_AVAILABLE, "Historical close was published after the sample decision"))
						continue
					try:
						value = close_observation.close / by_key[(sessions[0], ticker_id)].close - 1 if channel is FeatureChannel.RELATIVE_CLOSE else math.log(close_observation.close)
					except (KeyError, ValueError, ZeroDivisionError):
						continue
					if not math.isfinite(value):
						issues.append(FeatureIssue(channel_key(channel), ticker_id, session, FeatureIssueState.INVALID, "Close transformation is not finite"))
					else:
						values.append(value)
					continue
				if channel is FeatureChannel.ENTRY_OPEN_RETURN:
					close_observation = by_key.get((sessions[-1], ticker_id))
					if close_observation is None or ticker_id not in opening_prices:
						continue
					value = opening_prices[ticker_id] / close_observation.close - 1
					if math.isfinite(value):
						values.append(value)
					else:
						issues.append(FeatureIssue(channel_key(channel), ticker_id, session, FeatureIssueState.INVALID, "Entry opening transformation is not finite"))
					continue
				assert channel_data is not None
				try:
					channel_observation = channel_data.value(channel, session, ticker_id)
				except KeyError:
					issues.append(FeatureIssue(channel_key(channel), ticker_id, session, FeatureIssueState.MISSING, "Prepared feature channel omitted a required key"))
					continue
				readiness = channel_data.readiness_for(channel, session)
				if readiness is not None and readiness.state is not FeatureReadinessState.READY:
					issues.append(FeatureIssue(channel_key(channel), ticker_id, session, FeatureIssueState(readiness.state.value), readiness.detail, provenance=(readiness.revision,)))
				elif channel_observation.state is FeatureValueState.READY:
					assert channel_observation.value is not None
					values.append(float(channel_observation.value))
				else:
					issues.append(_configured_issue(channel_observation, channel, session, ticker_id))
	if issues:
		quality, gap_length, provenance = combine_quality(quality_items)
		return FeatureValues(specification, expected_sessions, None, tuple(issues), quality, gap_length, provenance)
	if len(values) != specification.input_size or any(not math.isfinite(value) for value in values):
		return FeatureValues(specification, expected_sessions, None, (FeatureIssue("layout", specification.ticker_ids[0], expected_sessions[0], FeatureIssueState.INVALID, "Configured feature layout is incomplete or non-finite"),))
	quality, gap_length, provenance = combine_quality(quality_items)
	return FeatureValues(specification, expected_sessions, tuple(values), quality=quality, gap_length=gap_length, provenance=provenance)
