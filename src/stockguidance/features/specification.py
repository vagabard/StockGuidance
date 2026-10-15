from dataclasses import dataclass
from enum import StrEnum

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import PriceAdjustment
from stockguidance.config.validation import require_enum, require_positive_int
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.intervals import ObservationInterval, require_daily_interval


class SignalMode(StrEnum):
	PREVIOUS_CLOSE = "previous_close"
	ENTRY_OPEN = "entry_open"


class MissingHistoryPolicy(StrEnum):
	UNAVAILABLE = "unavailable"


class CloseFeature(StrEnum):
	RELATIVE_CLOSE = "close_over_first_window_close_minus_one"
	LOG_CLOSE = "log_close"


@dataclass(frozen=True)
class FeatureSpec:
	"""Only closing-price history through the previous completed session is input.

	Ticker order is exact. Missing lookback is unavailable, never zero-filled.
	Layout is oldest session first, then ticker, then feature, in C order.
	Transformations are stateless and use only the observed window.
	"""

	ticker_ids: tuple[int, ...]
	lookback_sessions: int
	signal_mode: SignalMode = SignalMode.PREVIOUS_CLOSE
	interval: ObservationInterval = ObservationInterval.DAILY
	missing_history: MissingHistoryPolicy = MissingHistoryPolicy.UNAVAILABLE
	feature_order: tuple[CloseFeature, ...] = (CloseFeature.RELATIVE_CLOSE,)
	adjustment: PriceAdjustment = PriceAdjustment.ADJUSTED

	def __post_init__(self) -> None:
		require_enum(self.signal_mode, SignalMode, "signal_mode")
		if self.signal_mode is SignalMode.ENTRY_OPEN:
			raise ValueError("entry_open is reserved for Stage 10 and is not implemented")
		require_daily_interval(self.interval)
		require_enum(self.missing_history, MissingHistoryPolicy, "missing_history")
		require_enum(self.adjustment, PriceAdjustment, "price adjustment")
		if not isinstance(self.feature_order, tuple) or not self.feature_order:
			raise ValueError("feature_order must be a nonempty ordered tuple")
		for feature in self.feature_order:
			require_enum(feature, CloseFeature, "close feature")
		if len(set(self.feature_order)) != len(self.feature_order):
			raise ValueError("feature_order must be unique")
		require_positive_int(self.lookback_sessions, "lookback_sessions")
		if not isinstance(self.ticker_ids, tuple) or not self.ticker_ids:
			raise ValueError("ticker_ids must be a nonempty ordered tuple")
		for ticker_id in self.ticker_ids:
			require_positive_int(ticker_id, "ticker_id")
		if len(set(self.ticker_ids)) != len(self.ticker_ids):
			raise ValueError("ticker_ids must be unique")

	@property
	def shape(self) -> tuple[int, int, int]:
		return self.lookback_sessions, len(self.ticker_ids), len(self.feature_order)

	@property
	def input_size(self) -> int:
		return self.lookback_sessions * len(self.ticker_ids) * len(self.feature_order)

	@property
	def version(self) -> str:
		return "previous-close-v1"

	@property
	def layout(self) -> str:
		return "session_oldest_first,ticker,feature:C"

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
