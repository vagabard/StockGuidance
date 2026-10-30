"""Typed read contracts for configured stored and derived feature channels.

Feature construction is intentionally independent of MySQL and preparation
commands.  A database adapter supplies exact values together with readiness
and revision evidence; the shared builder decides layout and availability.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import math
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_enum, require_positive_int, require_text
from stockguidance.features.specification import FeatureChannelValue, FeatureSpecV2, channel_key
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.quality import QualityFlag


class FeatureValueState(StrEnum):
	READY = "ready"
	INSUFFICIENT_HISTORY = "insufficient_history"
	MISSING = "missing"
	UNVERIFIED = "unverified"
	NOT_YET_AVAILABLE = "not_yet_available"
	INVALID = "invalid"
	ZERO_VARIANCE = "zero_variance"
	STALE = "stale"
	PENDING = "pending"


class FeatureReadinessState(StrEnum):
	READY = "ready"
	UNVERIFIED = "unverified"
	PENDING = "pending"
	STALE = "stale"
	INCOMPLETE = "incomplete"


def _ordered_sessions(values: tuple[TradingSession, ...], name: str) -> tuple[TradingSession, ...]:
	if not isinstance(values, tuple) or not values or any(not isinstance(value, TradingSession) for value in values):
		raise ValueError(f"{name} must be a nonempty tuple of sessions")
	if any(current.calendar_id != values[0].calendar_id or current.sequence != previous.sequence + 1 for previous, current in zip(values, values[1:])):
		raise ValueError(f"{name} must contain adjacent sessions from one calendar")
	return values


@dataclass(frozen=True)
class FeatureChannelQuery:
	source: SourceIdentity
	specification: FeatureSpecV2
	dependency_sessions: tuple[TradingSession, ...]
	scored_sessions: tuple[TradingSession, ...]
	as_of: datetime

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity) or not isinstance(self.specification, FeatureSpecV2):
			raise ValueError("Feature channel query requires a source and FeatureSpecV2")
		dependencies = _ordered_sessions(self.dependency_sessions, "feature dependency sessions")
		scored = _ordered_sessions(self.scored_sessions, "feature scored sessions")
		if any(session not in dependencies for session in scored):
			raise ValueError("Feature scored sessions must be in the dependency scope")
		if len(scored) != self.specification.lookback_sessions:
			raise ValueError("Feature scored sessions must match the model lookback")
		if any(current.sequence != previous.sequence + 1 for previous, current in zip(scored, scored[1:])):
			raise ValueError("Feature scored sessions must be adjacent")
		if dependencies[-len(scored):] != scored:
			raise ValueError("Feature dependency scope must end at the scored feature window")
		require_aware_datetime(self.as_of, "feature channel cutoff")


@dataclass(frozen=True)
class FeatureChannelObservation:
	channel: FeatureChannelValue
	session: TradingSession
	ticker_id: int
	value: float | None
	state: FeatureValueState = FeatureValueState.READY
	revision: str = "fixture"
	quality: QualityFlag = QualityFlag.CLEAN
	gap_length: int = 0
	provenance: tuple[str, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.session, TradingSession):
			raise ValueError("Feature channel observation requires a session")
		require_positive_int(self.ticker_id, "feature channel ticker_id")
		require_enum(self.state, FeatureValueState, "feature channel value state")
		require_text(self.revision, "feature channel revision")
		if type(self.gap_length) is not int or self.gap_length < 0:
			raise ValueError("Feature channel gap_length must be nonnegative")
		if self.state is FeatureValueState.READY:
			if isinstance(self.value, bool) or not isinstance(self.value, (int, float)) or not math.isfinite(float(self.value)):
				raise ValueError("Ready feature channel values must be finite")
		elif self.value is not None:
			raise ValueError("Unavailable feature channel values must be null")


@dataclass(frozen=True)
class FeatureReadinessEvidence:
	channel: FeatureChannelValue
	session: TradingSession
	state: FeatureReadinessState
	revision: str
	detail: str

	def __post_init__(self) -> None:
		if not isinstance(self.session, TradingSession):
			raise ValueError("Feature readiness requires a session")
		require_enum(self.state, FeatureReadinessState, "feature readiness state")
		require_text(self.revision, "feature readiness revision")
		require_text(self.detail, "feature readiness detail")


@dataclass(frozen=True)
class FeatureChannelData:
	query: FeatureChannelQuery
	observations: tuple[FeatureChannelObservation, ...]
	revision: str
	readiness: tuple[FeatureReadinessEvidence, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.query, FeatureChannelQuery) or not isinstance(self.observations, tuple) or not isinstance(self.readiness, tuple):
			raise ValueError("Invalid feature channel data contract")
		require_text(self.revision, "feature channel data revision")
		if any(not isinstance(row, FeatureChannelObservation) for row in self.observations) or any(not isinstance(item, FeatureReadinessEvidence) for item in self.readiness):
			raise ValueError("Invalid feature channel observation or readiness evidence")
		expected = {(channel_key(channel), session, ticker_id) for channel in self.query.specification.external_channels for session in self.query.scored_sessions for ticker_id in self.query.specification.ticker_ids}
		actual = {(channel_key(row.channel), row.session, row.ticker_id) for row in self.observations}
		if len(actual) != len(self.observations) or actual != expected:
			raise ValueError("Feature channel data must account for every external channel/session/ticker key")

	@property
	def revision_with_readiness(self) -> str:
		return content_id({
			"data": self.revision,
			"observations": [(channel_key(item.channel), item.session.session_date.isoformat(), item.ticker_id, item.value, item.state.value, item.revision, int(item.quality), item.gap_length, item.provenance) for item in self.observations],
			"readiness": [(channel_key(item.channel), item.session.session_date.isoformat(), item.state.value, item.revision) for item in self.readiness],
		})

	def value(self, channel: FeatureChannelValue, session: TradingSession, ticker_id: int) -> FeatureChannelObservation:
		key = (channel_key(channel), session, ticker_id)
		for observation in self.observations:
			if (channel_key(observation.channel), observation.session, observation.ticker_id) == key:
				return observation
		raise KeyError(key)

	def readiness_for(self, channel: FeatureChannelValue, session: TradingSession) -> FeatureReadinessEvidence | None:
		for evidence in self.readiness:
			if channel_key(evidence.channel) == channel_key(channel) and evidence.session == session:
				return evidence
		return None


class FeatureChannelRepository(Protocol):
	"""Read prepared channel values without preparing or mutating storage."""

	def read(self, query: FeatureChannelQuery) -> FeatureChannelData: ...


__all__ = [
	"FeatureChannelData", "FeatureChannelObservation", "FeatureChannelQuery", "FeatureChannelRepository",
	"FeatureReadinessEvidence", "FeatureReadinessState", "FeatureValueState",
]
