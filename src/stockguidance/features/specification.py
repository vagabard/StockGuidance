from dataclasses import dataclass
from enum import StrEnum

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.validation import require_enum, require_positive_int
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.intervals import ObservationInterval, require_daily_interval


class SignalMode(StrEnum):
	PREVIOUS_CLOSE = "previous_close"
	ENTRY_OPEN = "entry_open"


class MissingHistoryPolicy(StrEnum):
	UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FeatureSpec:
	"""Only closing-price history through the previous completed session is input.

	Ticker order is exact. Missing lookback is unavailable, never zero-filled.
	Stage 5 must add explicit transformations and numerical layout before use.
	"""

	ticker_ids: tuple[int, ...]
	lookback_sessions: int
	signal_mode: SignalMode = SignalMode.PREVIOUS_CLOSE
	interval: ObservationInterval = ObservationInterval.DAILY
	missing_history: MissingHistoryPolicy = MissingHistoryPolicy.UNAVAILABLE

	def __post_init__(self) -> None:
		require_enum(self.signal_mode, SignalMode, "signal_mode")
		if self.signal_mode is SignalMode.ENTRY_OPEN:
			raise ValueError("entry_open is reserved for Stage 10 and is not implemented")
		require_daily_interval(self.interval)
		require_enum(self.missing_history, MissingHistoryPolicy, "missing_history")
		require_positive_int(self.lookback_sessions, "lookback_sessions")
		if not isinstance(self.ticker_ids, tuple) or not self.ticker_ids:
			raise ValueError("ticker_ids must be a nonempty ordered tuple")
		for ticker_id in self.ticker_ids:
			require_positive_int(ticker_id, "ticker_id")
		if len(set(self.ticker_ids)) != len(self.ticker_ids):
			raise ValueError("ticker_ids must be unique")

	def validate_observation(self, observation: ObservationTiming, timing: DecisionTiming) -> None:
		if not isinstance(observation, ObservationTiming) or not isinstance(timing, DecisionTiming):
			raise ValueError("Feature availability requires observation and decision timing contracts")
		if observation.session.calendar_id != timing.entry_session.calendar_id:
			raise ValueError("Feature observations must use the decision calendar")
		if observation.session.sequence > timing.previous_session.sequence:
			raise ValueError("previous_close cannot use entry-session or future daily data")
		if observation.session.sequence <= timing.previous_session.sequence - self.lookback_sessions:
			raise ValueError("Feature observation falls outside the specified lookback")
		if observation.observed_at > timing.previous_session.closes_at:
			raise ValueError("Feature observation must not follow the previous completed close")
		if not observation.is_available_at(timing.decision_cutoff):
			raise ValueError("Feature observation was not available at the decision cutoff")
