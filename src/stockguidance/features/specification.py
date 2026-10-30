from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias, cast

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


class FeatureChannel(StrEnum):
	"""Scalar channels accepted by the configurable feature contract.

	The v1 ``CloseFeature`` enum remains the public representation for legacy
	jobs.  This enum is deliberately separate so adding stored channels does
	not change the meaning or version of those jobs.
	"""

	RELATIVE_CLOSE = "relative_close"
	LOG_CLOSE = "log_close"
	RAW_VOLUME = "raw_volume"
	ENTRY_OPEN_RETURN = "entry_open_return"


def _zscore_window(value: object, name: str) -> int:
	if type(value) is not int or value < 2:
		raise ValueError(f"{name} must be an integer >= 2")
	return value


@dataclass(frozen=True)
class PriceZScore:
	window: int

	def __post_init__(self) -> None:
		_zscore_window(self.window, "price z-score window")

	@property
	def key(self) -> str:
		return f"price_zscore_{self.window}"


@dataclass(frozen=True)
class PriceDeltaZScore:
	window: int

	def __post_init__(self) -> None:
		_zscore_window(self.window, "price-delta z-score window")

	@property
	def key(self) -> str:
		return f"price_delta_zscore_{self.window}"


@dataclass(frozen=True)
class VolumeZScore:
	window: int

	def __post_init__(self) -> None:
		_zscore_window(self.window, "volume z-score window")

	@property
	def key(self) -> str:
		return f"volume_zscore_{self.window}"


FeatureChannelValue: TypeAlias = FeatureChannel | PriceZScore | PriceDeltaZScore | VolumeZScore
FeatureSpecification: TypeAlias = "FeatureSpec | FeatureSpecV2"


def _parse_channel_string(value: str) -> FeatureChannelValue | None:
	try:
		return FeatureChannel(value)
	except ValueError:
		pass
	for prefix, constructor in (
		("price_zscore_", PriceZScore),
		("price_delta_zscore_", PriceDeltaZScore),
		("volume_zscore_", VolumeZScore),
	):
		if value.startswith(prefix):
			window = value[len(prefix):]
			if window.isdigit():
				return constructor(int(window))
	return None


def channel_key(channel: FeatureChannelValue | CloseFeature | str) -> str:
	"""Return the stable serialized name for one ordered channel."""

	if isinstance(channel, (PriceZScore, PriceDeltaZScore, VolumeZScore)):
		return channel.key
	if isinstance(channel, CloseFeature):
		return {
			CloseFeature.RELATIVE_CLOSE: FeatureChannel.RELATIVE_CLOSE.value,
			CloseFeature.LOG_CLOSE: FeatureChannel.LOG_CLOSE.value,
			CloseFeature.ENTRY_OPEN_RETURN: FeatureChannel.ENTRY_OPEN_RETURN.value,
		}[channel]
	if isinstance(channel, FeatureChannel):
		return channel.value
	if isinstance(channel, str):
		parsed = _parse_channel_string(channel)
		if parsed is not None:
			return channel_key(parsed)
		raise ValueError(f"Unsupported feature channel: {channel}")
	raise ValueError("Invalid feature channel")


def _coerce_channel(value: object) -> FeatureChannelValue:
	if isinstance(value, (PriceZScore, PriceDeltaZScore, VolumeZScore)):
		return value
	if isinstance(value, CloseFeature):
		return {
			CloseFeature.RELATIVE_CLOSE: FeatureChannel.RELATIVE_CLOSE,
			CloseFeature.LOG_CLOSE: FeatureChannel.LOG_CLOSE,
			CloseFeature.ENTRY_OPEN_RETURN: FeatureChannel.ENTRY_OPEN_RETURN,
		}[value]
	if isinstance(value, FeatureChannel):
		return value
	if isinstance(value, str):
		parsed = _parse_channel_string(value)
		if parsed is not None:
			return parsed
		legacy = {item.value: item for item in (CloseFeature.RELATIVE_CLOSE, CloseFeature.LOG_CLOSE, CloseFeature.ENTRY_OPEN_RETURN)}
		if value in legacy:
			return _coerce_channel(legacy[value])
		raise ValueError(f"Unsupported feature channel: {value}") from None
	raise ValueError("Invalid feature channel")


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


