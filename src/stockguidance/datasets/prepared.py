"""One immutable history allocation shared by indexed training windows and replay.

The database adapter owns selection and loading. This module owns numerical
layout and never opens a database connection or consults receipt timestamps.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import cached_property
from hashlib import sha256
from time import perf_counter
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
	import numpy as np
	from numpy.typing import NDArray

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceIdentity
from stockguidance.datasets.sample import Sample, TargetState
from stockguidance.features.construction import FeatureValues
from stockguidance.features.history_window import WindowState
from stockguidance.features.specification import FeatureSpecification, channel_key
from stockguidance.market_data.availability import AvailabilityPolicy, ObservationTiming
from stockguidance.market_data.closes import CloseObservation
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.quality import QualityFlag
from stockguidance.preprocessing import FittedPreprocessing
from stockguidance.targets.construction import TargetValues
from stockguidance.targets.specification import TargetKind, TargetSpecification


@dataclass(frozen=True)
class PreparedHistory:
	source: SourceIdentity
	calendar: SessionCalendar
	sessions: tuple[TradingSession, ...]
	features: FeatureSpecification
	target: TargetSpecification
	fields: dict[str, NDArray[np.float64]]
	quality: NDArray[np.uint16]
	selection: dict[str, Any]
	timings: dict[str, Any]
	revision: str = field(init=False)
	_session_index: dict[int,int] = field(init=False,repr=False)
	_ticker_index: dict[int,int] = field(init=False,repr=False)

	def __post_init__(self) -> None:
		import numpy as np
		shape = (len(self.sessions), len(self.features.ticker_ids))
		if self.quality.shape != shape or self.quality.dtype != np.uint16:
			raise ValueError("Prepared quality flags disagree with the selected layout")
		if not self.sessions or not self.fields or any(b.sequence != a.sequence + 1 for a, b in zip(self.sessions, self.sessions[1:])):
			raise ValueError("Prepared history requires an adjacent trading-session axis")
		for array in self.fields.values():
			if array.shape != shape or array.dtype != np.float64:
				raise ValueError("Prepared history arrays disagree with the selected layout")
			array.flags.writeable = False
		self.quality.flags.writeable = False
		object.__setattr__(self, "_session_index", {s.sequence: i for i, s in enumerate(self.sessions)})
		object.__setattr__(self, "_ticker_index", {t: i for i, t in enumerate(self.features.ticker_ids)})
		digest = sha256()
		for name, array in sorted(self.fields.items()):
			digest.update(name.encode())
			digest.update(memoryview(array).cast("B"))
		digest.update(memoryview(self.quality).cast("B"))
		object.__setattr__(self, "revision", content_id({"schema": "prepared-history-1", "values": digest.hexdigest(), "tickers": self.features.ticker_ids, "sessions": [str(s.session_date) for s in self.sessions], "policy": "current_prepared", "source":asdict(self.source), "features":asdict(self.features), "target":self.target.to_mapping()}))

	def position(self, session: TradingSession) -> int:
		index: dict[int, int] = self._session_index
		position = index[session.sequence]
		if self.sessions[position] != session:
			raise ValueError("Prepared history session belongs to a different calendar")
		return position

	def feature_array(self, timing: DecisionTiming) -> NDArray[np.float64]:
		import numpy as np
		end = self.position(timing.previous_session) + 1
		start = end - self.features.lookback_sessions
		if start < 0:
			raise ValueError("Prepared history omits the feature lookback")
		channels = []
		for channel in self.features.feature_order:
			key = channel_key(channel)
			if key in ("relative_close", "close_over_first_window_close_minus_one"):
				values = self.fields["close"][start:end] / self.fields["close"][start] - 1
			elif key == "log_close":
				values = np.log(self.fields["close"][start:end])
			elif key in ("entry_open_return", "entry_open_over_previous_close_minus_one"):
				entry = self.position(timing.entry_session)
				values = np.broadcast_to(self.fields["open"][entry] / self.fields["close"][end - 1] - 1, (end-start, len(self.features.ticker_ids)))
			else:
				values = self.fields[key][start:end]
			channels.append(values)
		result = channels[0].reshape(-1) if len(channels) == 1 else np.stack(channels, axis=-1).reshape(-1)
		if not np.isfinite(result).all():
			raise ValueError("Prepared feature transformation is not finite")
		return result

	def target_arrays(self, timing: DecisionTiming) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
		import numpy as np
		entry = self.position(timing.entry_session)
		exit_index = self.position(timing.exit_session)
		closes = self.fields["close"]
		raw = closes[exit_index] / closes[entry] - 1
		kind = getattr(self.target, "kind", TargetKind.RAW_RETURN)
		if kind is TargetKind.RETURN_ZSCORE_PER_TICKER:
			window = self.target.rolling_window_sessions
			assert window is not None
			baseline = closes[entry-window+1:entry+1] / closes[entry-window:entry] - 1
			if len(baseline) != window:
				raise ValueError("Prepared history omits the target baseline")
			mean = baseline.mean(axis=0)
			variance = ((baseline - mean) ** 2).mean(axis=0)
		elif kind is TargetKind.RETURN_ZSCORE_CROSS_SECTIONAL:
			mean = raw.mean()
			variance = ((raw - mean) ** 2).mean()
		else:
			return raw, raw
		result = np.divide(raw - mean, np.sqrt(variance), out=np.zeros_like(raw), where=variance != 0)
		if not np.isfinite(result).all():
			raise ValueError("Prepared target transformation is not finite")
		return raw, result

	def sample(self, timing: DecisionTiming, cutoff: datetime | None = None) -> Sample:
		import numpy as np
		end = self.position(timing.previous_session) + 1
		sessions = self.sessions[end-self.features.lookback_sessions:end]
		quality = QualityFlag(int(np.bitwise_or.reduce(self.quality[end-self.features.lookback_sessions:end].reshape(-1), initial=0)))
		features = FeatureValues(self.features, sessions, tuple(self.feature_array(timing)), quality=quality)
		values: tuple[float | None, ...] = (None,) * len(self.features.ticker_ids)
		state = TargetState.NOT_REQUESTED if cutoff is None else TargetState.NOT_YET_AVAILABLE
		targets = TargetValues(values, values)
		if cutoff is not None and timing.exit_session.closes_at <= cutoff:
			raw, transformed = self.target_arrays(timing)
			entry = self.position(timing.entry_session)
			label_quality = QualityFlag(int(np.bitwise_or.reduce(self.quality[[entry+o for o in (*self.target.baseline_offsets,0,1)]].reshape(-1),initial=0)))
			targets = TargetValues(tuple(raw), tuple(transformed),quality=label_quality)
			state = TargetState.READY
		revision = self.revision
		return Sample(self.source, timing, features, WindowState.EXPECTED_AVAILABLE, self.target, targets, state, cutoff, revision, revision, self.calendar.version, AvailabilityPolicy.CURRENT_PREPARED)

	def read(self, query: Any) -> Any:
		"""Compatibility boundary for execution quotes; no database reads."""
		from stockguidance.database.repositories.closes import CloseData
		index: dict[int, int] = self._ticker_index
		observations = tuple(CloseObservation(t, float(self.fields["close"][self.position(s), index[t]]), ObservationTiming(s, s.closes_at, s.closes_at), self.revision, QualityFlag(int(self.quality[self.position(s), index[t]]))) for s in query.scope.sessions for t in query.ticker_ids)
		return CloseData(query, observations, ())

	def liquidity(self, query: Any) -> Any:
		from stockguidance.recommendations.eligibility import LiquidityObservation
		index: dict[int, int] = self._ticker_index
		session = query.scope.sessions[0]
		return tuple(LiquidityObservation(t, int(self.fields["transactions"][self.position(session), index[t]]), ObservationTiming(session, session.closes_at, session.closes_at), self.revision) for t in query.ticker_ids)

	def dataset(self, decisions: tuple[DecisionTiming, ...], cutoff: datetime) -> ArrayTrainingDataset:
		return ArrayTrainingDataset(self, decisions, cutoff)


@dataclass(frozen=True)
class PreparedLiquidity:
	history: PreparedHistory

	def read(self, query: Any) -> Any:
		return self.history.liquidity(query)


@dataclass(frozen=True)
class ArrayCase:
	"""Small provenance record; feature vectors are constructed by the loader."""
	history: PreparedHistory
	timing: DecisionTiming
	label_cutoff: datetime

	@property
	def features(self) -> _FeatureReference:
		return _FeatureReference(self.history.features)

	@property
	def source(self) -> SourceIdentity:
		return self.history.source

	@property
	def target_specification(self) -> TargetSpecification:
		return self.history.target

	@property
	def calendar_version(self) -> str:
		return self.history.calendar.version

	@property
	def availability_policy(self) -> AvailabilityPolicy:
		return AvailabilityPolicy.CURRENT_PREPARED

	@property
	def training_eligible(self) -> bool:
		return self.timing.exit_session.closes_at <= self.label_cutoff

	@property
	def cache_key(self) -> str:
		return content_id({"history": self.history.revision, "entry": str(self.timing.entry_session.session_date), "target": self.target_specification.to_mapping(), "cutoff": self.label_cutoff.isoformat()})


@dataclass(frozen=True)
class _FeatureReference:
	specification: FeatureSpecification


@dataclass(frozen=True)
class ArrayTrainingDataset:
	history: PreparedHistory
	decisions: tuple[DecisionTiming, ...]
	training_cutoff: datetime
	preprocessing: FittedPreprocessing | None = None
	data_snapshot: datetime | None = None

	def __post_init__(self) -> None:
		import numpy as np
		if not self.decisions or any(d.decision_cutoff > self.training_cutoff for d in self.decisions) or any(b.entry_session.sequence <= a.entry_session.sequence for a,b in zip(self.decisions,self.decisions[1:])):
			raise ValueError("Array training decisions must be unique, chronological, and precede the cutoff")
		if self.preprocessing is not None:
			self.preprocessing.validate_for(self.history.features, self.history.target)

	@cached_property
	def samples(self) -> tuple[ArrayCase, ...]:
		return tuple(ArrayCase(self.history, d, self.training_cutoff) for d in self.decisions)

	@cached_property
	def eligible(self) -> tuple[ArrayCase, ...]:
		return tuple(s for s in self.samples if s.training_eligible)

	@property
	def excluded(self) -> tuple[Sample, ...]:
		return tuple(self.history.sample(d, self.training_cutoff) for d in self.decisions if d.exit_session.closes_at > self.training_cutoff)

	@cached_property
	def revision(self) -> str:
		return content_id({"schema": "array-training-1", "history": self.history.revision, "samples": [s.cache_key for s in self.samples], "preprocessing": None if self.preprocessing is None else self.preprocessing.identity})

	def with_preprocessing(self, preprocessing: FittedPreprocessing) -> ArrayTrainingDataset:
		return replace(self, preprocessing=preprocessing)

	def case_arrays(self, index: int) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
		started = perf_counter()
		timing = self.eligible[index].timing
		features = self.history.feature_array(timing).reshape(1, -1)
		targets = self.history.target_arrays(timing)[1].reshape(1, -1)
		if self.preprocessing is not None:
			features = self.preprocessing.transform_inputs(features)
			targets = self.preprocessing.transform_outputs(targets)
		self.history.timings["case_materialization_seconds"] = self.history.timings.get("case_materialization_seconds",0.0)+perf_counter()-started
		self.history.timings["materialized_cases"] = self.history.timings.get("materialized_cases",0)+1
		return features[0], targets[0]

