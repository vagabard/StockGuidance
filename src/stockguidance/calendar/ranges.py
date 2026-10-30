"""Versioned date expressions and exchange-session dependency planning.

This module is deliberately independent of databases, providers, models, and
training controllers.  A caller supplies the source coverage snapshot and the
authoritative exchange calendar; resolution then happens once and produces a
portable record suitable for a saved run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Mapping, Protocol, cast

from stockguidance.calendar.clock import Clock
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date, require_positive_int, require_text
from stockguidance.market_data.intervals import ObservationInterval


RANGE_SCHEMA = "stockguidance-date-resolution-1"


class RangeResolutionError(ValueError):
	"""Base error for invalid, unavailable, or ambiguous date resolution."""


class RangeConfigurationError(RangeResolutionError):
	"""The expression or dependency configuration is invalid."""


class RangeCoverageError(RangeResolutionError):
	"""The requested date or a dependency lacks verified source coverage."""


class RangeOutOfCoverageError(RangeCoverageError):
	"""A date falls outside the verified source coverage boundary."""


class RangeUnknownCoverageError(RangeCoverageError):
	"""A date is inside the requested scope but its source state is unresolved."""


class RangeChronologyError(RangeResolutionError):
	"""A resolved range or dependency plan is empty or out of order."""


class RerunResolutionMode(StrEnum):
	PINNED = "pinned"
	RERESOLVE = "re_resolve"


class DateAnchor(StrEnum):
	FIRST_DATA = "first_data"
	LAST_DATA = "last_data"
	LAST_COMPLETED_AVAILABLE = "last_completed_available"
	TODAY = "today"


class CalendarOffsetUnit(StrEnum):
	CALENDAR_DAYS = "calendar_days"
	CALENDAR_YEARS = "calendar_years"


class DurationUnit(StrEnum):
	CALENDAR_DAYS = "calendar_days"
	CALENDAR_YEARS = "calendar_years"
	EXCHANGE_SESSIONS = "exchange_sessions"


class CoverageStatus(StrEnum):
	VERIFIED = "verified"
	ABSENT = "absent"
	UNKNOWN = "unknown"
	INCOMPLETE = "incomplete"


class _RangeClock(Protocol):
	def now(self) -> datetime: ...


def _require_mapping(value: object, name: str) -> dict[str, object]:
	if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
		raise RangeConfigurationError(f"{name} must be a string-keyed mapping")
	return dict(value)


def _parse_date(value: object, name: str) -> date:
	if isinstance(value, date) and not isinstance(value, datetime):
		return value
	if isinstance(value, str):
		try:
			parsed = date.fromisoformat(value)
		except ValueError:
			raise RangeConfigurationError(f"{name} must be an ISO date") from None
		return parsed
	raise RangeConfigurationError(f"{name} must be an ISO date")


def _parse_aware_datetime(value: object, name: str) -> datetime:
	if isinstance(value, datetime):
		parsed = value
	elif isinstance(value, str):
		try:
			parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
		except ValueError:
			raise RangeConfigurationError(f"{name} must be an ISO timestamp") from None
	else:
		raise RangeConfigurationError(f"{name} must be an ISO timestamp")
	require_aware_datetime(parsed, name)
	return parsed


def _require_int(value: object, name: str) -> int:
	if type(value) is not int:
		raise RangeConfigurationError(f"{name} must be an integer")
	return value


def _parse_offset(value: object, name: str) -> tuple[int, str]:
	payload = _require_mapping(value, name)
	if set(payload) != {"value", "unit"}:
		raise RangeConfigurationError(f"{name} requires value and unit")
	amount = _require_int(payload["value"], f"{name}.value")
	unit = payload["unit"]
	if not isinstance(unit, str):
		raise RangeConfigurationError(f"{name}.unit must be text")
	return amount, unit


@dataclass(frozen=True)
class AbsoluteDate:
	value: date

	def __post_init__(self) -> None:
		require_date(self.value, "absolute date")

	def to_mapping(self) -> dict[str, object]:
		return {"date": self.value.isoformat()}


@dataclass(frozen=True)
class RelativeDate:
	anchor: DateAnchor
	offset_value: int = 0
	offset_unit: CalendarOffsetUnit = CalendarOffsetUnit.CALENDAR_DAYS

	def __post_init__(self) -> None:
		if not isinstance(self.anchor, DateAnchor):
			raise RangeConfigurationError("relative date anchor must be a DateAnchor")
		if type(self.offset_value) is not int:
			raise RangeConfigurationError("relative date offset_value must be an integer")
		if not isinstance(self.offset_unit, CalendarOffsetUnit):
			raise RangeConfigurationError("relative date offset_unit must be a CalendarOffsetUnit")

	def to_mapping(self) -> dict[str, object]:
		return {"anchor": self.anchor.value, "offset": {"value": self.offset_value, "unit": self.offset_unit.value}}


DateExpression = AbsoluteDate | RelativeDate


def parse_date_expression(value: object) -> DateExpression:
	if isinstance(value, (AbsoluteDate, RelativeDate)):
		return value
	if isinstance(value, (date, datetime)):
		return AbsoluteDate(_parse_date(value, "date expression"))
	if isinstance(value, str):
		return AbsoluteDate(_parse_date(value, "date expression"))
	payload = _require_mapping(value, "date expression")
	if "date" in payload:
		if set(payload) != {"date"}:
			raise RangeConfigurationError("absolute date expression has unknown fields")
		return AbsoluteDate(_parse_date(payload["date"], "date expression.date"))
	if "anchor" not in payload or set(payload) - {"anchor", "offset"}:
		raise RangeConfigurationError("date expression requires date or anchor")
	try:
		anchor = DateAnchor(cast(str, payload["anchor"]))
	except (KeyError, TypeError, ValueError):
		raise RangeConfigurationError("unknown date expression anchor") from None
	amount, unit = (0, CalendarOffsetUnit.CALENDAR_DAYS.value) if "offset" not in payload else _parse_offset(payload["offset"], "date expression.offset")
	try:
		offset_unit = CalendarOffsetUnit(unit)
	except ValueError:
		raise RangeConfigurationError("date expression offset unit must be calendar_days or calendar_years") from None
	return RelativeDate(anchor, amount, offset_unit)


@dataclass(frozen=True)
class ExplicitRange:
	start: DateExpression
	end: DateExpression

	def __post_init__(self) -> None:
		if not isinstance(self.start, (AbsoluteDate, RelativeDate)) or not isinstance(self.end, (AbsoluteDate, RelativeDate)):
			raise RangeConfigurationError("explicit range requires typed start and end expressions")

	def to_mapping(self) -> dict[str, object]:
		return {"start": self.start.to_mapping(), "end": self.end.to_mapping()}


@dataclass(frozen=True)
class AnchorDuration:
	anchor: DateExpression
	duration_value: int
	duration_unit: DurationUnit

	def __post_init__(self) -> None:
		if not isinstance(self.anchor, (AbsoluteDate, RelativeDate)):
			raise RangeConfigurationError("anchor duration requires a typed anchor")
		if type(self.duration_value) is not int or self.duration_value == 0:
			raise RangeConfigurationError("duration_value must be a nonzero integer")
		if not isinstance(self.duration_unit, DurationUnit):
			raise RangeConfigurationError("duration_unit must be a DurationUnit")

	def to_mapping(self) -> dict[str, object]:
		return {"anchor": self.anchor.to_mapping(), "duration": {"value": self.duration_value, "unit": self.duration_unit.value}}


DateRange = ExplicitRange | AnchorDuration


def parse_date_range(value: object) -> DateRange:
	if isinstance(value, (ExplicitRange, AnchorDuration)):
		return value
	payload = _require_mapping(value, "date range")
	if set(payload) == {"start", "end"}:
		return ExplicitRange(parse_date_expression(payload["start"]), parse_date_expression(payload["end"]))
	if set(payload) == {"anchor", "duration"}:
		amount, unit = _parse_offset(payload["duration"], "date range.duration")
		try:
			duration_unit = DurationUnit(unit)
		except ValueError:
			raise RangeConfigurationError("date range duration unit is invalid") from None
		return AnchorDuration(parse_date_expression(payload["anchor"]), amount, duration_unit)
	raise RangeConfigurationError("date range requires exactly start/end or anchor/duration")


@dataclass(frozen=True)
class CoverageObservation:
	session_date: date
	status: CoverageStatus
	detail: str = ""
	available_at: datetime | None = None

	def __post_init__(self) -> None:
		require_date(self.session_date, "coverage session date")
		if not isinstance(self.status, CoverageStatus):
			raise RangeConfigurationError("coverage status must be typed")
		if not isinstance(self.detail, str):
			raise RangeConfigurationError("coverage detail must be text")
		if self.available_at is not None:
			require_aware_datetime(self.available_at, "coverage available_at")

	def to_mapping(self) -> dict[str, object]:
		return {"date": self.session_date.isoformat(), "status": self.status.value, "detail": self.detail, "available_at": None if self.available_at is None else self.available_at.isoformat()}


@dataclass(frozen=True)
class SourceCoverageSnapshot:
	"""Immutable, source-specific coverage evidence used by one resolution."""

	source: SourceIdentity
	calendar_id: str
	revision: str
	observations: tuple[CoverageObservation, ...]
	snapshot_id: str = ""
	last_completed_available: date | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise RangeConfigurationError("coverage snapshot requires a SourceIdentity")
		require_text(self.calendar_id, "coverage calendar_id")
		require_text(self.revision, "coverage revision")
		if not isinstance(self.observations, tuple):
			raise RangeConfigurationError("coverage observations must be a tuple")
		if any(not isinstance(item, CoverageObservation) for item in self.observations):
			raise RangeConfigurationError("coverage observations must be typed")
		dates = tuple(item.session_date for item in self.observations)
		if dates != tuple(sorted(set(dates))):
			raise RangeConfigurationError("coverage observations must be sorted and unique")
		if self.snapshot_id and not isinstance(self.snapshot_id, str):
			raise RangeConfigurationError("snapshot_id must be text")
		if self.last_completed_available is not None:
			require_date(self.last_completed_available, "last_completed_available")
			if self.status(self.last_completed_available) is not CoverageStatus.VERIFIED:
				raise RangeConfigurationError("last_completed_available must be verified in the snapshot")

	@property
	def verified_sessions(self) -> tuple[date, ...]:
		return tuple(item.session_date for item in self.observations if item.status is CoverageStatus.VERIFIED)

	@property
	def first_verified(self) -> date | None:
		return self.verified_sessions[0] if self.verified_sessions else None

	@property
	def last_verified(self) -> date | None:
		return self.verified_sessions[-1] if self.verified_sessions else None

	def status(self, session_date: date) -> CoverageStatus:
		for item in self.observations:
			if item.session_date == session_date:
				return item.status
		return CoverageStatus.UNKNOWN

	def observation(self, session_date: date) -> CoverageObservation | None:
		return next((item for item in self.observations if item.session_date == session_date), None)

	@classmethod
	def from_verified_dates(
		cls,
		source: SourceIdentity,
		calendar_id: str,
		revision: str,
		verified_sessions: tuple[date, ...] | list[date],
		*,
		absent_sessions: tuple[date, ...] = (),
		unknown_sessions: tuple[date, ...] = (),
		incomplete_sessions: tuple[date, ...] = (),
		snapshot_id: str = "",
		last_completed_available: date | None = None,
	) -> "SourceCoverageSnapshot":
		statuses: dict[date, CoverageStatus] = {}
		for session_date in verified_sessions:
			statuses[_parse_date(session_date, "verified session")] = CoverageStatus.VERIFIED
		for session_date in absent_sessions:
			parsed = _parse_date(session_date, "absent session")
			if parsed in statuses:
				raise RangeConfigurationError("coverage session has multiple statuses")
			statuses[parsed] = CoverageStatus.ABSENT
		for session_date in unknown_sessions:
			parsed = _parse_date(session_date, "unknown session")
			if parsed in statuses:
				raise RangeConfigurationError("coverage session has multiple statuses")
			statuses[parsed] = CoverageStatus.UNKNOWN
		for session_date in incomplete_sessions:
			parsed = _parse_date(session_date, "incomplete session")
			if parsed in statuses:
				raise RangeConfigurationError("coverage session has multiple statuses")
			statuses[parsed] = CoverageStatus.INCOMPLETE
		return cls(source, calendar_id, revision, tuple(CoverageObservation(day, statuses[day]) for day in sorted(statuses)), snapshot_id, last_completed_available)

	def to_mapping(self) -> dict[str, object]:
		return {
			"snapshot_id": self.snapshot_id or None,
			"revision": self.revision,
			"source": _source_mapping(self.source),
			"calendar_id": self.calendar_id,
			"observations": [item.to_mapping() for item in self.observations],
			"last_completed_available": None if self.last_completed_available is None else self.last_completed_available.isoformat(),
		}


@dataclass(frozen=True)
class DependencySpec:
	"""Independent session dependencies for samples and evaluation planning."""

	model_lookback_sessions: int = 1
	training_end_gap_sessions: int = 0
	zscore_window_sessions: int | None = None
	price_delta_predecessor_sessions: int = 0
	target_baseline_sessions: int = 0
	label_horizon_sessions: int = 1
	simulation_horizon_sessions: int = 10

	def __post_init__(self) -> None:
		require_positive_int(self.model_lookback_sessions, "model_lookback_sessions")
		if type(self.training_end_gap_sessions) is not int or self.training_end_gap_sessions < 0:
			raise RangeConfigurationError("training_end_gap_sessions must be nonnegative")
		if self.zscore_window_sessions is not None:
			require_positive_int(self.zscore_window_sessions, "zscore_window_sessions")
		if type(self.price_delta_predecessor_sessions) is not int or self.price_delta_predecessor_sessions < 0:
			raise RangeConfigurationError("price_delta_predecessor_sessions must be nonnegative")
		if self.zscore_window_sessions is None and self.price_delta_predecessor_sessions:
			raise RangeConfigurationError("price_delta_predecessor_sessions requires zscore_window_sessions")
		if type(self.target_baseline_sessions) is not int or self.target_baseline_sessions < 0:
			raise RangeConfigurationError("target_baseline_sessions must be nonnegative")
		require_positive_int(self.label_horizon_sessions, "label_horizon_sessions")
		require_positive_int(self.simulation_horizon_sessions, "simulation_horizon_sessions")

	@property
	def feature_warmup_sessions(self) -> int:
		if self.zscore_window_sessions is None:
			return self.model_lookback_sessions
		return self.model_lookback_sessions + self.zscore_window_sessions - 1 + self.price_delta_predecessor_sessions

	def to_mapping(self) -> dict[str, object]:
		return {
			"model_lookback_sessions": self.model_lookback_sessions,
			"training_end_gap_sessions": self.training_end_gap_sessions,
			"zscore_window_sessions": self.zscore_window_sessions,
			"price_delta_predecessor_sessions": self.price_delta_predecessor_sessions,
			"target_baseline_sessions": self.target_baseline_sessions,
			"label_horizon_sessions": self.label_horizon_sessions,
			"simulation_horizon_sessions": self.simulation_horizon_sessions,
			"feature_warmup_sessions": self.feature_warmup_sessions,
		}

	@classmethod
	def from_mapping(cls, value: object) -> "DependencySpec":
		payload = _require_mapping(value, "dependencies")
		allowed = {"model_lookback_sessions", "training_end_gap_sessions", "zscore_window_sessions", "price_delta_predecessor_sessions", "target_baseline_sessions", "label_horizon_sessions", "simulation_horizon_sessions", "feature_warmup_sessions"}
		if set(payload) - allowed:
			raise RangeConfigurationError("dependencies contains unknown fields")
		def integer(name: str, default: int) -> int:
			item = payload.get(name, default)
			if type(item) is not int:
				raise RangeConfigurationError(f"dependencies.{name} must be an integer")
			return item

		zscore = payload.get("zscore_window_sessions")
		if zscore is not None and type(zscore) is not int:
			raise RangeConfigurationError("dependencies.zscore_window_sessions must be an integer or null")
		result = cls(
			model_lookback_sessions=integer("model_lookback_sessions", 1),
			training_end_gap_sessions=integer("training_end_gap_sessions", 0),
			zscore_window_sessions=zscore,
			price_delta_predecessor_sessions=integer("price_delta_predecessor_sessions", 0),
			target_baseline_sessions=integer("target_baseline_sessions", 0),
			label_horizon_sessions=integer("label_horizon_sessions", 1),
			simulation_horizon_sessions=integer("simulation_horizon_sessions", 10),
		)
		if "feature_warmup_sessions" in payload and payload["feature_warmup_sessions"] != result.feature_warmup_sessions:
			raise RangeConfigurationError("dependencies.feature_warmup_sessions does not match its independent settings")
		return result


@dataclass(frozen=True)
class DatePlanSpec:
	training: DateRange
	evaluation: DateRange | None = None
	dependencies: DependencySpec = field(default_factory=DependencySpec)

	def __post_init__(self) -> None:
		if not isinstance(self.training, (ExplicitRange, AnchorDuration)):
			raise RangeConfigurationError("training date plan requires a date range")
		if self.evaluation is not None and not isinstance(self.evaluation, (ExplicitRange, AnchorDuration)):
			raise RangeConfigurationError("evaluation date plan requires a date range")
		if not isinstance(self.dependencies, DependencySpec):
			raise RangeConfigurationError("date plan requires typed dependencies")

	def to_mapping(self) -> dict[str, object]:
		return {"training": self.training.to_mapping(), "evaluation": None if self.evaluation is None else self.evaluation.to_mapping(), "dependencies": self.dependencies.to_mapping()}


@dataclass(frozen=True)
class AnchorEvidence:
	expression: dict[str, object]
	anchor: str | None
	anchor_date: date | None
	resolved_date: date
	evidence: str

	def __post_init__(self) -> None:
		if not isinstance(self.expression, dict) or not all(isinstance(key, str) for key in self.expression):
			raise RangeConfigurationError("anchor expression must be a mapping")
		if self.anchor is not None:
			require_text(self.anchor, "anchor evidence anchor")
		if self.anchor_date is not None:
			require_date(self.anchor_date, "anchor evidence anchor_date")
		require_date(self.resolved_date, "anchor evidence resolved_date")
		require_text(self.evidence, "anchor evidence detail")

	def to_mapping(self) -> dict[str, object]:
		return {"expression": self.expression, "anchor": self.anchor, "anchor_date": None if self.anchor_date is None else self.anchor_date.isoformat(), "resolved_date": self.resolved_date.isoformat(), "evidence": self.evidence}


def _session_mapping(session: TradingSession) -> dict[str, object]:
	return {"date": session.session_date.isoformat(), "sequence": session.sequence, "opens_at": session.opens_at.isoformat(), "closes_at": session.closes_at.isoformat()}


@dataclass(frozen=True)
class ResolvedDateRange:
	original: DateRange
	requested_start: date
	requested_end: date
	start_session: TradingSession
	end_session: TradingSession
	sessions: tuple[TradingSession, ...]
	anchors: tuple[AnchorEvidence, ...]
	source: SourceIdentity
	source_snapshot_id: str
	source_revision: str
	calendar_id: str
	calendar_version: str

	def __post_init__(self) -> None:
		if not isinstance(self.original, (ExplicitRange, AnchorDuration)):
			raise RangeConfigurationError("resolved range requires an original date range")
		require_date(self.requested_start, "requested start")
		require_date(self.requested_end, "requested end")
		if not isinstance(self.start_session, TradingSession) or not isinstance(self.end_session, TradingSession):
			raise RangeConfigurationError("resolved range requires typed boundary sessions")
		if not isinstance(self.sessions, tuple) or not self.sessions:
			raise RangeChronologyError("resolved range must contain sessions")
		if self.sessions[0] != self.start_session or self.sessions[-1] != self.end_session:
			raise RangeChronologyError("resolved range boundaries do not match effective sessions")
		if any(not isinstance(item, TradingSession) for item in self.sessions):
			raise RangeConfigurationError("resolved sessions must be typed")
		if any(right.sequence != left.sequence + 1 for left, right in zip(self.sessions, self.sessions[1:])):
			raise RangeChronologyError("resolved sessions must be consecutive exchange sessions")
		if not isinstance(self.anchors, tuple) or any(not isinstance(item, AnchorEvidence) for item in self.anchors):
			raise RangeConfigurationError("resolved anchors must be typed")
		require_text(self.source_snapshot_id, "source snapshot ID")
		require_text(self.source_revision, "source revision")
		require_text(self.calendar_id, "resolved calendar ID")
		require_text(self.calendar_version, "resolved calendar version")

	@property
	def start(self) -> date:
		return self.start_session.session_date

	@property
	def end(self) -> date:
		return self.end_session.session_date

	@property
	def entry_sessions(self) -> tuple[TradingSession, ...]:
		return self.sessions

	def to_mapping(self) -> dict[str, object]:
		return {
			"original": self.original.to_mapping(),
			"requested_start": self.requested_start.isoformat(),
			"requested_end": self.requested_end.isoformat(),
			"start": self.start.isoformat(),
			"end": self.end.isoformat(),
			"effective_sessions": [_session_mapping(item) for item in self.sessions],
			"anchors": [item.to_mapping() for item in self.anchors],
			"source_snapshot_id": self.source_snapshot_id,
			"source_revision": self.source_revision,
			"source": _source_mapping(self.source),
			"calendar": {"calendar_id": self.calendar_id, "version": self.calendar_version},
		}


@dataclass(frozen=True)
class DependencyPlan:
	"""Resolved entry sessions and all explicitly requested session dependencies."""

	specification: DatePlanSpec
	training: ResolvedDateRange
	evaluation: ResolvedDateRange
	feature_warmup_sessions: tuple[TradingSession, ...]
	target_baseline_sessions: tuple[TradingSession, ...]
	training_label_exit_sessions: tuple[TradingSession, ...]
	evaluation_label_exit_sessions: tuple[TradingSession, ...]
	training_information_cutoff: TradingSession
	evaluation_decision_cutoff: TradingSession
	feature_cutoff_before_first_training: TradingSession
	feature_cutoff_before_first_evaluation: TradingSession
	final_evaluation_open_position_session: TradingSession

	def __post_init__(self) -> None:
		for name in ("feature_warmup_sessions", "target_baseline_sessions", "training_label_exit_sessions", "evaluation_label_exit_sessions"):
			values = getattr(self, name)
			if not isinstance(values, tuple) or any(not isinstance(item, TradingSession) for item in values):
				raise RangeConfigurationError(f"{name} must contain typed sessions")
		for name in ("training_information_cutoff", "evaluation_decision_cutoff", "feature_cutoff_before_first_training", "feature_cutoff_before_first_evaluation", "final_evaluation_open_position_session"):
			if not isinstance(getattr(self, name), TradingSession):
				raise RangeConfigurationError(f"{name} must be a typed session")
		if self.training_information_cutoff.sequence >= self.evaluation.entry_sessions[0].sequence:
			raise RangeChronologyError("evaluation begins before training labels mature")

	@property
	def earliest_fitting_cutoff(self) -> TradingSession:
		return self.feature_cutoff_before_first_training

	@property
	def earliest_simulation_cutoff(self) -> TradingSession:
		return self.evaluation_decision_cutoff

	@property
	def simulation_sessions(self) -> tuple[TradingSession, ...]:
		return self.evaluation.entry_sessions

	@property
	def warmup_sessions(self) -> tuple[TradingSession, ...]:
		return tuple(sorted(set(self.feature_warmup_sessions) | set(self.target_baseline_sessions), key=lambda item: item.sequence))

	def to_mapping(self) -> dict[str, object]:
		return {
			"schema": RANGE_SCHEMA,
			"original": self.specification.to_mapping(),
			"dependencies": self.specification.dependencies.to_mapping(),
			"training": self.training.to_mapping(),
			"evaluation": self.evaluation.to_mapping(),
			"feature_warmup_sessions": [_session_mapping(item) for item in self.feature_warmup_sessions],
			"target_baseline_sessions": [_session_mapping(item) for item in self.target_baseline_sessions],
			"training_label_exit_sessions": [_session_mapping(item) for item in self.training_label_exit_sessions],
			"evaluation_label_exit_sessions": [_session_mapping(item) for item in self.evaluation_label_exit_sessions],
			"training_information_cutoff": _session_mapping(self.training_information_cutoff),
			"evaluation_decision_cutoff": _session_mapping(self.evaluation_decision_cutoff),
			"earliest_fitting_cutoff": _session_mapping(self.earliest_fitting_cutoff),
			"earliest_simulation_cutoff": _session_mapping(self.earliest_simulation_cutoff),
			"final_evaluation_open_position_session": _session_mapping(self.final_evaluation_open_position_session),
		}


def _source_mapping(source: SourceIdentity) -> dict[str, object]:
	return {"provider": source.provider, "endpoint": source.endpoint, "market": source.market, "universe": source.universe, "adjustment": source.adjustment.value, "interval": source.interval.value}


def _add_calendar_years(value: date, amount: int) -> date:
	try:
		return value.replace(year=value.year + amount)
	except ValueError:
		# The only invalid Gregorian replacement produced by this contract is
		# February 29 in a non-leap year; clamp it as explicitly specified.
		if value.month == 2 and value.day == 29:
			return value.replace(year=value.year + amount, day=28)
		raise


def _source_status(status: CoverageStatus) -> str:
	return status.value


class DateRangeResolver:
	"""Resolve typed dates against injected source evidence and a calendar."""

	def __init__(self, calendar: SessionCalendar, coverage: SourceCoverageSnapshot, source: SourceIdentity, clock: Clock | _RangeClock) -> None:
		if not isinstance(coverage, SourceCoverageSnapshot):
			raise RangeConfigurationError("resolver requires a SourceCoverageSnapshot")
		if coverage.source != source:
			raise RangeConfigurationError("coverage snapshot belongs to a different source")
		if coverage.calendar_id != calendar.calendar_id:
			raise RangeConfigurationError("coverage snapshot belongs to a different calendar")
		self.calendar = calendar
		self.coverage = coverage
		self.source = source
		self.clock = clock
		require_aware_datetime(clock.now(), "resolver clock")

	def resolve(self, value: DateRange) -> ResolvedDateRange:
		if not isinstance(value, (ExplicitRange, AnchorDuration)):
			raise RangeConfigurationError("date range resolution requires a typed DateRange")
		if isinstance(value, ExplicitRange):
			start_date, start_evidence = self._evaluate(value.start)
			end_date, end_evidence = self._evaluate(value.end)
			if start_date > end_date:
				raise RangeChronologyError("requested date range is reversed")
			return self._resolved(value, start_date, end_date, (start_evidence, end_evidence))
		anchor_date, anchor_evidence = self._evaluate(value.anchor)
		if value.duration_unit is DurationUnit.EXCHANGE_SESSIONS:
			return self._resolve_session_duration(value, anchor_date, anchor_evidence)
		end_date = _add_duration(anchor_date, value.duration_value, value.duration_unit)
		start_date, final_end = (anchor_date, end_date) if value.duration_value > 0 else (end_date, anchor_date)
		return self._resolved(value, start_date, final_end, (anchor_evidence,))

	def resolve_plan(self, value: DatePlanSpec) -> DependencyPlan:
		if not isinstance(value, DatePlanSpec):
			raise RangeConfigurationError("dependency planning requires a typed DatePlanSpec")
		training = self.resolve(value.training)
		dependencies = value.dependencies
		training_labels = self._shift_all(training.sessions, dependencies.label_horizon_sessions, "training label exits")
		training_feature_start = self._shift(training.start_session, -dependencies.feature_warmup_sessions, "training feature warm-up")
		training_feature_end = self._shift(training.start_session, -1, "training feature cutoff")
		training_warmup = self._sessions_between(training_feature_start, training_feature_end)
		training_target_start = self._shift(training.start_session, -dependencies.target_baseline_sessions, "target baseline") if dependencies.target_baseline_sessions else ()
		target_baseline = () if not training_target_start else self._sessions_between(training_target_start, training_feature_end)
		self._require_verified((*training_warmup, *target_baseline, *training_labels), "training dependencies")
		training_information_cutoff = max(training_labels, key=lambda item: item.sequence)

		if value.evaluation is None:
			evaluation_start = self._shift(training_information_cutoff, dependencies.training_end_gap_sessions + 1, "automatic evaluation start")
			evaluation_end = self._shift(evaluation_start, dependencies.simulation_horizon_sessions - 1, "simulation horizon")
			evaluation = self.resolve(ExplicitRange(AbsoluteDate(evaluation_start.session_date), AbsoluteDate(evaluation_end.session_date)))
		else:
			evaluation = self.resolve(value.evaluation)
		minimum_evaluation = self._shift(training_information_cutoff, dependencies.training_end_gap_sessions + 1, "evaluation chronology")
		if evaluation.start_session.sequence < minimum_evaluation.sequence:
			raise RangeChronologyError("evaluation begins before the training information cutoff and configured gap")

		evaluation_feature_start = self._shift(evaluation.start_session, -dependencies.feature_warmup_sessions, "evaluation feature warm-up")
		evaluation_feature_end = self._shift(evaluation.start_session, -1, "evaluation feature cutoff")
		evaluation_warmup = self._sessions_between(evaluation_feature_start, evaluation_feature_end)
		evaluation_labels = self._shift_all(evaluation.sessions[:-1], dependencies.label_horizon_sessions, "evaluation label exits")
		self._require_verified((*evaluation_warmup, *evaluation_labels), "evaluation dependencies")
		return DependencyPlan(
			value,
			training,
			evaluation,
			training_warmup,
			target_baseline,
			training_labels,
			evaluation_labels,
			training_information_cutoff,
			evaluation.start_session,
			training_feature_end,
			evaluation_feature_end,
			evaluation.end_session,
		)

	def _evaluate(self, expression: DateExpression) -> tuple[date, AnchorEvidence]:
		if isinstance(expression, AbsoluteDate):
			return expression.value, AnchorEvidence(expression.to_mapping(), None, None, expression.value, "Explicit ISO calendar date")
		if not isinstance(expression, RelativeDate):
			raise RangeConfigurationError("unsupported date expression")
		clock_now = self.clock.now()
		require_aware_datetime(clock_now, "resolver clock")
		if expression.anchor is DateAnchor.FIRST_DATA:
			base = self.coverage.first_verified
			evidence = "First verified source-coverage session"
		elif expression.anchor is DateAnchor.LAST_DATA:
			base = self.coverage.last_verified
			evidence = "Last verified source-coverage session"
		elif expression.anchor is DateAnchor.LAST_COMPLETED_AVAILABLE:
			base = self._last_completed_available(clock_now)
			evidence = "Last verified source session complete and available by the injected clock"
		else:
			base = clock_now.astimezone(self.calendar.timezone).date()
			evidence = "Injected clock date in the exchange-calendar timezone"
		if base is None:
			raise RangeOutOfCoverageError(f"No verified source coverage is available for anchor {expression.anchor.value}")
		resolved = base + timedelta(days=expression.offset_value) if expression.offset_unit is CalendarOffsetUnit.CALENDAR_DAYS else _add_calendar_years(base, expression.offset_value)
		return resolved, AnchorEvidence(expression.to_mapping(), expression.anchor.value, base, resolved, evidence + f"; offset {expression.offset_value} {expression.offset_unit.value}")

	def _last_completed_available(self, now: datetime) -> date | None:
		if self.coverage.last_completed_available is not None:
			candidate = self.coverage.last_completed_available
			if self.calendar.session(candidate).closes_at > now:
				raise RangeOutOfCoverageError("Snapshot last_completed_available is after the injected clock")
			observation = self.coverage.observation(candidate)
			if observation is not None and observation.available_at is not None and observation.available_at > now:
				raise RangeOutOfCoverageError("Snapshot last_completed_available is not available by the injected clock")
			return candidate
		for candidate in reversed(self.coverage.verified_sessions):
			try:
				session = self.calendar.session(candidate)
			except ValueError:
				continue
			observation = self.coverage.observation(candidate)
			if session.closes_at <= now and (observation is None or observation.available_at is None or observation.available_at <= now):
				return candidate
		return None

	def _resolve_session_duration(self, value: AnchorDuration, anchor_date: date, anchor_evidence: AnchorEvidence) -> ResolvedDateRange:
		anchor_session = self._snap_start(anchor_date) if value.duration_value > 0 else self._snap_end(anchor_date)
		count = abs(value.duration_value)
		other = self._shift(anchor_session, (count - 1) if value.duration_value > 0 else -(count - 1), "exchange-session duration")
		start_session, end_session = (anchor_session, other) if value.duration_value > 0 else (other, anchor_session)
		requested_start, requested_end = (anchor_date, other.session_date) if value.duration_value > 0 else (other.session_date, anchor_date)
		return self._resolved_sessions(value, requested_start, requested_end, start_session, end_session, (anchor_evidence,))

	def _resolved(self, original: DateRange, start_date: date, end_date: date, anchors: tuple[AnchorEvidence, ...]) -> ResolvedDateRange:
		start_session = self._snap_start(start_date)
		end_session = self._snap_end(end_date)
		if start_session.sequence > end_session.sequence:
			raise RangeChronologyError("range is empty after exchange-session boundary snapping")
		return self._resolved_sessions(original, start_date, end_date, start_session, end_session, anchors)

	def _resolved_sessions(self, original: DateRange, requested_start: date, requested_end: date, start_session: TradingSession, end_session: TradingSession, anchors: tuple[AnchorEvidence, ...]) -> ResolvedDateRange:
		if requested_start > requested_end or start_session.sequence > end_session.sequence:
			raise RangeChronologyError("resolved date range is empty or reversed")
		first_verified, last_verified = self.coverage.first_verified, self.coverage.last_verified
		if first_verified is None or last_verified is None or requested_start < first_verified or requested_end > last_verified:
			raise RangeOutOfCoverageError("Requested date range extends outside verified source coverage")
		sessions = self._sessions_between(start_session, end_session)
		self._require_verified(sessions, "resolved date range")
		return ResolvedDateRange(original, requested_start, requested_end, start_session, end_session, sessions, anchors, self.source, self.coverage.snapshot_id or self.coverage.revision, self.coverage.revision, self.calendar.calendar_id, self.calendar.version)

	def _snap_start(self, requested: date) -> TradingSession:
		self._calendar_bounds(requested)
		sessions = self.calendar.sessions(requested, self.calendar.end)
		if not sessions:
			raise RangeOutOfCoverageError("Range start is after the loaded exchange-calendar coverage")
		return sessions[0]

	def _snap_end(self, requested: date) -> TradingSession:
		self._calendar_bounds(requested)
		sessions = self.calendar.sessions(self.calendar.start, requested)
		if not sessions:
			raise RangeOutOfCoverageError("Range end is before the loaded exchange-calendar coverage")
		return sessions[-1]

	def _calendar_bounds(self, requested: date) -> None:
		require_date(requested, "resolved calendar date")
		if requested < self.calendar.start or requested > self.calendar.end:
			raise RangeOutOfCoverageError("Resolved date is outside the loaded exchange-calendar coverage")

	def _shift(self, session: TradingSession, offset: int, dependency: str) -> TradingSession:
		try:
			return self.calendar.shift(session, offset)
		except (IndexError, KeyError, ValueError) as error:
			raise RangeOutOfCoverageError(f"{dependency} exceeds the loaded exchange-calendar coverage") from error

	def _shift_all(self, sessions: tuple[TradingSession, ...], offset: int, dependency: str) -> tuple[TradingSession, ...]:
		return tuple(self._shift(session, offset, dependency) for session in sessions)

	def _sessions_between(self, start: TradingSession, end: TradingSession) -> tuple[TradingSession, ...]:
		if start.sequence > end.sequence:
			return ()
		return self.calendar.sessions(start.session_date, end.session_date)

	def _require_verified(self, sessions: tuple[TradingSession, ...], scope: str) -> None:
		for session in sorted(set(sessions), key=lambda item: item.sequence):
			status = self.coverage.status(session.session_date)
			if status is CoverageStatus.VERIFIED:
				observation = self.coverage.observation(session.session_date)
				if observation is not None and observation.available_at is not None and observation.available_at > self.clock.now():
					raise RangeUnknownCoverageError(f"{scope} session {session.session_date.isoformat()} is not available by the injected clock")
				continue
			if status is CoverageStatus.ABSENT:
				raise RangeOutOfCoverageError(f"{scope} session {session.session_date.isoformat()} is known absent from the source snapshot")
			first_verified, last_verified = self.coverage.first_verified, self.coverage.last_verified
			if first_verified is None or last_verified is None or session.session_date < first_verified or session.session_date > last_verified:
				raise RangeOutOfCoverageError(f"{scope} session {session.session_date.isoformat()} is outside verified source coverage")
			raise RangeUnknownCoverageError(f"{scope} session {session.session_date.isoformat()} has { _source_status(status) } source coverage")


def _add_duration(anchor: date, amount: int, unit: DurationUnit) -> date:
	if unit is DurationUnit.CALENDAR_DAYS:
		return anchor + timedelta(days=amount)
	if unit is DurationUnit.CALENDAR_YEARS:
		return _add_calendar_years(anchor, amount)
	raise RangeConfigurationError("exchange_sessions must be resolved with a session-aware duration")


def resolve_rerun(saved: ResolvedDateRange | DependencyPlan, resolver: DateRangeResolver, mode: RerunResolutionMode) -> ResolvedDateRange | DependencyPlan:
	"""Return saved pinned dates or explicitly resolve relative expressions again."""
	if not isinstance(mode, RerunResolutionMode):
		raise RangeConfigurationError("rerun mode must be typed")
	if mode is RerunResolutionMode.PINNED:
		return saved
	if isinstance(saved, ResolvedDateRange):
		return resolver.resolve(saved.original)
	if isinstance(saved, DependencyPlan):
		return resolver.resolve_plan(saved.specification)
	raise RangeConfigurationError("rerun requires a resolved range or dependency plan")


def persist_resolution(run_service: object, run_id: str, resolved: ResolvedDateRange | DependencyPlan) -> object:
	"""Persist resolution through the Stage 1 service without creating another store."""
	if not isinstance(resolved, (ResolvedDateRange, DependencyPlan)):
		raise RangeConfigurationError("persist_resolution requires a resolved range or dependency plan")
	update = getattr(run_service, "update_sections", None)
	if not callable(update):
		raise RangeConfigurationError("run service does not provide update_sections")
	payload = resolved.to_mapping()
	return update(run_id, resolved_configuration=payload, sections={"resolution": payload})


def parse_preview_spec(payload: Mapping[str, object]) -> DatePlanSpec:
	"""Parse the small, non-secret JSON contract accepted by the preview CLI."""
	data = _require_mapping(payload, "preview configuration")
	training_value = data.get("training_range", data.get("date_range"))
	if training_value is None:
		raise RangeConfigurationError("preview configuration requires date_range or training_range")
	evaluation_value = data.get("evaluation_range")
	return DatePlanSpec(parse_date_range(training_value), None if evaluation_value is None else parse_date_range(evaluation_value), DependencySpec.from_mapping(data.get("dependencies", {})))


def coverage_snapshot_from_mapping(source: SourceIdentity, calendar_id: str, payload: Mapping[str, object]) -> SourceCoverageSnapshot:
	data = _require_mapping(payload, "coverage")
	revision = data.get("revision")
	if not isinstance(revision, str) or not revision:
		raise RangeConfigurationError("coverage.revision is required")
	observations: dict[date, CoverageObservation] = {}
	for key, status in (("verified_sessions", CoverageStatus.VERIFIED), ("absent_sessions", CoverageStatus.ABSENT), ("unknown_sessions", CoverageStatus.UNKNOWN), ("incomplete_sessions", CoverageStatus.INCOMPLETE)):
		values = data.get(key, ())
		if not isinstance(values, (list, tuple)):
			raise RangeConfigurationError(f"coverage.{key} must be an array")
		for value in values:
			day = _parse_date(value, f"coverage.{key} date")
			if day in observations:
				raise RangeConfigurationError("coverage session has multiple statuses")
			observations[day] = CoverageObservation(day, status)
	statuses = data.get("statuses", {})
	if statuses:
		status_mapping = _require_mapping(statuses, "coverage.statuses")
		for raw_day, raw_status in status_mapping.items():
			day = _parse_date(raw_day, "coverage.statuses date")
			try:
				state = CoverageStatus(cast(str, raw_status))
			except ValueError:
				raise RangeConfigurationError("coverage.statuses contains an unknown status") from None
			if day in observations:
				raise RangeConfigurationError("coverage session has multiple statuses")
			observations[day] = CoverageObservation(day, state)
	last_completed = data.get("last_completed_available")
	return SourceCoverageSnapshot(source, calendar_id, revision, tuple(observations[day] for day in sorted(observations)), str(data.get("snapshot_id", "")), None if last_completed is None else _parse_date(last_completed, "coverage.last_completed_available"))


def source_identity_from_mapping(payload: Mapping[str, object]) -> SourceIdentity:
	data = _require_mapping(payload, "source")
	try:
		return SourceIdentity(str(data["provider"]), str(data["endpoint"]), str(data["market"]), str(data["universe"]), PriceAdjustment(str(data.get("adjustment", PriceAdjustment.ADJUSTED.value))), ObservationInterval(str(data.get("interval", ObservationInterval.DAILY.value))))
	except (KeyError, TypeError, ValueError):
		raise RangeConfigurationError("source requires provider, endpoint, market, universe, adjustment, and interval") from None


def decode_resolved_plan(payload: Mapping[str, object]) -> DatePlanSpec:
	"""Decode only the original typed plan from a saved resolution mapping."""
	data = _require_mapping(payload, "saved resolution")
	original = _require_mapping(data.get("original"), "saved resolution.original")
	training = parse_date_range(original.get("training"))
	evaluation = original.get("evaluation")
	return DatePlanSpec(training, None if evaluation is None else parse_date_range(evaluation), DependencySpec.from_mapping(original.get("dependencies", data.get("dependencies", {}))))


__all__ = [
	"AbsoluteDate", "AnchorDuration", "AnchorEvidence", "CalendarOffsetUnit", "CoverageObservation", "CoverageStatus", "DateAnchor", "DateExpression", "DatePlanSpec", "DateRange", "DateRangeResolver", "DependencyPlan", "DependencySpec", "DurationUnit", "ExplicitRange", "RANGE_SCHEMA", "RangeChronologyError", "RangeConfigurationError", "RangeCoverageError", "RangeOutOfCoverageError", "RangeResolutionError", "RangeUnknownCoverageError", "ResolvedDateRange", "RerunResolutionMode", "SourceCoverageSnapshot", "coverage_snapshot_from_mapping", "decode_resolved_plan", "parse_date_expression", "parse_date_range", "parse_preview_spec", "persist_resolution", "resolve_rerun", "source_identity_from_mapping",
]
