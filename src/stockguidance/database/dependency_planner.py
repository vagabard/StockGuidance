"""Pure dependency closure planning for the simplified database operation.

The planner consumes an immutable description of the database and produces the
complete, serializable pending scope.  It deliberately has no connection,
repository, provider, or filesystem dependency.  The snapshot is the evidence
read by a later coordinator before it registers the scope; once registered,
the scope is sufficient to clear and resume without reading deleted lineage.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_positive_int
from stockguidance.database.simplified_contract import (
	CacheInvalidation,
	ClearScope,
	OperationKind,
	PendingScope,
	UnitKind,
	WorkUnit,
)
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.features.specification import FeatureSpec
from stockguidance.targets.specification import HoldingHorizon, TargetSpec
from stockguidance.zscores.specification import ZScoreFamily
from stockguidance.zscores.storage import ZScoreSeriesDefinition


class PlanningError(ValueError):
	"""Raised when a planner input cannot identify a safe durable scope."""


def _utc(value: datetime, name: str) -> datetime:
	if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
		raise PlanningError(f"{name} must be timezone-aware UTC")
	return value.astimezone(timezone.utc)


def _dates(values: Iterable[date], name: str) -> tuple[date, ...]:
	result = tuple(values)
	if any(not isinstance(value, date) or isinstance(value, datetime) for value in result):
		raise PlanningError(f"{name} must contain dates")
	if len(set(result)) != len(result):
		raise PlanningError(f"{name} must not contain duplicate dates")
	return result


def _ids(values: Iterable[int], name: str) -> tuple[int, ...]:
	result = tuple(values)
	if any(type(value) is not int or value <= 0 for value in result):
		raise PlanningError(f"{name} must contain positive integer IDs")
	if len(set(result)) != len(result):
		raise PlanningError(f"{name} must not contain duplicate IDs")
	return result


@dataclass(frozen=True)
class UpdateRequest:
	"""The source-session change request handed to the pure planner."""

	source: SourceConfig
	sessions: tuple[TradingSession, ...]
	as_of: datetime
	operation_kind: OperationKind
	forced_sessions: frozenset[date] = frozenset()

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceConfig):
			raise PlanningError("source must be a SourceConfig")
		if not isinstance(self.sessions, tuple) or any(not isinstance(session, TradingSession) for session in self.sessions):
			raise PlanningError("sessions must contain TradingSession values")
		if not self.sessions:
			raise PlanningError("sessions must not be empty")
		calendar_id = self.source.calendar_id
		keys = {(session.calendar_id, session.sequence, session.session_date) for session in self.sessions}
		if len(keys) != len(self.sessions) or any(session.calendar_id != calendar_id for session in self.sessions):
			raise PlanningError("request sessions must use one authoritative calendar without duplicates")
		if not isinstance(self.operation_kind, OperationKind):
			raise PlanningError("operation_kind must be an OperationKind")
		if not isinstance(self.forced_sessions, frozenset):
			raise PlanningError("forced_sessions must be a frozenset")
		if any(not isinstance(value, date) or isinstance(value, datetime) for value in self.forced_sessions):
			raise PlanningError("forced_sessions must contain dates")
		if not self.forced_sessions.issubset({session.session_date for session in self.sessions}):
			raise PlanningError("forced_sessions must be a subset of request sessions")
		object.__setattr__(self, "as_of", _utc(self.as_of, "as_of"))


@dataclass(frozen=True)
class SnapshotSession:
	"""Database/session inventory needed by the closure calculation.

	``raw_ticker_ids`` and ``delta_ticker_ids`` are actual persisted keys.  The
	optional expected set is coverage evidence for an incomplete initial load;
	it is never used to create placeholders for a completed no-trading session.
	``repair_ticker_ids`` identifies exceptional metadata rows that already
	exist and therefore may safely be cleared.
	"""

	session: TradingSession
	date_id: int
	raw_complete: bool = False
	delta_complete: bool = False
	raw_ticker_ids: tuple[int, ...] = ()
	delta_ticker_ids: tuple[int, ...] = ()
	repair_ticker_ids: tuple[int, ...] = ()
	expected_ticker_ids: tuple[int, ...] = ()
	replacement_ticker_ids: tuple[int, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.session, TradingSession):
			raise PlanningError("snapshot session requires a TradingSession")
		require_positive_int(self.date_id, "date_id")
		for value, name in ((self.raw_complete, "raw_complete"), (self.delta_complete, "delta_complete")):
			if type(value) is not bool:
				raise PlanningError(f"{name} must be boolean")
		for values, name in (
			(self.raw_ticker_ids, "raw_ticker_ids"), (self.delta_ticker_ids, "delta_ticker_ids"),
			(self.repair_ticker_ids, "repair_ticker_ids"), (self.expected_ticker_ids, "expected_ticker_ids"),
			(self.replacement_ticker_ids, "replacement_ticker_ids"),
		):
			if not isinstance(values, tuple):
				raise PlanningError(f"{name} must be a tuple")
			_ids(values, name)

	@property
	def source_ticker_ids(self) -> frozenset[int]:
		if self.raw_complete:
			return frozenset(self.raw_ticker_ids)
		return frozenset(self.expected_ticker_ids or self.raw_ticker_ids)

	@property
	def known_ticker_ids(self) -> frozenset[int]:
		return frozenset(self.raw_ticker_ids) | frozenset(self.delta_ticker_ids) | frozenset(self.repair_ticker_ids) | frozenset(self.expected_ticker_ids) | frozenset(self.replacement_ticker_ids)


@dataclass(frozen=True)
class InterpolationInterval:
	"""One currently recorded bounded interpolation interval for one ticker."""

	ticker_id: int
	prior_date_id: int
	prior_date: date
	following_date_id: int
	following_date: date

	def __post_init__(self) -> None:
		for value, name in ((self.ticker_id, "ticker_id"), (self.prior_date_id, "prior_date_id"), (self.following_date_id, "following_date_id")):
			require_positive_int(value, name)
		_dates((self.prior_date, self.following_date), "interpolation endpoint dates")
		if self.prior_date >= self.following_date:
			raise PlanningError("interpolation endpoints must be ordered")


@dataclass(frozen=True)
class ZScoreSeriesSnapshot:
	"""One configured z-score series and its persisted completion evidence."""

	definition: ZScoreSeriesDefinition
	ticker_ids: tuple[int, ...] = ()
	completed_date_ids: frozenset[int] = frozenset()

	def __post_init__(self) -> None:
		if not isinstance(self.definition, ZScoreSeriesDefinition):
			raise PlanningError("z-score snapshot requires a series definition")
		_ids(self.ticker_ids, "z-score ticker_ids")
		if not isinstance(self.completed_date_ids, frozenset) or any(type(value) is not int or value <= 0 for value in self.completed_date_ids):
			raise PlanningError("completed_date_ids must contain positive integer IDs")

	@property
	def series_key(self) -> str:
		return self.definition.series_key


@dataclass(frozen=True)
class CacheEntry:
	"""A cache revision and the source dates it actually depends on."""

	session_date: date
	dependency_dates: tuple[date, ...]

	def __post_init__(self) -> None:
		_dates((self.session_date,), "cache session_date")
		_dates(self.dependency_dates, "cache dependency_dates")


@dataclass(frozen=True)
class CacheRevision:
	"""Persisted cache scope evidence supplied to the planner.

	When entries are available, only entries whose actual dependency dates
	overlap the closure are invalidated.  The fallback fields describe a whole
	revision when the cache has no per-entry dependency records.
	"""

	revision_scope: str
	session_dates: tuple[date, ...] = ()
	dependency_dates: tuple[date, ...] = ()
	entries: tuple[CacheEntry, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.revision_scope, str) or not self.revision_scope:
			raise PlanningError("cache revision_scope must be nonempty text")
		_dates(self.session_dates, "cache session_dates")
		_dates(self.dependency_dates, "cache dependency_dates")
		if not isinstance(self.entries, tuple) or any(not isinstance(entry, CacheEntry) for entry in self.entries):
			raise PlanningError("cache entries must contain CacheEntry values")
		if len({entry.session_date for entry in self.entries}) != len(self.entries):
			raise PlanningError("cache entries must not repeat session dates")


@dataclass(frozen=True)
class DatabaseSnapshot:
	"""Immutable database evidence used by :class:`DependencyPlanner`.

	The coordinator is responsible for filling this from one consistent read
	snapshot.  It must include all exchange sessions needed by configured
	lookback windows; the planner never guesses a calendar-day radius.
	"""

	sessions: tuple[SnapshotSession, ...]
	configuration_key: str | None = None
	calendar_id: str | None = None
	interpolation_intervals: tuple[InterpolationInterval, ...] = ()
	zscore_series: tuple[ZScoreSeriesSnapshot, ...] = ()
	cache_revisions: tuple[CacheRevision, ...] = ()
	feature_specs: tuple[FeatureSpec, ...] = ()
	target_specs: tuple[TargetSpec, ...] = ()
	normalization_warmup_sessions: int = 0

	def __post_init__(self) -> None:
		if not isinstance(self.sessions, tuple) or any(not isinstance(item, SnapshotSession) for item in self.sessions):
			raise PlanningError("snapshot sessions must contain SnapshotSession values")
		if not self.sessions:
			raise PlanningError("snapshot must contain session inventory")
		keys = {(item.session.calendar_id, item.session.sequence, item.session.session_date) for item in self.sessions}
		if len(keys) != len(self.sessions):
			raise PlanningError("snapshot sessions must be unique")
		calendar_ids = {item.session.calendar_id for item in self.sessions}
		if self.calendar_id is not None and (not isinstance(self.calendar_id, str) or self.calendar_id not in calendar_ids):
			raise PlanningError("snapshot calendar_id does not match session inventory")
		if self.configuration_key is not None and (not isinstance(self.configuration_key, str) or len(self.configuration_key) != 64 or any(character not in "0123456789abcdef" for character in self.configuration_key)):
			raise PlanningError("snapshot configuration_key must be lowercase SHA-256")
		for value, name in (
			(self.interpolation_intervals, "interpolation_intervals"), (self.zscore_series, "zscore_series"),
			(self.cache_revisions, "cache_revisions"),
		):
			if not isinstance(value, tuple):
				raise PlanningError(f"{name} must be a tuple")
		if any(not isinstance(value, InterpolationInterval) for value in self.interpolation_intervals):
			raise PlanningError("interpolation_intervals must contain InterpolationInterval values")
		if any(not isinstance(value, ZScoreSeriesSnapshot) for value in self.zscore_series):
			raise PlanningError("zscore_series must contain ZScoreSeriesSnapshot values")
		if len({value.series_key for value in self.zscore_series}) != len(self.zscore_series):
			raise PlanningError("snapshot z-score series must be unique")
		if any(not isinstance(value, CacheRevision) for value in self.cache_revisions):
			raise PlanningError("cache_revisions must contain CacheRevision values")
		if not isinstance(self.feature_specs, tuple) or any(not isinstance(value, FeatureSpec) for value in self.feature_specs):
			raise PlanningError("feature_specs must contain FeatureSpec values")
		if not isinstance(self.target_specs, tuple) or any(not isinstance(value, TargetSpec) for value in self.target_specs):
			raise PlanningError("target_specs must contain TargetSpec values")
		if type(self.normalization_warmup_sessions) is not int or self.normalization_warmup_sessions < 0:
			raise PlanningError("normalization_warmup_sessions must be a nonnegative integer")

	@property
	def by_date(self) -> Mapping[date, SnapshotSession]:
		return {item.session.session_date: item for item in self.sessions}

	@property
	def by_sequence(self) -> Mapping[int, SnapshotSession]:
		return {item.session.sequence: item for item in self.sessions}


class DependencyPlannerProtocol(Protocol):
	def plan(self, request: UpdateRequest, snapshot: DatabaseSnapshot) -> PendingScope: ...


_UNIT_ORDER = {
	UnitKind.RAW_SESSION.value: 0,
	UnitKind.REPAIR_ROW.value: 1,
	UnitKind.DELTA_SESSION.value: 2,
	UnitKind.ZSCORE_SESSION.value: 3,
	UnitKind.CACHE_REVISION.value: 4,
}


def _unit_key(unit: WorkUnit) -> tuple[object, ...]:
	return (
		_UNIT_ORDER[unit.unit_kind],
		-1 if unit.session_date is None else unit.session_date.toordinal(),
		-1 if unit.date_id is None else unit.date_id,
		-1 if unit.ticker_id is None else unit.ticker_id,
		"" if unit.series_key is None else unit.series_key,
		-1 if unit.window is None else unit.window,
	)


def _sorted_units(units: Iterable[WorkUnit]) -> tuple[WorkUnit, ...]:
	return tuple(sorted(set(units), key=_unit_key))


class DependencyPlanner:
	"""Calculate a deterministic transitive dependency closure in memory."""

	@staticmethod
	def _changed_tickers(item: SnapshotSession) -> set[int]:
		if item.replacement_ticker_ids:
			# A replacement can add keys and withdraw old keys; both sides are
			# dependencies that must be cleared without inventing other universe
			# members.
			return set(item.replacement_ticker_ids) | set(item.source_ticker_ids) | set(item.delta_ticker_ids) | set(item.repair_ticker_ids)
		return set(item.source_ticker_ids or item.known_ticker_ids)

	def plan(self, request: UpdateRequest, snapshot: DatabaseSnapshot) -> PendingScope:
		if not isinstance(request, UpdateRequest) or not isinstance(snapshot, DatabaseSnapshot):
			raise PlanningError("plan requires an UpdateRequest and DatabaseSnapshot")
		configuration_key = data_configuration_key(request.source.data_configuration)
		if snapshot.configuration_key is not None and snapshot.configuration_key != configuration_key:
			raise PlanningError("snapshot source configuration does not match request")
		if snapshot.calendar_id is not None and snapshot.calendar_id != request.source.calendar_id:
			raise PlanningError("snapshot calendar does not match request")

		by_date, by_sequence = snapshot.by_date, snapshot.by_sequence
		request_items: dict[date, SnapshotSession] = {}
		for session in request.sessions:
			item = by_date.get(session.session_date)
			if item is None or item.session.sequence != session.sequence or item.session.calendar_id != session.calendar_id:
				raise PlanningError(f"snapshot has no matching date-id for request session {session.session_date.isoformat()}")
			request_items[session.session_date] = item

		raw_changes = self._raw_changes(request, request_items)
		raw_units = {
		WorkUnit(UnitKind.RAW_SESSION.value, item.date_id, None, item.session.session_date)
		for item in raw_changes.values()
	}

		repair_keys = self._repair_closure(raw_changes, snapshot, by_sequence)
		repair_units = {WorkUnit(UnitKind.REPAIR_ROW.value, date_id, ticker_id, session_date) for date_id, ticker_id, session_date in repair_keys}

		delta_keys = self._delta_closure(request, request_items, raw_changes, snapshot, by_sequence)
		delta_units = {WorkUnit(UnitKind.DELTA_SESSION.value, date_id, ticker_id, session_date) for date_id, ticker_id, session_date in delta_keys}

		delta_completion_dates = {session_date for _, _, session_date in delta_keys}
		completion_units = {
			WorkUnit(UnitKind.RAW_SESSION.value, item.date_id, None, item.session.session_date)
			for item in raw_changes.values()
		}
		completion_units.update(
			WorkUnit(UnitKind.DELTA_SESSION.value, item.date_id, None, item.session.session_date)
			for item in by_date.values() if item.session.session_date in delta_completion_dates
		)

		zscore_keys = self._zscore_closure(raw_changes, delta_keys, request_items, snapshot, by_sequence)
		zscore_units = {
			WorkUnit(UnitKind.ZSCORE_SESSION.value, date_id, None, session_date, series_key, window)
			for series_key, window, date_id, session_date, _ in zscore_keys
		}
		completion_units.update(
			WorkUnit(UnitKind.ZSCORE_SESSION.value, date_id, None, session_date, series_key, window)
			for series_key, window, date_id, session_date, _ in zscore_keys
		)

		clear_raw = _sorted_units(raw_units)
		clear_repairs = _sorted_units(repair_units)
		clear_deltas = _sorted_units(delta_units)
		clear_zscores = _sorted_units(
			WorkUnit(UnitKind.ZSCORE_SESSION.value, date_id, ticker_id, session_date, series_key, window)
			for series_key, window, date_id, session_date, ticker_id in zscore_keys
		)
		clear_completion = _sorted_units(completion_units)
		cache_invalidations = self._cache_closure(raw_changes, repair_keys, delta_keys, zscore_keys, snapshot)

		return PendingScope(
			1,
			request.operation_kind,
			configuration_key,
			request.source.calendar_id,
			request.as_of,
			request.as_of,
			(),
			ClearScope(clear_raw, clear_repairs, clear_deltas, clear_zscores, clear_completion),
			_sorted_units((*raw_units, *repair_units, *delta_units, *zscore_units)),
			cache_invalidations,
		)

	@staticmethod
	def _raw_changes(request: UpdateRequest, request_items: Mapping[date, SnapshotSession]) -> dict[date, SnapshotSession]:
		if request.operation_kind is OperationKind.DERIVED_REPAIR:
			return {}
		forced = request.forced_sessions or ({session.session_date for session in request.sessions} if request.operation_kind is OperationKind.FORCED_REPLACEMENT else frozenset())
		return {
			date_value: item
			for date_value, item in request_items.items()
			if request.operation_kind is OperationKind.INITIAL_IMPORT and not item.raw_complete
			or request.operation_kind is OperationKind.FORCED_REPLACEMENT and date_value in forced
		}

	@staticmethod
	def _repair_closure(
		raw_changes: Mapping[date, SnapshotSession], snapshot: DatabaseSnapshot, by_sequence: Mapping[int, SnapshotSession],
	) -> set[tuple[int, int, date]]:
		keys: set[tuple[int, int, date]] = set()
		changed_by_ticker: dict[int, set[int]] = {}
		for item in raw_changes.values():
			for ticker_id in DependencyPlanner._changed_tickers(item):
				changed_by_ticker.setdefault(ticker_id, set()).add(item.session.sequence)
			for ticker_id in item.repair_ticker_ids:
				keys.add((item.date_id, ticker_id, item.session.session_date))

		for interval in snapshot.interpolation_intervals:
			changed_sequences = changed_by_ticker.get(interval.ticker_id, set())
			if not any(interval.prior_date <= item.session.session_date <= interval.following_date and item.session.sequence in changed_sequences for item in raw_changes.values()):
				continue
			prior = next((item for item in by_sequence.values() if item.session.session_date == interval.prior_date), None)
			following = next((item for item in by_sequence.values() if item.session.session_date == interval.following_date), None)
			if prior is None or following is None:
				raise PlanningError("interpolation endpoints are absent from the snapshot")
			for sequence in range(prior.session.sequence + 1, following.session.sequence):
				candidate = by_sequence.get(sequence)
				if candidate is None or interval.ticker_id not in candidate.source_ticker_ids:
					continue
				if candidate.session.session_date in (interval.prior_date, interval.following_date):
					continue
				keys.add((candidate.date_id, interval.ticker_id, candidate.session.session_date))

		return keys

	@staticmethod
	def _candidate_tickers(item: SnapshotSession, changed: set[int] | None = None) -> set[int]:
		result = set(item.source_ticker_ids) | set(item.delta_ticker_ids)
		if changed:
			result.update(ticker_id for ticker_id in changed if ticker_id in item.known_ticker_ids or ticker_id in item.source_ticker_ids)
		return result

	@classmethod
	def _delta_closure(
		cls,
		request: UpdateRequest,
		request_items: Mapping[date, SnapshotSession],
		raw_changes: Mapping[date, SnapshotSession],
		snapshot: DatabaseSnapshot,
		by_sequence: Mapping[int, SnapshotSession],
	) -> set[tuple[int, int, date]]:
		keys: set[tuple[int, int, date]] = set()

		for item in raw_changes.values():
			changed = cls._changed_tickers(item)
			if not changed:
				continue
			for sequence in (item.session.sequence - 1, item.session.sequence, item.session.sequence + 1):
				candidate = by_sequence.get(sequence)
				if candidate is None:
					continue
				candidate_tickers = cls._candidate_tickers(candidate, changed)
				if candidate.session.sequence == item.session.sequence:
					candidate_tickers.update(changed)
				for ticker_id in candidate_tickers:
					keys.add((candidate.date_id, ticker_id, candidate.session.session_date))
			# The ranking at follower d is an input to every prior row.  Invalidate
			# the whole actual prior-session population when its boundary may move.
			prior = by_sequence.get(item.session.sequence - 1)
			if prior is not None:
				for ticker_id in cls._candidate_tickers(prior):
					keys.add((prior.date_id, ticker_id, prior.session.session_date))

		for item in request_items.values():
			if item.delta_complete:
				missing = set(item.source_ticker_ids) - set(item.delta_ticker_ids)
			else:
				missing = set(item.source_ticker_ids)
			if request.operation_kind is OperationKind.DERIVED_REPAIR and not item.delta_complete:
				missing |= set(item.known_ticker_ids)
			for ticker_id in missing:
				keys.add((item.date_id, ticker_id, item.session.session_date))
		return keys

	@staticmethod
	def _series_tickers(series: ZScoreSeriesSnapshot, item: SnapshotSession, impacted: set[int]) -> set[int]:
		available = set(series.ticker_ids) if series.ticker_ids else set(item.source_ticker_ids or item.known_ticker_ids)
		return available & impacted if impacted else set()

	@classmethod
	def _zscore_closure(
		cls,
		raw_changes: Mapping[date, SnapshotSession],
		delta_keys: set[tuple[int, int, date]],
		request_items: Mapping[date, SnapshotSession],
		snapshot: DatabaseSnapshot,
		by_sequence: Mapping[int, SnapshotSession],
	) -> set[tuple[str, int, int, date, int]]:
		keys: set[tuple[str, int, int, date, int]] = set()
		changed_by_ticker: dict[int, set[int]] = {}
		for item in raw_changes.values():
			for ticker_id in cls._changed_tickers(item):
				changed_by_ticker.setdefault(ticker_id, set()).add(item.session.sequence)
		# A legacy delta row can change because of a cross-sectional label/rank
		# boundary.  That is not a raw close dependency.  Only derived-only
		# repairs without a raw change introduce delta evidence independently.
		if not raw_changes:
			for date_id, ticker_id, session_date in delta_keys:
				candidate = next((value for value in by_sequence.values() if value.date_id == date_id), None)
				if candidate is not None:
					changed_by_ticker.setdefault(ticker_id, set()).add(candidate.session.sequence)

		for series in snapshot.zscore_series:
			if not raw_changes and series.definition.family is not ZScoreFamily.PRICE_DELTA:
				continue
			window = series.definition.window
			for ticker_id, changed_sequences in changed_by_ticker.items():
				if series.ticker_ids and ticker_id not in series.ticker_ids:
					continue
				for changed_sequence in changed_sequences:
					last = changed_sequence + (window if series.definition.family is ZScoreFamily.PRICE_DELTA else window - 1)
					for sequence in range(changed_sequence, last + 1):
						candidate = by_sequence.get(sequence)
						if candidate is not None:
							keys.add((series.series_key, window, candidate.date_id, candidate.session.session_date, ticker_id))

		# A derived-only request can repair a missing z-score completion unit even
		# when no raw revision is present.  The requested session is the exact
		# target; no later history is guessed.
		for series in snapshot.zscore_series:
			for item in request_items.values():
				if item.date_id in series.completed_date_ids:
					continue
				for ticker_id in cls._series_tickers(series, item, set(item.source_ticker_ids or item.known_ticker_ids)):
					keys.add((series.series_key, series.definition.window, item.date_id, item.session.session_date, ticker_id))
		return keys

	@staticmethod
	def _cache_closure(
		raw_changes: Mapping[date, SnapshotSession],
		repair_keys: set[tuple[int, int, date]],
		delta_keys: set[tuple[int, int, date]],
		zscore_keys: set[tuple[str, int, int, date, int]],
		snapshot: DatabaseSnapshot,
	) -> tuple[CacheInvalidation, ...]:
		affected_dates = set(raw_changes)
		affected_dates.update(session_date for _, _, session_date in repair_keys)
		affected_dates.update(session_date for _, _, session_date in delta_keys)
		affected_dates.update(session_date for _, _, _, session_date, _ in zscore_keys)
		cache_dates = set(affected_dates)
		lookback = max((spec.lookback_sessions for spec in snapshot.feature_specs), default=0) + snapshot.normalization_warmup_sessions
		label_horizon = max((1 if spec.horizon is HoldingHorizon.NEXT_TRADING_SESSION else 0 for spec in snapshot.target_specs), default=0)
		if lookback or label_horizon:
			sequence_by_date = {item.session.session_date: item.session.sequence for item in snapshot.sessions}
			date_by_sequence = {item.session.sequence: item.session.session_date for item in snapshot.sessions}
			for changed_date in tuple(affected_dates):
				sequence = sequence_by_date.get(changed_date)
				if sequence is None:
					continue
				for candidate in range(sequence - label_horizon, sequence + lookback + 1):
					if candidate in date_by_sequence:
						cache_dates.add(date_by_sequence[candidate])
		result: list[CacheInvalidation] = []
		for revision in sorted(snapshot.cache_revisions, key=lambda item: item.revision_scope):
			if revision.entries:
				dates = tuple(sorted(entry.session_date for entry in revision.entries if set(entry.dependency_dates) & affected_dates))
			elif set(revision.dependency_dates) & affected_dates:
				dates = tuple(sorted(set(revision.session_dates) & cache_dates))
			else:
				dates = tuple(sorted(set(revision.session_dates) & cache_dates))
			if dates:
				result.append(CacheInvalidation(revision.revision_scope, dates))
		return tuple(result)


__all__ = [
	"CacheEntry", "CacheRevision", "DatabaseSnapshot", "DependencyPlanner", "DependencyPlannerProtocol",
	"InterpolationInterval", "PlanningError", "SnapshotSession", "UpdateRequest", "ZScoreSeriesSnapshot",
]
