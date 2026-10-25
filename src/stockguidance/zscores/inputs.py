from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_enum, require_positive_int
from stockguidance.zscores.specification import ZScoreFamily
from stockguidance.market_data.quality import QualityFlag


class InputState(StrEnum):
	AVAILABLE = "available"
	MISSING = "missing"
	UNVERIFIED = "unverified"
	NOT_YET_AVAILABLE = "not_yet_available"
	INVALID = "invalid"
	INSUFFICIENT_HISTORY = "insufficient_history"


@dataclass(frozen=True)
class ZScoreObservation:
	"""One explicitly supplied value and its source/evidence state.

	Values are intentionally accepted as objects so malformed and non-finite
	inputs become INVALID_INPUT rows instead of being silently discarded.
	"""

	value: object | None = None
	state: InputState = InputState.AVAILABLE
	actual_available: bool = True
	quality: QualityFlag = QualityFlag.CLEAN
	gap_length: int = 0
	provenance: tuple[str, ...] = ()

	def __post_init__(self) -> None:
		require_enum(self.state, InputState, "z-score input state")
		if type(self.actual_available) is not bool:
			raise ValueError("actual_available must be a boolean")

	@classmethod
	def ready(cls, value: object) -> "ZScoreObservation":
		return cls(value=value)

	@classmethod
	def missing(cls) -> "ZScoreObservation":
		return cls(state=InputState.MISSING)

	@classmethod
	def unverified(cls, value: object | None = None) -> "ZScoreObservation":
		return cls(value=value, state=InputState.UNVERIFIED)

	@classmethod
	def not_yet_available(cls, value: object | None = None) -> "ZScoreObservation":
		return cls(value=value, state=InputState.NOT_YET_AVAILABLE, actual_available=False)

	@classmethod
	def invalid(cls, value: object | None = None) -> "ZScoreObservation":
		return cls(value=value, state=InputState.INVALID)

	@classmethod
	def insufficient_history(cls) -> "ZScoreObservation":
		return cls(state=InputState.INSUFFICIENT_HISTORY)


@dataclass(frozen=True)
class TickerSeries:
	ticker_id: int
	observations: tuple[ZScoreObservation, ...]

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "z-score ticker_id")
		if not isinstance(self.observations, tuple) or any(not isinstance(item, ZScoreObservation) for item in self.observations):
			raise ValueError("ticker observations must be a tuple of ZScoreObservation values")


@dataclass(frozen=True)
class ZScoreInput:
	"""Ordered values for one family and its declared session dependencies."""

	family: ZScoreFamily
	sessions: tuple[TradingSession, ...]
	tickers: tuple[TickerSeries, ...]
	dependency_sessions: tuple[TradingSession, ...] | None = None
	scored_sessions: tuple[TradingSession, ...] | None = None

	def __post_init__(self) -> None:
		require_enum(self.family, ZScoreFamily, "z-score input family")
		if not isinstance(self.sessions, tuple) or any(not isinstance(item, TradingSession) for item in self.sessions):
			raise ValueError("z-score sessions must be a tuple of TradingSession values")
		self._validate_order(self.sessions, "input sessions")
		if not isinstance(self.tickers, tuple) or any(not isinstance(item, TickerSeries) for item in self.tickers):
			raise ValueError("z-score tickers must be a tuple of TickerSeries values")
		ticker_ids = tuple(series.ticker_id for series in self.tickers)
		if len(set(ticker_ids)) != len(ticker_ids):
			raise ValueError("z-score ticker IDs must be unique")
		for series in self.tickers:
			if len(series.observations) != len(self.sessions):
				raise ValueError("each ticker must provide one observation per input session")

		dependencies = self.sessions if self.dependency_sessions is None else self.dependency_sessions
		if not isinstance(dependencies, tuple) or any(not isinstance(item, TradingSession) for item in dependencies):
			raise ValueError("dependency sessions must be a tuple of TradingSession values")
		self._validate_order(dependencies, "dependency sessions")
		if dependencies and self.sessions and dependencies[0].calendar_id != self.sessions[0].calendar_id:
			raise ValueError("dependency sessions must use the input session calendar")
		if self.dependency_sessions is None:
			object.__setattr__(self, "dependency_sessions", dependencies)

		scored = self.sessions if self.scored_sessions is None else self.scored_sessions
		if not isinstance(scored, tuple) or any(not isinstance(item, TradingSession) for item in scored):
			raise ValueError("scored sessions must be a tuple of TradingSession values")
		self._validate_order(scored, "scored sessions")
		input_keys = {(item.calendar_id, item.sequence) for item in self.sessions}
		if any((item.calendar_id, item.sequence) not in input_keys for item in scored):
			raise ValueError("scored sessions must be selected from input sessions")
		if self.scored_sessions is None:
			object.__setattr__(self, "scored_sessions", scored)

	@staticmethod
	def _validate_order(sessions: tuple[TradingSession, ...], name: str) -> None:
		seen: set[tuple[str, int]] = set()
		for previous, current in zip(sessions, sessions[1:]):
			previous_key = (previous.calendar_id, previous.sequence)
			current_key = (current.calendar_id, current.sequence)
			if previous_key in seen:
				raise ValueError(f"{name} must not repeat a session")
			seen.add(previous_key)
			if previous.calendar_id != current.calendar_id or previous.sequence >= current.sequence or previous.session_date >= current.session_date:
				raise ValueError(f"{name} must be chronologically ordered")
		if sessions:
			last_key = (sessions[-1].calendar_id, sessions[-1].sequence)
			if last_key in seen:
				raise ValueError(f"{name} must not repeat a session")

	@classmethod
	def from_values(
		cls,
		family: ZScoreFamily,
		sessions: tuple[TradingSession, ...],
		values: Mapping[int, Sequence[object | ZScoreObservation]],
		*,
		dependency_sessions: tuple[TradingSession, ...] | None = None,
		scored_sessions: tuple[TradingSession, ...] | None = None,
	) -> "ZScoreInput":
		series: list[TickerSeries] = []
		for ticker_id, ticker_values in values.items():
			observations = tuple(value if isinstance(value, ZScoreObservation) else ZScoreObservation.ready(value) for value in ticker_values)
			series.append(TickerSeries(ticker_id, observations))
		return cls(family, sessions, tuple(series), dependency_sessions, scored_sessions)

	@property
	def session_by_sequence(self) -> dict[int, TradingSession]:
		return {session.sequence: session for session in self.sessions}

	@property
	def dependency_sequences(self) -> frozenset[int]:
		return frozenset(session.sequence for session in self.dependency_sessions or ())
