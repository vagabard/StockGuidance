from dataclasses import dataclass
from enum import StrEnum

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import PriceAdjustment
from stockguidance.config.validation import require_enum, require_positive_int
from stockguidance.market_data.availability import AvailabilityPolicy, ObservationTiming
from stockguidance.market_data.intervals import ObservationInterval, require_daily_interval
from stockguidance.market_data.openings import OPENING_ENDPOINT, OPENING_METHOD
from stockguidance.market_data.publication import DailyPublicationPolicy


class SignalMode(StrEnum):
	PREVIOUS_CLOSE = "previous_close"
	ENTRY_OPEN = "entry_open"


class MissingHistoryPolicy(StrEnum):
	UNAVAILABLE = "unavailable"


class CloseFeature(StrEnum):
	RELATIVE_CLOSE = "close_over_first_window_close_minus_one"
	LOG_CLOSE = "log_close"
	ENTRY_OPEN_RETURN = "entry_open_over_previous_close_minus_one"


@dataclass(frozen=True)
class FeatureSpec:
	"""Closing history, optionally supplemented by an observed entry opening.

	Ticker order is exact. Missing lookback is unavailable, never zero-filled.
	Layout is oldest session first, then ticker, then feature, in C order.
	Transformations are stateless and use only that history and a timely opening.
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
		require_daily_interval(self.interval)
		require_enum(self.missing_history, MissingHistoryPolicy, "missing_history")
		require_enum(self.adjustment, PriceAdjustment, "price adjustment")
		if not isinstance(self.feature_order, tuple) or not self.feature_order:
			raise ValueError("feature_order must be a nonempty ordered tuple")
		for feature in self.feature_order:
			require_enum(feature, CloseFeature, "close feature")
		if len(set(self.feature_order)) != len(self.feature_order):
			raise ValueError("feature_order must be unique")
		if self.signal_mode is SignalMode.ENTRY_OPEN:
			if len(self.feature_order) < 2 or self.feature_order[-1] is not CloseFeature.ENTRY_OPEN_RETURN:
				raise ValueError("entry_open requires close features followed by entry_open_over_previous_close_minus_one")
		elif CloseFeature.ENTRY_OPEN_RETURN in self.feature_order:
			raise ValueError("previous_close cannot contain an entry opening feature")
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
		return "previous-close-v1" if self.signal_mode is SignalMode.PREVIOUS_CLOSE else "entry-open-v1"

	@property
	def layout(self) -> str:
		return "session_oldest_first,ticker,feature:C" if self.signal_mode is SignalMode.PREVIOUS_CLOSE else "session_oldest_first,ticker,feature:C;opening_channel_repeated_over_history"

	@property
	def preprocessing(self) -> str:
		return "stateless_window_only" if self.signal_mode is SignalMode.PREVIOUS_CLOSE else "stateless_previous_closes_and_entry_open"

	@property
	def opening_contract(self) -> dict[str, str] | None:
		if self.signal_mode is SignalMode.PREVIOUS_CLOSE:
			return None
		return {"endpoint": OPENING_ENDPOINT, "method": OPENING_METHOD, "availability": "actual_receipt_after_first_minute_and_no_later_than_decision_cutoff", "missing_policy": "entire_sample_unavailable_if_any_ordered_ticker_is_missing", "transformation": CloseFeature.ENTRY_OPEN_RETURN.value}

	def validate_observation(self, observation: ObservationTiming, timing: DecisionTiming, *, availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT, publication: DailyPublicationPolicy | None = None) -> None:
		if not isinstance(observation, ObservationTiming) or not isinstance(timing, DecisionTiming):
			raise ValueError("Feature availability requires observation and decision timing contracts")
		if not isinstance(availability_policy, AvailabilityPolicy):
			raise ValueError("Feature availability requires a typed availability policy")
		availability_policy.validate_features(self)
		if observation.session.calendar_id != timing.entry_session.calendar_id:
			raise ValueError("Feature observations must use the decision calendar")
		if observation.session.sequence > timing.previous_session.sequence:
			raise ValueError("previous_close cannot use entry-session or future daily data")
		if observation.session.sequence <= timing.previous_session.sequence - self.lookback_sessions:
			raise ValueError("Feature observation falls outside the specified lookback")
		if observation.observed_at > timing.previous_session.closes_at:
			raise ValueError("Feature observation must not follow the previous completed close")
		if not availability_policy.observation_available_at(observation, timing.decision_cutoff, publication):
			raise ValueError("Feature observation was not available at the decision cutoff")