@dataclass(frozen=True)
class FeatureSpecV2:
	"""Versioned ordered channels for stored and derived feature inputs.

	The feature tensor is always ``(time, ticker, channel)``.  Ticker IDs are
	stable identities and the tuple order is the model's fixed tensor order;
	they are never converted into a dense ID range or descriptors.
	"""

	ticker_ids: tuple[int, ...]
	lookback_sessions: int
	channels: tuple[FeatureChannelValue | CloseFeature | str, ...]
	signal_mode: SignalMode = SignalMode.PREVIOUS_CLOSE
	interval: ObservationInterval = ObservationInterval.DAILY
	missing_history: MissingHistoryPolicy = MissingHistoryPolicy.UNAVAILABLE
	adjustment: PriceAdjustment = PriceAdjustment.ADJUSTED

	def __post_init__(self) -> None:
		require_enum(self.signal_mode, SignalMode, "signal_mode")
		require_daily_interval(self.interval)
		require_enum(self.missing_history, MissingHistoryPolicy, "missing_history")
		require_enum(self.adjustment, PriceAdjustment, "price adjustment")
		require_positive_int(self.lookback_sessions, "lookback_sessions")
		if not isinstance(self.ticker_ids, tuple) or not self.ticker_ids:
			raise ValueError("ticker_ids must be a nonempty ordered tuple")
		for ticker_id in self.ticker_ids:
			require_positive_int(ticker_id, "ticker_id")
		if len(set(self.ticker_ids)) != len(self.ticker_ids):
			raise ValueError("ticker_ids must be unique")
		if not isinstance(self.channels, tuple) or not self.channels:
			raise ValueError("channels must be a nonempty ordered tuple")
		coerced = tuple(_coerce_channel(channel) for channel in self.channels)
		keys = tuple(channel_key(channel) for channel in coerced)
		if len(set(keys)) != len(keys):
			raise ValueError("channels must be unique")
		if self.signal_mode is SignalMode.ENTRY_OPEN:
			if coerced[-1] is not FeatureChannel.ENTRY_OPEN_RETURN:
				raise ValueError("entry_open requires entry_open_return as the last channel")
		elif FeatureChannel.ENTRY_OPEN_RETURN in coerced:
			raise ValueError("previous_close cannot contain an entry opening channel")
		object.__setattr__(self, "channels", coerced)

	@property
	def feature_order(self) -> tuple[FeatureChannelValue, ...]:
		return cast(tuple[FeatureChannelValue, ...], self.channels)

	@property
	def shape(self) -> tuple[int, int, int]:
		return self.lookback_sessions, len(self.ticker_ids), len(self.channels)

	@property
	def input_size(self) -> int:
		return self.lookback_sessions * len(self.ticker_ids) * len(self.channels)

	@property
	def version(self) -> str:
		return "configured-features-v2"

	@property
	def layout(self) -> str:
		return "session_oldest_first,ticker,feature:C"

	@property
	def preprocessing(self) -> str:
		return "stored_and_derived_channels_v2"

	@property
	def channel_keys(self) -> tuple[str, ...]:
		return tuple(channel_key(channel) for channel in self.feature_order)

	@property
	def close_channels(self) -> tuple[FeatureChannelValue, ...]:
		return tuple(channel for channel in self.feature_order if channel in (FeatureChannel.RELATIVE_CLOSE, FeatureChannel.LOG_CLOSE))

	@property
	def requires_entry_open(self) -> bool:
		return FeatureChannel.ENTRY_OPEN_RETURN in self.channels

	@property
	def external_channels(self) -> tuple[FeatureChannelValue, ...]:
		return tuple(channel for channel in self.feature_order if channel not in (FeatureChannel.RELATIVE_CLOSE, FeatureChannel.LOG_CLOSE, FeatureChannel.ENTRY_OPEN_RETURN))

	@property
	def required_close_sessions(self) -> int:
		"""Number of prior closes needed for all configured channels."""

		required = self.lookback_sessions
		for channel in self.feature_order:
			if isinstance(channel, (PriceZScore, PriceDeltaZScore)):
				required = max(required, self.lookback_sessions + channel.window - 1 + int(isinstance(channel, PriceDeltaZScore)))
		return required

	@property
	def required_external_sessions(self) -> int:
		"""Dependency span for stored channels, including z-score warm-up."""

		required = self.lookback_sessions
		for channel in self.external_channels:
			if isinstance(channel, (VolumeZScore, PriceZScore, PriceDeltaZScore)):
				required = max(required, self.lookback_sessions + channel.window - 1 + int(isinstance(channel, PriceDeltaZScore)))
		return required

	@property
	def required_history_sessions(self) -> int:
		return max(self.required_close_sessions, self.required_external_sessions)

	def to_mapping(self) -> dict[str, object]:
		channels: list[str] = list(self.channel_keys)
		return {
			"schema": self.version,
			"ticker_ids": list(self.ticker_ids),
			"lookback_sessions": self.lookback_sessions,
			"channels": channels,
			"signal_mode": self.signal_mode.value,
			"interval": self.interval.value,
			"missing_history": self.missing_history.value,
			"adjustment": self.adjustment.value,
			"layout": self.layout,
		}
