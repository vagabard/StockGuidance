"""Pure, versioned selection of a complete ordered training universe.

The selector deliberately consumes typed source evidence instead of querying a
database.  Database adapters only assemble :class:`UniverseSourceData`; this
keeps coverage rules, filter formulas, explanations, and stable ordering shared
by fixtures, MySQL reads, and later training orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Protocol

from stockguidance.calendar.ranges import DependencyPlan
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime, require_date, require_positive_int, require_text
from stockguidance.market_data.identity import content_id
from stockguidance.validation.coverage import CoverageState


UNIVERSE_SCHEMA = "stockguidance-universe-selection-1"


class UniverseSelectionError(ValueError):
	"""The requested universe cannot be safely frozen."""


class UniverseCoverageError(UniverseSelectionError):
	"""A whole required session is not known to be complete."""

	def __init__(self, coverage: tuple["SourceSessionEvidence", ...]) -> None:
		self.coverage = coverage
		details = ", ".join(f"{item.session.session_date.isoformat()}={item.state.value}" for item in coverage)
		super().__init__(f"Universe selection is blocked by unknown or incomplete whole-session coverage: {details}")

	def to_mapping(self) -> dict[str, object]:
		return {"code": "unknown_or_incomplete_coverage", "message": str(self), "coverage": [item.to_mapping() for item in self.coverage]}


class EmptyUniverseError(UniverseSelectionError):
	"""No ticker passed complete coverage and the enabled filters."""

	def __init__(self, result: "UniverseSelectionResult") -> None:
		self.result = result
		super().__init__("Universe selection produced no eligible tickers; inspect per-ticker exclusions")


class RawObservationState(StrEnum):
	AVAILABLE = "available"
	MISSING = "missing"
	INVALID = "invalid"
	UNVERIFIED = "unverified"


class ExclusionReason(StrEnum):
	MISSING_SESSION_COVERAGE = "missing_session_coverage"
	UNKNOWN_SESSION_COVERAGE = "unknown_session_coverage"
	MISSING_OBSERVATION = "missing_observation"
	INVALID_OBSERVATION = "invalid_observation"
	UNVERIFIED_OBSERVATION = "unverified_observation"
	MISSING_VOLUME = "missing_volume"
	MISSING_TRANSACTIONS = "missing_transactions"
	AVERAGE_VOLUME_BELOW = "average_volume_below_lower_bound"
	AVERAGE_VOLUME_ABOVE = "average_volume_above_upper_bound"
	VOLATILITY_BELOW = "volatility_below_lower_bound"
	VOLATILITY_ABOVE = "volatility_above_upper_bound"
	TRANSACTIONS_BELOW = "transactions_below_minimum"


def _decimal(value: object, name: str, *, nonnegative: bool = False) -> Decimal:
	if isinstance(value, bool):
		raise ValueError(f"{name} must be a finite number")
	try:
		result = value if isinstance(value, Decimal) else Decimal(str(value))
	except (InvalidOperation, ValueError, TypeError):
		raise ValueError(f"{name} must be a finite number") from None
	if not result.is_finite() or (nonnegative and result < 0):
		raise ValueError(f"{name} must be a finite{' nonnegative' if nonnegative else ''} number")
	return result


def _positive_window(value: object, name: str) -> int:
	if type(value) is not int or value <= 0:
		raise ValueError(f"{name} must be a positive integer")
	return value


def _bound(value: object, name: str) -> Decimal | None:
	return None if value is None else _decimal(value, name, nonnegative=True)


def _validate_bounds(lower: Decimal | None, upper: Decimal | None, name: str) -> None:
	if lower is None and upper is None:
		raise ValueError(f"{name} requires at least one bound")
	if lower is not None and upper is not None and lower > upper:
		raise ValueError(f"{name} lower bound must not exceed upper bound")


@dataclass(frozen=True)
class AverageVolumeFilter:
	window_sessions: int = 20
	lower: Decimal | None = None
	upper: Decimal | None = None

	def __post_init__(self) -> None:
		window = _positive_window(self.window_sessions, "average-volume window")
		lower, upper = _bound(self.lower, "average-volume lower"), _bound(self.upper, "average-volume upper")
		_validate_bounds(lower, upper, "average-volume filter")
		object.__setattr__(self, "window_sessions", window)
		object.__setattr__(self, "lower", lower)
		object.__setattr__(self, "upper", upper)

	def to_mapping(self) -> dict[str, object]:
		return {"window_sessions": self.window_sessions, "lower": None if self.lower is None else str(self.lower), "upper": None if self.upper is None else str(self.upper)}


@dataclass(frozen=True)
class VolatilityFilter:
	window_sessions: int = 20
	lower: Decimal | None = None
	upper: Decimal | None = None

	def __post_init__(self) -> None:
		window = _positive_window(self.window_sessions, "volatility window")
		lower, upper = _bound(self.lower, "volatility lower"), _bound(self.upper, "volatility upper")
		_validate_bounds(lower, upper, "volatility filter")
		object.__setattr__(self, "window_sessions", window)
		object.__setattr__(self, "lower", lower)
		object.__setattr__(self, "upper", upper)

	def to_mapping(self) -> dict[str, object]:
		return {"window_sessions": self.window_sessions, "lower": None if self.lower is None else str(self.lower), "upper": None if self.upper is None else str(self.upper)}


@dataclass(frozen=True)
class LegacyTransactionFilter:
	minimum: int = 200

	def __post_init__(self) -> None:
		if type(self.minimum) is not int or self.minimum < 0:
			raise ValueError("Legacy transaction minimum must be a nonnegative integer")

	def to_mapping(self) -> dict[str, object]:
		return {"minimum": self.minimum}


@dataclass(frozen=True)
class UniverseFilters:
	average_volume: AverageVolumeFilter | None = None
	volatility: VolatilityFilter | None = None
	legacy_transactions: LegacyTransactionFilter | None = None

	def __post_init__(self) -> None:
		if self.average_volume is not None and not isinstance(self.average_volume, AverageVolumeFilter):
			raise ValueError("average_volume has an invalid filter type")
		if self.volatility is not None and not isinstance(self.volatility, VolatilityFilter):
			raise ValueError("volatility has an invalid filter type")
		if self.legacy_transactions is not None and not isinstance(self.legacy_transactions, LegacyTransactionFilter):
			raise ValueError("legacy_transactions has an invalid filter type")

	@property
	def enabled(self) -> bool:
		return any(value is not None for value in (self.average_volume, self.volatility, self.legacy_transactions))

	def to_mapping(self) -> dict[str, object]:
		return {
			"average_volume": None if self.average_volume is None else self.average_volume.to_mapping(),
			"volatility": None if self.volatility is None else self.volatility.to_mapping(),
			"legacy_transactions": None if self.legacy_transactions is None else self.legacy_transactions.to_mapping(),
		}


def _session_key(session: TradingSession) -> tuple[str, int]:
	return session.calendar_id, session.sequence


def _sessions(values: tuple[TradingSession, ...], name: str) -> tuple[TradingSession, ...]:
	if not isinstance(values, tuple) or any(not isinstance(item, TradingSession) for item in values):
		raise ValueError(f"{name} must contain typed sessions")
	if not values:
		raise ValueError(f"{name} must not be empty")
	calendars = {item.calendar_id for item in values}
	if len(calendars) != 1 or len({_session_key(item) for item in values}) != len(values):
		raise ValueError(f"{name} must use one calendar and contain unique sessions")
	return tuple(sorted(values, key=lambda item: item.sequence))


@dataclass(frozen=True)
class TickerIdentity:
	ticker_id: int
	label: str

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "ticker ID")
		require_text(self.label, "ticker label")

	def to_mapping(self) -> dict[str, object]:
		return {"ticker_id": self.ticker_id, "label": self.label}


@dataclass(frozen=True)
class SourceSessionEvidence:
	session: TradingSession
	state: CoverageState
	detail: str
	revision: str | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.session, TradingSession) or not isinstance(self.state, CoverageState):
			raise ValueError("Source session evidence requires a typed session and coverage state")
		require_text(self.detail, "source session detail")
		if self.revision is not None:
			require_text(self.revision, "source session revision")

	def to_mapping(self) -> dict[str, object]:
		return {"date": self.session.session_date.isoformat(), "sequence": self.session.sequence, "state": self.state.value, "detail": self.detail, "revision": self.revision}


@dataclass(frozen=True)
class UniverseObservation:
	ticker_id: int
	session: TradingSession
	state: RawObservationState
	close: Decimal | None = None
	volume: Decimal | None = None
	transactions: int | None = None
	revision: str | None = None

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "observation ticker ID")
		if not isinstance(self.session, TradingSession) or not isinstance(self.state, RawObservationState):
			raise ValueError("Universe observations require typed session and state")
		if self.close is not None:
			close = _decimal(self.close, "observation close", nonnegative=True)
			if close <= 0:
				raise ValueError("Observation close must be positive")
			object.__setattr__(self, "close", close)
		if self.volume is not None:
			object.__setattr__(self, "volume", _decimal(self.volume, "observation volume", nonnegative=True))
		if self.transactions is not None and (type(self.transactions) is not int or self.transactions < 0):
			raise ValueError("Observation transactions must be a nonnegative integer")
		if self.revision is not None:
			require_text(self.revision, "observation revision")
		if self.state is RawObservationState.AVAILABLE and self.close is None:
			raise ValueError("Available observation requires a close")


@dataclass(frozen=True)
class UniverseReadRequest:
	sessions: tuple[TradingSession, ...]
	ticker_ids: tuple[int, ...] | None
	read_volume: bool
	read_transactions: bool
	as_of: datetime | None

	def __post_init__(self) -> None:
		_sessions(self.sessions, "universe read sessions")
		if self.ticker_ids is not None and (not isinstance(self.ticker_ids, tuple) or not self.ticker_ids or len(set(self.ticker_ids)) != len(self.ticker_ids) or any(type(item) is not int or item <= 0 for item in self.ticker_ids)):
			raise ValueError("universe read ticker IDs must be unique positive integers")
		if type(self.read_volume) is not bool or type(self.read_transactions) is not bool:
			raise ValueError("universe read flags must be boolean")
		if self.as_of is not None:
			require_aware_datetime(self.as_of, "universe read cutoff")


@dataclass(frozen=True)
class UniverseSelectionRequest:
	membership_sessions: tuple[TradingSession, ...]
	dependency_sessions: tuple[TradingSession, ...]
	filter_sessions: tuple[TradingSession, ...] = ()
	filters: UniverseFilters = UniverseFilters()
	ordered_ticker_ids: tuple[int, ...] | None = None
	filter_cutoff_session: TradingSession | None = None
	as_of: datetime | None = None
	source_snapshot_id: str | None = None
	source_revision: str | None = None

	def __post_init__(self) -> None:
		membership = _sessions(self.membership_sessions, "membership sessions")
		dependencies = () if not self.dependency_sessions else _sessions(self.dependency_sessions, "dependency sessions")
		filter_sessions = () if not self.filter_sessions else _sessions(self.filter_sessions, "filter sessions")
		if not isinstance(self.filters, UniverseFilters):
			raise ValueError("universe selection requires typed filters")
		all_sessions = {*membership, *dependencies, *filter_sessions}
		if any(item.calendar_id != membership[0].calendar_id for item in all_sessions):
			raise ValueError("universe selection sessions must use one calendar")
		if self.ordered_ticker_ids is not None and (not isinstance(self.ordered_ticker_ids, tuple) or not self.ordered_ticker_ids or len(set(self.ordered_ticker_ids)) != len(self.ordered_ticker_ids) or any(type(item) is not int or item <= 0 for item in self.ordered_ticker_ids)):
			raise ValueError("ordered ticker override must contain unique positive IDs")
		if self.filter_cutoff_session is not None and self.filter_cutoff_session not in all_sessions:
			raise ValueError("filter cutoff must be included in the requested sessions")
		if self.filters.enabled and self.filter_cutoff_session is None:
			raise ValueError("enabled filters require a filter cutoff session")
		if not self.filters.enabled and filter_sessions:
			raise ValueError("disabled filters must not request optional dependency sessions")
		if self.as_of is not None:
			require_aware_datetime(self.as_of, "universe selection cutoff")
		for value, name in ((self.source_snapshot_id, "source snapshot ID"), (self.source_revision, "source revision")):
			if value is not None:
				require_text(value, name)
		object.__setattr__(self, "membership_sessions", membership)
		object.__setattr__(self, "dependency_sessions", dependencies)
		object.__setattr__(self, "filter_sessions", filter_sessions)

	@property
	def required_sessions(self) -> tuple[TradingSession, ...]:
		return tuple(sorted({*self.membership_sessions, *self.dependency_sessions, *self.filter_sessions}, key=lambda item: item.sequence))

	@property
	def selection_mode(self) -> str:
		return "explicit_subset" if self.ordered_ticker_ids is not None else "stable_id"

	@property
	def read_volume(self) -> bool:
		return self.filters.average_volume is not None

	@property
	def read_transactions(self) -> bool:
		return self.filters.legacy_transactions is not None

	def read_request(self) -> UniverseReadRequest:
		return UniverseReadRequest(self.required_sessions, self.ordered_ticker_ids, self.read_volume, self.read_transactions, self.as_of)

	@classmethod
	def from_dependency_plan(
		cls,
		plan: DependencyPlan,
		calendar: SessionCalendar,
		*,
		filters: UniverseFilters = UniverseFilters(),
		ordered_ticker_ids: tuple[int, ...] | None = None,
		as_of: datetime | None = None,
		source_snapshot_id: str | None = None,
		source_revision: str | None = None,
	) -> "UniverseSelectionRequest":
		if not isinstance(plan, DependencyPlan):
			raise ValueError("Universe selection requires a Stage 2 dependency plan")
		# SessionCalendar is a structural protocol and is intentionally not
		# runtime-checkable.  Validate the callable surface for fixture calendars.
		if not all(callable(getattr(calendar, name, None)) for name in ("session", "sessions", "shift")):
			raise ValueError("Universe selection requires a session calendar")
		membership = plan.training.entry_sessions
		dependencies = tuple(sorted(set((*plan.warmup_sessions, *plan.training_label_exit_sessions)), key=lambda item: item.sequence))
		filter_sessions: set[TradingSession] = set()
		cutoff = plan.training_information_cutoff
		if filters.average_volume is not None:
			start = calendar.shift(cutoff, -(filters.average_volume.window_sessions - 1))
			filter_sessions.update(calendar.sessions(start.session_date, cutoff.session_date))
		if filters.volatility is not None:
			start = calendar.shift(cutoff, -filters.volatility.window_sessions)
			filter_sessions.update(calendar.sessions(start.session_date, cutoff.session_date))
		if filters.legacy_transactions is not None:
			filter_sessions.add(cutoff)
		return cls(membership, dependencies, tuple(sorted(filter_sessions, key=lambda item: item.sequence)), filters, ordered_ticker_ids, cutoff if filters.enabled else None, as_of, source_snapshot_id, source_revision)

	def to_mapping(self) -> dict[str, object]:
		return {
			"schema": UNIVERSE_SCHEMA,
			"selection_mode": self.selection_mode,
			"membership_sessions": [_session_mapping(item) for item in self.membership_sessions],
			"dependency_sessions": [_session_mapping(item) for item in self.dependency_sessions],
			"filter_sessions": [_session_mapping(item) for item in self.filter_sessions],
			"required_sessions": [_session_mapping(item) for item in self.required_sessions],
			"ordered_ticker_ids": self.ordered_ticker_ids,
			"filter_cutoff_session": None if self.filter_cutoff_session is None else self.filter_cutoff_session.session_date.isoformat(),
			"filters": self.filters.to_mapping(),
			"as_of": None if self.as_of is None else self.as_of.isoformat(),
			"source_snapshot_id": self.source_snapshot_id,
			"source_revision": self.source_revision,
		}


def _session_mapping(session: TradingSession) -> dict[str, object]:
	return {"date": session.session_date.isoformat(), "sequence": session.sequence}


@dataclass(frozen=True)
class UniverseSourceData:
	tickers: tuple[TickerIdentity, ...]
	coverage: tuple[SourceSessionEvidence, ...]
	observations: tuple[UniverseObservation, ...]
	source_revision: str | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.tickers, tuple) or any(not isinstance(item, TickerIdentity) for item in self.tickers) or len({item.ticker_id for item in self.tickers}) != len(self.tickers):
			raise ValueError("Universe source tickers must be unique typed identities")
		if not isinstance(self.coverage, tuple) or any(not isinstance(item, SourceSessionEvidence) for item in self.coverage) or len({_session_key(item.session) for item in self.coverage}) != len(self.coverage):
			raise ValueError("Universe source coverage must be unique typed sessions")
		if not isinstance(self.observations, tuple) or any(not isinstance(item, UniverseObservation) for item in self.observations) or len({(item.ticker_id, _session_key(item.session)) for item in self.observations}) != len(self.observations):
			raise ValueError("Universe observations must have unique ticker/session keys")
		if self.source_revision is not None:
			require_text(self.source_revision, "universe source revision")

	@property
	def revision(self) -> str:
		if self.source_revision is not None:
			return self.source_revision
		return content_id({
			"tickers": [item.to_mapping() for item in self.tickers],
			"coverage": [item.to_mapping() for item in self.coverage],
			"observations": [_observation_mapping(item) for item in self.observations],
		})


class UniverseDataRepository(Protocol):
	def read(self, request: UniverseReadRequest) -> UniverseSourceData: ...


@dataclass(frozen=True)
class TickerExclusion:
	ticker_id: int
	label: str
	reasons: tuple[ExclusionReason, ...]
	details: tuple[str, ...]

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "excluded ticker ID")
		require_text(self.label, "excluded ticker label")
		if not self.reasons or len(self.reasons) != len(self.details) or any(not isinstance(item, ExclusionReason) for item in self.reasons) or any(not isinstance(item, str) or not item for item in self.details):
			raise ValueError("Ticker exclusions require aligned typed reasons and details")

	@property
	def reason(self) -> ExclusionReason:
		return self.reasons[0]

	def to_mapping(self) -> dict[str, object]:
		return {"ticker_id": self.ticker_id, "label": self.label, "reasons": [item.value for item in self.reasons], "details": list(self.details)}


@dataclass(frozen=True)
class FilterMeasurement:
	ticker_id: int
	filter_name: str
	window_sessions: int
	value: Decimal | None
	lower: Decimal | None
	upper: Decimal | None
	passed: bool

	def __post_init__(self) -> None:
		require_positive_int(self.ticker_id, "measurement ticker ID")
		require_text(self.filter_name, "measurement filter name")
		_positive_window(self.window_sessions, "measurement window")
		if self.value is not None:
			_decimal(self.value, "measurement value", nonnegative=True)
		if self.lower is None and self.upper is None:
			raise ValueError("Measurement requires a bound")
		if type(self.passed) is not bool:
			raise ValueError("Measurement passed must be boolean")

	def to_mapping(self) -> dict[str, object]:
		return {"ticker_id": self.ticker_id, "filter": self.filter_name, "window_sessions": self.window_sessions, "value": None if self.value is None else str(self.value), "lower": None if self.lower is None else str(self.lower), "upper": None if self.upper is None else str(self.upper), "passed": self.passed}


@dataclass(frozen=True)
class UniverseSelectionResult:
	request: UniverseSelectionRequest
	selected: tuple[TickerIdentity, ...]
	exclusions: tuple[TickerExclusion, ...]
	measurements: tuple[FilterMeasurement, ...]
	coverage: tuple[SourceSessionEvidence, ...]
	source_revision: str

	def __post_init__(self) -> None:
		if not isinstance(self.request, UniverseSelectionRequest) or not isinstance(self.selected, tuple) or any(not isinstance(item, TickerIdentity) for item in self.selected):
			raise ValueError("Invalid universe selection result")
		if len({item.ticker_id for item in self.selected}) != len(self.selected) or not isinstance(self.exclusions, tuple) or not isinstance(self.measurements, tuple) or not isinstance(self.coverage, tuple):
			raise ValueError("Universe selection result contains duplicate or invalid evidence")
		require_text(self.source_revision, "selection source revision")

	@property
	def ordered_ticker_ids(self) -> tuple[int, ...]:
		return tuple(item.ticker_id for item in self.selected)

	@property
	def ordered_ticker_labels(self) -> tuple[str, ...]:
		return tuple(item.label for item in self.selected)

	@property
	def selected_symbols(self) -> tuple[str, ...]:
		return self.ordered_ticker_labels

	def to_mapping(self) -> dict[str, object]:
		return {
			"schema": UNIVERSE_SCHEMA,
			"status": "selected" if self.selected else "empty",
			"request": self.request.to_mapping(),
			"source_revision": self.source_revision,
			"coverage": [item.to_mapping() for item in self.coverage],
			"ordered_ticker_ids": list(self.ordered_ticker_ids),
			"ordered_ticker_labels": list(self.ordered_ticker_labels),
			"selected": [item.to_mapping() for item in self.selected],
			"exclusions": [item.to_mapping() for item in self.exclusions],
			"filter_measurements": [item.to_mapping() for item in self.measurements],
		}


def _observation_mapping(item: UniverseObservation) -> dict[str, object]:
	return {
		"ticker_id": item.ticker_id,
		"session": _session_mapping(item.session),
		"state": item.state.value,
		"close": None if item.close is None else str(item.close),
		"volume": None if item.volume is None else str(item.volume),
		"transactions": item.transactions,
		"revision": item.revision,
	}


def _measurement_bounds(value: Decimal, lower: Decimal | None, upper: Decimal | None) -> tuple[bool, ExclusionReason | None]:
	if lower is not None and value < lower:
		return False, None
	if upper is not None and value > upper:
		return False, None
	return True, None


def _trailing(sessions: tuple[TradingSession, ...], cutoff: TradingSession, count: int) -> tuple[TradingSession, ...]:
	values = tuple(item for item in sessions if item.sequence <= cutoff.sequence)
	if len(values) < count:
		return ()
	return values[-count:]


def _average(values: tuple[Decimal, ...]) -> Decimal:
	with localcontext() as context:
		context.prec = 50
		return sum(values, Decimal(0)) / Decimal(len(values))


def _population_std(values: tuple[Decimal, ...]) -> Decimal:
	mean = _average(values)
	with localcontext() as context:
		context.prec = 50
		variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(len(values))
		return variance.sqrt()


def _add_exclusion(store: dict[int, list[tuple[ExclusionReason, str]]], ticker_id: int, reason: ExclusionReason, detail: str) -> None:
	entries = store.setdefault(ticker_id, [])
	if (reason, detail) not in entries:
		entries.append((reason, detail))


def _unknown_coverage(coverage: tuple[SourceSessionEvidence, ...]) -> tuple[SourceSessionEvidence, ...]:
	return tuple(item for item in coverage if item.state in {CoverageState.INCOMPLETE, CoverageState.INCONSISTENT, CoverageState.UNVERIFIED, CoverageState.NOT_YET_AVAILABLE})


def assess_universe(request: UniverseSelectionRequest, data: UniverseSourceData) -> UniverseSelectionResult:
	"""Assess all candidates and return complete evidence, including an empty result."""
	if not isinstance(request, UniverseSelectionRequest) or not isinstance(data, UniverseSourceData):
		raise ValueError("Universe assessment requires typed request and source data")
	by_coverage = {_session_key(item.session): item for item in data.coverage}
	missing_coverage = [session for session in request.required_sessions if _session_key(session) not in by_coverage]
	if missing_coverage:
		raise UniverseCoverageError(tuple(SourceSessionEvidence(session, CoverageState.UNVERIFIED, "No source completion evidence was returned") for session in missing_coverage))
	coverage = tuple(by_coverage[_session_key(session)] for session in request.required_sessions)
	unknown = _unknown_coverage(coverage)
	if unknown:
		raise UniverseCoverageError(unknown)
	identities = {item.ticker_id: item for item in data.tickers}
	if request.ordered_ticker_ids is not None and any(item not in identities for item in request.ordered_ticker_ids):
		missing = tuple(item for item in request.ordered_ticker_ids if item not in identities)
		raise UniverseSelectionError(f"Explicit ticker subset contains unknown stable IDs: {missing}")
	candidates = request.ordered_ticker_ids or tuple(sorted(identities))
	if not candidates:
		raise UniverseSelectionError("Automatic universe selection returned no ticker identities")
	by_observation = {(item.ticker_id, _session_key(item.session)): item for item in data.observations}
	exclusions: dict[int, list[tuple[ExclusionReason, str]]] = {}
	measurements: list[FilterMeasurement] = []
	missing_session_dates = tuple(item.session.session_date.isoformat() for item in coverage if item.state is CoverageState.MISSING)
	for ticker_id in candidates:
		identity = identities[ticker_id]
		for session in request.required_sessions:
			if _session_key(session) in {_session_key(item.session) for item in coverage if item.state is CoverageState.MISSING}:
				_add_exclusion(exclusions, ticker_id, ExclusionReason.MISSING_SESSION_COVERAGE, f"Verified source absence for required session {session.session_date.isoformat()}")
				continue
			observation = by_observation.get((ticker_id, _session_key(session)))
			if observation is None or observation.state is RawObservationState.MISSING:
				_add_exclusion(exclusions, ticker_id, ExclusionReason.MISSING_OBSERVATION, f"No valid raw observation for required session {session.session_date.isoformat()}")
			elif observation.state is RawObservationState.INVALID:
				_add_exclusion(exclusions, ticker_id, ExclusionReason.INVALID_OBSERVATION, f"Raw observation is invalid for required session {session.session_date.isoformat()}")
			elif observation.state is RawObservationState.UNVERIFIED:
				_add_exclusion(exclusions, ticker_id, ExclusionReason.UNVERIFIED_OBSERVATION, f"Raw observation is not verified for required session {session.session_date.isoformat()}")
			elif observation.close is None:
				_add_exclusion(exclusions, ticker_id, ExclusionReason.INVALID_OBSERVATION, f"Raw observation has no positive close for required session {session.session_date.isoformat()}")
		if request.filters.enabled and not exclusions.get(ticker_id):
			cutoff = request.filter_cutoff_session
			assert cutoff is not None
			if request.filters.average_volume is not None:
				volume_policy: AverageVolumeFilter = request.filters.average_volume
				tail = _trailing(request.filter_sessions, cutoff, volume_policy.window_sessions)
				values = tuple(by_observation.get((ticker_id, _session_key(item)), UniverseObservation(ticker_id, item, RawObservationState.MISSING)).volume for item in tail)
				if not tail or len(values) != volume_policy.window_sessions or any(value is None for value in values):
					_add_exclusion(exclusions, ticker_id, ExclusionReason.MISSING_VOLUME, f"Average-volume filter lacks {volume_policy.window_sessions} complete sessions ending {cutoff.session_date.isoformat()}")
					measurements.append(FilterMeasurement(ticker_id, "average_volume", volume_policy.window_sessions, None, volume_policy.lower, volume_policy.upper, False))
				else:
					average = _average(tuple(value for value in values if value is not None))
					passed, _ = _measurement_bounds(average, volume_policy.lower, volume_policy.upper)
					measurements.append(FilterMeasurement(ticker_id, "average_volume", volume_policy.window_sessions, average, volume_policy.lower, volume_policy.upper, passed))
					if not passed:
						_add_exclusion(exclusions, ticker_id, ExclusionReason.AVERAGE_VOLUME_BELOW if volume_policy.lower is not None and average < volume_policy.lower else ExclusionReason.AVERAGE_VOLUME_ABOVE, f"Average volume {average} is outside the inclusive configured bounds")
			if request.filters.volatility is not None:
				volatility_policy: VolatilityFilter = request.filters.volatility
				tail = _trailing(request.filter_sessions, cutoff, volatility_policy.window_sessions + 1)
				closes = tuple(by_observation.get((ticker_id, _session_key(item)), UniverseObservation(ticker_id, item, RawObservationState.MISSING)).close for item in tail)
				typed_closes = tuple(value for value in closes if value is not None)
				if len(tail) != volatility_policy.window_sessions + 1 or len(typed_closes) != len(closes):
					_add_exclusion(exclusions, ticker_id, ExclusionReason.MISSING_OBSERVATION, f"Volatility filter lacks {volatility_policy.window_sessions} predecessor-backed sessions ending {cutoff.session_date.isoformat()}")
					measurements.append(FilterMeasurement(ticker_id, "volatility", volatility_policy.window_sessions, None, volatility_policy.lower, volatility_policy.upper, False))
				else:
					returns = tuple(typed_closes[index] / typed_closes[index - 1] - Decimal(1) for index in range(1, len(typed_closes)))
					volatility = _population_std(returns)
					passed, _ = _measurement_bounds(volatility, volatility_policy.lower, volatility_policy.upper)
					measurements.append(FilterMeasurement(ticker_id, "volatility", volatility_policy.window_sessions, volatility, volatility_policy.lower, volatility_policy.upper, passed))
					if not passed:
						_add_exclusion(exclusions, ticker_id, ExclusionReason.VOLATILITY_BELOW if volatility_policy.lower is not None and volatility < volatility_policy.lower else ExclusionReason.VOLATILITY_ABOVE, f"Volatility {volatility} is outside the inclusive configured bounds")
			if request.filters.legacy_transactions is not None:
				transaction_policy: LegacyTransactionFilter = request.filters.legacy_transactions
				observation = by_observation.get((ticker_id, _session_key(cutoff)))
				if observation is None or observation.transactions is None:
					_add_exclusion(exclusions, ticker_id, ExclusionReason.MISSING_TRANSACTIONS, f"Legacy transaction count is unavailable at {cutoff.session_date.isoformat()}")
				else:
					passed = observation.transactions >= transaction_policy.minimum
					if not passed:
						_add_exclusion(exclusions, ticker_id, ExclusionReason.TRANSACTIONS_BELOW, f"Legacy transaction count {observation.transactions} is below {transaction_policy.minimum}")
	selected = tuple(identities[item] for item in candidates if item not in exclusions)
	excluded = tuple(TickerExclusion(ticker_id, identities[ticker_id].label, tuple(reason for reason, _ in values), tuple(detail for _, detail in values)) for ticker_id, values in sorted(exclusions.items()))
	return UniverseSelectionResult(request, selected, excluded, tuple(measurements), coverage, data.revision)


def select_universe(request: UniverseSelectionRequest, data: UniverseSourceData) -> UniverseSelectionResult:
	result = assess_universe(request, data)
	if not result.selected:
		raise EmptyUniverseError(result)
	return result


def persist_universe_evidence(run_service: object, run_id: str, result: UniverseSelectionResult) -> object:
	"""Write selection evidence through the Stage 1 run service only."""
	if not isinstance(result, UniverseSelectionResult):
		raise ValueError("Universe persistence requires a typed selection result")
	update = getattr(run_service, "update_sections", None)
	if not callable(update):
		raise ValueError("run service does not provide update_sections")
	return update(run_id, sections={"universe": result.to_mapping(), "results": {"exclusions": [item.to_mapping() for item in result.exclusions], "universe_selection": result.to_mapping()}})


def persist_universe_failure_evidence(run_service: object, run_id: str, request: UniverseSelectionRequest, failure: UniverseSelectionError) -> object:
	"""Persist blocked/empty selection evidence before a caller marks its run failed."""
	if not isinstance(request, UniverseSelectionRequest) or not isinstance(failure, UniverseSelectionError):
		raise ValueError("Universe failure persistence requires typed request and error")
	if isinstance(failure, EmptyUniverseError):
		payload = failure.result.to_mapping()
	else:
		payload = {"schema": UNIVERSE_SCHEMA, "status": "blocked", "request": request.to_mapping(), "failure": failure.to_mapping() if isinstance(failure, UniverseCoverageError) else {"code": "selection_failed", "message": str(failure)}}
	update = getattr(run_service, "update_sections", None)
	if not callable(update):
		raise ValueError("run service does not provide update_sections")
	return update(run_id, sections={"universe": payload, "results": {"exclusions": payload.get("exclusions", []), "universe_selection": payload}})


__all__ = [
	"UNIVERSE_SCHEMA", "AverageVolumeFilter", "EmptyUniverseError", "ExclusionReason", "FilterMeasurement", "LegacyTransactionFilter", "RawObservationState", "SourceSessionEvidence", "TickerExclusion", "TickerIdentity", "UniverseCoverageError", "UniverseDataRepository", "UniverseFilters", "UniverseObservation", "UniverseReadRequest", "UniverseSelectionError", "UniverseSelectionRequest", "UniverseSelectionResult", "UniverseSourceData", "VolatilityFilter", "assess_universe", "persist_universe_evidence", "persist_universe_failure_evidence", "select_universe",
]
