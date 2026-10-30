"""Stage 9 readiness and read-only target-schema repositories.

The training, prediction, and historical-evaluation frontends all use this
module when the versioned replacement schema is present.  It is deliberately
the only layer that may request missing target coverage.  The close and
liquidity repositories below only read the database and never initialize,
repair, or refresh it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import ContextManager

from stockguidance.calendar.clock import Clock
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.database.operation_recovery import (
	DatabaseReadinessGuard,
	ReadScope,
	ReadinessState,
	consistent_read_transaction,
)
from stockguidance.database.repositories.closes import CloseData, CloseQuery, CloseRepository
from stockguidance.database.repositories.eligibility import EligibilityRepository
from stockguidance.database.repositories.validation import DatabaseInspection
from stockguidance.database.repositories.simplified import SourceConfigurationRepository
from stockguidance.database.simplified_contract import DATA_STATE_BY_ID, DataState
from stockguidance.database.simplified_runtime import (
	SimplifiedValidationResult,
	SimplifiedUpdateResult,
	run_simplified_update,
	validate_simplified_database,
)
from stockguidance.database.raw_rebuild import RawPayloadProvider
from stockguidance.database.simplified_schema import require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.features.specification import FeatureSpec, FeatureSpecV2, FeatureSpecification
from stockguidance.market_data.availability import AvailabilityPolicy, ObservationTiming
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.quality import QualityFlag
from stockguidance.market_data.closes import CloseIssue, CloseObservation, PriceState
from stockguidance.recommendations.eligibility import LiquidityObservation
from stockguidance.targets.specification import TargetSpec


class SimplifiedCoverageError(RuntimeError):
	"""A structured target-coverage or pending-operation failure."""

	def __init__(self, phase: str, reason: str, *, session_date: date | None = None, ticker_id: int | None = None) -> None:
		if not phase or not reason:
			raise ValueError("Coverage failures require a phase and reason")
		self.phase = phase
		self.reason = reason
		self.session_date = session_date
		self.ticker_id = ticker_id
		detail = f"{phase}: {reason}"
		if session_date is not None:
			detail += f" ({session_date.isoformat()})"
		if ticker_id is not None:
			detail += f" ticker_id={ticker_id}"
		super().__init__(detail)


def _placeholders(values: Sequence[object]) -> str:
	return ",".join("%s" for _ in values)


def _parse_date(value: object) -> date:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	if isinstance(value, bytes):
		return date.fromisoformat(value.decode("ascii"))
	if isinstance(value, str):
		return date.fromisoformat(value[:10])
	raise ValueError("Target database returned an invalid date")


def _integer(value: object, name: str) -> int:
	if isinstance(value, bool):
		raise ValueError(f"Target database returned an invalid {name}")
	if isinstance(value, int):
		result = value
	elif isinstance(value, bytes) and value.decode("ascii").isdigit():
		result = int(value.decode("ascii"))
	elif isinstance(value, str) and value.isdigit():
		result = int(value)
	else:
		raise ValueError(f"Target database returned an invalid {name}")
	if result <= 0:
		raise ValueError(f"Target database returned an invalid {name}")
	return result


def _text(value: object, name: str) -> str:
	if isinstance(value, bytes):
		value = value.decode("utf-8")
	if not isinstance(value, str) or not value:
		raise ValueError(f"Target database returned an invalid {name}")
	return value


def _boolean(value: object, name: str) -> bool:
	if value in (0, False):
		return False
	if value in (1, True):
		return True
	raise ValueError(f"Target database returned an invalid {name}")


def _timestamp(value: object, name: str, *, optional: bool = False) -> datetime | None:
	if value is None and optional:
		return None
	if isinstance(value, datetime):
		return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
	if isinstance(value, bytes):
		value = value.decode("ascii")
	if isinstance(value, str):
		try:
			parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
		except ValueError:
			raise ValueError(f"Target database returned an invalid {name}") from None
		return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
	raise ValueError(f"Target database returned an invalid {name}")


def _close_revision(raw_revision: object, repair_revision: str | None) -> str:
	if repair_revision is not None:
		return repair_revision
	return _text(raw_revision, "raw revision")


def _quality_for_repair(state: DataState) -> tuple[QualityFlag, int]:
	if state is not DataState.INTERPOLATED:
		return QualityFlag.CLEAN, 0
	# The target schema stores endpoint IDs and effective values, not a second
	# quality graph.  A one-session interpolation is the conservative default;
	# the provenance/revision still makes the source change visible to caches.
	return QualityFlag.BAD | QualityFlag.INTERPOLATED_SINGLE, 1


@dataclass(frozen=True)
class RequiredSessionScope:
	sessions: tuple[TradingSession, ...]
	feature_sessions: tuple[TradingSession, ...]
	label_sessions: tuple[TradingSession, ...]
	normalization_warmup: int
	feature_dependency_sessions: tuple[TradingSession, ...] = ()

	def __post_init__(self) -> None:
		for name, values in (("sessions", self.sessions), ("feature_sessions", self.feature_sessions), ("label_sessions", self.label_sessions), ("feature_dependency_sessions", self.feature_dependency_sessions)):
			if not isinstance(values, tuple) or any(not isinstance(value, TradingSession) for value in values):
				raise ValueError(f"{name} must contain trading sessions")
		if type(self.normalization_warmup) is not int or self.normalization_warmup < 0:
			raise ValueError("normalization_warmup must be nonnegative")


def required_session_scope(
	calendar: SessionCalendar,
	decisions: tuple[DecisionTiming, ...],
	features: FeatureSpecification,
	_target: TargetSpec,
	*,
	include_labels: bool,
	normalization_warmup: int = 0,
	extra_sessions: tuple[TradingSession, ...] = (),
) -> RequiredSessionScope:
	"""Expand feature, normalization, label, and explicit execution dependencies.

	The result uses exchange-session adjacency.  It never creates rows for a
	ticker/date Cartesian product; ticker coverage is checked by the read path.
	"""
	if not isinstance(decisions, tuple) or not decisions or any(not isinstance(item, DecisionTiming) for item in decisions):
		raise ValueError("Readiness requires explicit chronological decisions")
	if any(right.entry_session.sequence <= left.entry_session.sequence for left, right in zip(decisions, decisions[1:])):
		raise ValueError("Readiness decisions must be chronological")
	if not isinstance(features, (FeatureSpec, FeatureSpecV2)) or not isinstance(_target, TargetSpec):
		raise ValueError("Readiness requires current feature and target specifications")
	if type(normalization_warmup) is not int or normalization_warmup < 0:
		raise ValueError("normalization_warmup must be nonnegative")
	if any(not isinstance(session, TradingSession) for session in extra_sessions):
		raise ValueError("extra_sessions must contain trading sessions")

	feature: set[TradingSession] = set()
	feature_dependencies: set[TradingSession] = set()
	labels: set[TradingSession] = set()
	for timing in decisions:
		history_length = features.required_history_sessions if isinstance(features, FeatureSpecV2) else features.lookback_sessions
		window = tuple(calendar.shift(timing.entry_session, offset) for offset in range(-features.lookback_sessions, 0))
		feature.update(window)
		feature_dependencies.update(calendar.shift(timing.entry_session, offset) for offset in range(-history_length, 0))
		if include_labels:
			labels.update((timing.entry_session, timing.exit_session))
	feature.update(extra_sessions)
	all_required = set(feature) | feature_dependencies | labels
	if not all_required:
		raise ValueError("Readiness produced an empty session scope")
	first = min(all_required, key=lambda item: item.sequence)
	for offset in range(-normalization_warmup, 0):
		all_required.add(calendar.shift(first, offset))
	ordered = tuple(sorted(all_required, key=lambda item: item.sequence))
	return RequiredSessionScope(ordered, tuple(sorted(feature, key=lambda item: item.sequence)), tuple(sorted(labels, key=lambda item: item.sequence)), normalization_warmup, tuple(sorted(feature_dependencies, key=lambda item: item.sequence)))


def boundary_for_scope(source: SourceConfig, calendar: SessionCalendar, scope: RequiredSessionScope, established_at: datetime) -> HistoryBoundary:
	if not scope.sessions:
		raise ValueError("Cannot establish a database history boundary for an empty scope")
	return HistoryBoundary(source.identity, calendar.calendar_id, scope.sessions[0].session_date, HistoryProvenance.SOURCE_HISTORY, "Versioned database session completion", established_at.astimezone(timezone.utc))


@dataclass(frozen=True)
class SimplifiedReadinessResult:
	source: SourceConfig
	boundary: HistoryBoundary
	scope: RequiredSessionScope
	update: SimplifiedUpdateResult
	validation: SimplifiedValidationResult
	data_revision: str

	@property
	def successful(self) -> bool:
		return self.update.successful


class SimplifiedCloseRepository(CloseRepository):
	"""Read target raw closes under the pending-operation guard."""

	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None) -> None:
		self.source = source
		self.connect = connect
		self.table_names = table_names or load_table_names()

	def read(self, query: CloseQuery) -> CloseData:
		if query.scope.boundary.source != self.source.identity:
			raise ValueError("Simplified close repository and query sources differ")
		dates = tuple(session.session_date for session in query.scope.sessions)
		date_parameters = tuple(dates)
		with consistent_read_transaction(self.source, self.connect) as transaction:
			cursor = transaction.cursor()
			require_simplified_schema(cursor, self.table_names)
			sessions_table = quote_identifier(self.table_names.table(TableRole.SESSIONS))
			cursor.execute(
				f"SELECT id, transaction_date, raw_complete, raw_available_at, raw_revision FROM {sessions_table} WHERE transaction_date IN ({_placeholders(dates)}) ORDER BY transaction_date",
				date_parameters,
			)
			rows = cursor.fetchall()
			by_date: dict[date, tuple[int, bool, datetime | None, str | None]] = {}
			for row in rows:
				if len(row) != 5:
					raise SimplifiedCoverageError("raw", "Target session row has an invalid layout")
				day = _parse_date(row[1])
				by_date[day] = (_integer(row[0], "date_id"), _boolean(row[2], "raw_complete"), _timestamp(row[3], "raw_available_at", optional=True), None if row[4] is None else _text(row[4], "raw revision"))
			if len(by_date) != len(dates):
				missing = next(day for day in dates if day not in by_date)
				raise SimplifiedCoverageError("raw", "Target session is absent", session_date=missing)
			if any(not value[1] for value in by_date.values()):
				day = next(day for day, value in by_date.items() if not value[1])
				raise SimplifiedCoverageError("raw", "Raw session completion is not committed", session_date=day)
			ids = tuple(value[0] for value in by_date.values())
			readiness = DatabaseReadinessGuard(self.table_names).check(ReadScope(date_ids=ids), transaction)
			if readiness.status is ReadinessState.UNFINISHED_OPERATION:
				overlap_date = next((day for day, value in by_date.items() if readiness.date_id is not None and value[0] == readiness.date_id), None)
				raise SimplifiedCoverageError(readiness.phase or "operation", readiness.reason or "An unfinished operation overlaps this read", session_date=overlap_date, ticker_id=readiness.ticker_id)
			prices_table = quote_identifier(self.table_names.table(TableRole.PRICES))
			parameters = ids + tuple(query.ticker_ids)
			cursor.execute(
				f"SELECT date_id, ticker_id, close, state_id, observed_at FROM {prices_table} WHERE date_id IN ({_placeholders(ids)}) AND ticker_id IN ({_placeholders(query.ticker_ids)}) ORDER BY date_id, ticker_id",
				parameters,
			)
			price_rows = cursor.fetchall()
			repair_table = quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))
			cursor.execute(
				f"SELECT id, date_id, ticker_id, revision, state_id, available_at, recorded_at FROM {repair_table} WHERE date_id IN ({_placeholders(ids)}) AND ticker_id IN ({_placeholders(query.ticker_ids)}) ORDER BY recorded_at DESC, id DESC",
				parameters,
			)
			repair: dict[tuple[int, int], tuple[str, DataState, datetime | None]] = {}
			for row in cursor.fetchall():
				if len(row) != 7:
					raise SimplifiedCoverageError("repair", "Repair metadata row has an invalid layout")
				key = (_integer(row[1], "repair date_id"), _integer(row[2], "repair ticker_id"))
				if key not in repair:
					repair[key] = (_text(row[3], "repair revision"), DATA_STATE_BY_ID.get(_integer(row[4], "repair state"), DataState.BAD), _timestamp(row[5], "repair available_at", optional=True))

		observations = []
		issues: list[CloseIssue] = []
		for session in query.scope.sessions:
			date_id, _complete, raw_available, raw_revision = by_date[session.session_date]
			for ticker_id in query.ticker_ids:
				price_row: Sequence[object] | None = None
				for candidate in price_rows:
					if _integer(candidate[0], "price date_id") == date_id and _integer(candidate[1], "price ticker_id") == ticker_id:
						price_row = candidate
						break
				if price_row is None:
					issues.append(CloseIssue(ticker_id, session, PriceState.EXPECTED_ABSENCE, "Ticker is absent from the completed source response"))
					continue
				state = DATA_STATE_BY_ID.get(_integer(price_row[3], "price state"), DataState.BAD)
				repair_row = repair.get((date_id, ticker_id))
				quality, gap = _quality_for_repair(state)
				available_at = raw_available
				revision = raw_revision
				if state is DataState.INTERPOLATED:
					if repair_row is None or repair_row[1] is not DataState.INTERPOLATED:
						issues.append(CloseIssue(ticker_id, session, PriceState.INVALID, "Interpolated source state has no matching repair metadata"))
						continue
					revision, _repair_state, repair_available = repair_row
					available_at = repair_available or raw_available
				if state is DataState.BAD:
					issues.append(CloseIssue(ticker_id, session, PriceState.INVALID, "Source observation is explicitly invalid", quality, gap, () if revision is None else (revision,)))
					continue
				if state is DataState.MISSING:
					issues.append(CloseIssue(ticker_id, session, PriceState.MISSING, "Source observation is explicitly known missing", quality, gap, () if revision is None else (revision,)))
					continue
				if available_at is None or revision is None:
					issues.append(CloseIssue(ticker_id, session, PriceState.UNVERIFIED, "Completed source observation lacks availability or revision evidence"))
					continue
				assert available_at is not None
				if available_at > query.scope.as_of:
					issues.append(CloseIssue(ticker_id, session, PriceState.NOT_YET_AVAILABLE, "Source observation was committed after the query cutoff", quality, gap, (revision,)))
					continue
				try:
					close = float(Decimal(str(price_row[2])))
				except Exception:
					close = 0.0
				if not close > 0:
					issues.append(CloseIssue(ticker_id, session, PriceState.INVALID, "Completed source close is not a positive finite value", quality, gap, (revision,)))
					continue
				if not query.quality_policy.accepts(quality):
					issues.append(CloseIssue(ticker_id, session, PriceState.INVALID, "Interpolated or bad observation is excluded by the selected quality policy", quality, gap, (revision,)))
					continue
				observations.append(CloseObservation(ticker_id, close, ObservationTiming(session, session.closes_at, available_at), _close_revision(raw_revision, revision), quality, gap, (revision,) if revision is not None else ()))
		return CloseData(query, tuple(observations), tuple(issues))

	def inspect(self, query: CloseQuery) -> tuple[CloseData, DatabaseInspection]:
		"""Provide only the symbol mapping needed by entry-open orchestration."""
		data = self.read(query)
		with consistent_read_transaction(self.source, self.connect) as transaction:
			cursor = transaction.cursor()
			require_simplified_schema(cursor, self.table_names)
			tickers = quote_identifier(self.table_names.table(TableRole.TICKERS))
			cursor.execute(f"SELECT id, label FROM {tickers} WHERE id IN ({_placeholders(query.ticker_ids)}) ORDER BY id", query.ticker_ids)
			mapping = tuple((_integer(row[0], "ticker_id"), _text(row[1], "ticker label")) for row in cursor.fetchall() if len(row) == 2)
			return data, DatabaseInspection((), ticker_symbols=mapping)


class SimplifiedEligibilityRepository(EligibilityRepository):
	def __init__(self, closes: SimplifiedCloseRepository) -> None:
		self.closes = closes

	def read(self, query: CloseQuery) -> tuple[LiquidityObservation, ...]:
		if len(query.scope.sessions) != 1:
			raise ValueError("Liquidity must use exactly one previous session")
		data = self.closes.read(query)
		by_key = {(item.timing.session, item.ticker_id): item for item in data.observations}
		session = query.scope.sessions[0]
		with consistent_read_transaction(self.closes.source, self.closes.connect) as transaction:
			cursor = transaction.cursor()
			require_simplified_schema(cursor, self.closes.table_names)
			sessions_table = quote_identifier(self.closes.table_names.table(TableRole.SESSIONS))
			prices_table = quote_identifier(self.closes.table_names.table(TableRole.PRICES))
			cursor.execute(f"SELECT id FROM {sessions_table} WHERE transaction_date = %s", (session.session_date,))
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise SimplifiedCoverageError("liquidity", "Previous session is absent", session_date=session.session_date)
			date_id = _integer(rows[0][0], "liquidity date_id")
			readiness = DatabaseReadinessGuard(self.closes.table_names).check(ReadScope(date_ids=(date_id,)), transaction)
			if readiness.status is ReadinessState.UNFINISHED_OPERATION:
				raise SimplifiedCoverageError(readiness.phase or "operation", readiness.reason or "An unfinished operation overlaps this read", session_date=session.session_date, ticker_id=readiness.ticker_id)
			cursor.execute(
				f"SELECT ticker_id, transactions FROM {prices_table} WHERE date_id = %s AND ticker_id IN ({_placeholders(query.ticker_ids)})",
				(date_id, *query.ticker_ids),
			)
			transactions = {_integer(row[0], "liquidity ticker_id"): row[1] for row in cursor.fetchall()}
		result: list[LiquidityObservation] = []
		for ticker_id in query.ticker_ids:
			observation = by_key.get((session, ticker_id))
			value = transactions.get(ticker_id)
			if observation is not None and value is not None:
				try:
					count = _integer(value, "liquidity transaction count")
				except ValueError:
					continue
				if count >= 0:
					result.append(LiquidityObservation(ticker_id, count, observation.timing, observation.revision))
		return tuple(result)


def database_revision(source: SourceConfig, connect: WriteConnectionFactory, sessions: tuple[TradingSession, ...], table_names: TableNames | None = None) -> str:
	names = table_names or load_table_names()
	if not sessions:
		raise ValueError("Revision scope cannot be empty")
	with consistent_read_transaction(source, connect) as transaction:
		cursor = transaction.cursor()
		require_simplified_schema(cursor, names)
		session_table = quote_identifier(names.table(TableRole.SESSIONS))
		dates = tuple(item.session_date for item in sessions)
		cursor.execute(f"SELECT transaction_date, raw_revision, raw_complete, delta_complete FROM {session_table} WHERE transaction_date IN ({_placeholders(dates)}) ORDER BY transaction_date", dates)
		rows = cursor.fetchall()
		return content_id([(str(row[0]), None if row[1] is None else str(row[1]), _boolean(row[2], "raw_complete"), _boolean(row[3], "delta_complete")) for row in rows])


class SimplifiedReadinessWorkflow:
	"""Planner-owned writes followed by a read-only, completion-guarded pass."""

	def __init__(
		self,
		source: SourceConfig,
		connect: WriteConnectionFactory,
		calendar: SessionCalendar,
		publication: DailyPublicationPolicy,
		provider: RawPayloadProvider | None,
		clock: Clock,
		*,
		table_names: TableNames | None = None,
		staging_root: Path | None = None,
		lock_factory: Callable[[], ContextManager[object]] | None = None,
	) -> None:
		self.source, self.connect, self.calendar = source, connect, calendar
		self.publication, self.provider, self.clock = publication, provider, clock
		self.table_names = table_names or load_table_names()
		self.staging_root = staging_root
		self.lock_factory = lock_factory

	def ensure(
		self,
		decisions: tuple[DecisionTiming, ...],
		features: FeatureSpecification,
		target: TargetSpec,
		*,
		label_cutoff: datetime | None = None,
		as_of: datetime | None = None,
		availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT,
		data_snapshot: datetime | None = None,
		normalization_warmup: int = 0,
		extra_sessions: tuple[TradingSession, ...] = (),
	) -> SimplifiedReadinessResult:
		if not isinstance(availability_policy, AvailabilityPolicy):
			raise ValueError("Readiness requires a typed availability policy")
		if label_cutoff is not None and label_cutoff.tzinfo is None:
			raise ValueError("Readiness label cutoff must be timezone aware")
		include_labels = label_cutoff is not None
		try:
			scope = required_session_scope(self.calendar, decisions, features, target, include_labels=include_labels, normalization_warmup=normalization_warmup, extra_sessions=extra_sessions)
		except (IndexError, KeyError) as error:
			raise SimplifiedCoverageError("history", "Insufficient exchange-session history for the requested feature or label dependencies") from error
		if availability_policy.retrospective:
			if data_snapshot is None:
				raise ValueError("Retrospective readiness requires an explicit data snapshot")
			cutoff = data_snapshot
		else:
			cutoff = as_of or label_cutoff or max(item.decision_cutoff for item in decisions)
		if cutoff.tzinfo is None:
			raise ValueError("Readiness requires an aware database cutoff")
		assert cutoff is not None
		effective_cutoff: datetime = cutoff
		boundary = boundary_for_scope(self.source, self.calendar, scope, self.clock.now())
		published_candidates: list[TradingSession] = []
		for session in scope.sessions:
			expected = self.publication.expected_at(session)
			if session.closes_at <= effective_cutoff and (expected is None or expected <= effective_cutoff):
				published_candidates.append(session)
		published = tuple(published_candidates)
		if not published:
			raise SimplifiedCoverageError("coverage", "No required exchange session is available at the requested cutoff")
		SourceConfigurationRepository(self.source, self.connect, self.table_names).assert_compatible()
		try:
			update = run_simplified_update(self.source, self.connect, self.calendar, published, provider=self.provider, windows=(), staging_root=self.staging_root, clock=self.clock, table_names=self.table_names, lock_factory=self.lock_factory)
		except SimplifiedCoverageError:
			raise
		except Exception as error:
			first = published[0]
			raise SimplifiedCoverageError("raw", "Required database coverage could not be completed", session_date=first.session_date) from error
		validation = validate_simplified_database(self.source, self.connect, self.calendar, published, table_names=self.table_names)
		if validation.state == "unfinished_operation":
			raise SimplifiedCoverageError("operation", "An unfinished database operation overlaps the requested range")
		self._require_completion(published)
		revision = database_revision(self.source, self.connect, published, self.table_names)
		update = replace(update, data_revision=revision)
		return SimplifiedReadinessResult(self.source, boundary, scope, update, validation, revision)

	def _require_completion(self, sessions: tuple[TradingSession, ...]) -> None:
		with consistent_read_transaction(self.source, self.connect) as transaction:
			cursor = transaction.cursor()
			require_simplified_schema(cursor, self.table_names)
			table = quote_identifier(self.table_names.table(TableRole.SESSIONS))
			dates = tuple(session.session_date for session in sessions)
			cursor.execute(f"SELECT transaction_date, raw_complete, delta_complete FROM {table} WHERE transaction_date IN ({_placeholders(dates)}) ORDER BY transaction_date", dates)
			rows = cursor.fetchall()
			by_date = {_parse_date(row[0]): (_boolean(row[1], "raw_complete"), _boolean(row[2], "delta_complete")) for row in rows if len(row) == 3}
			for session in sessions:
				value = by_date.get(session.session_date)
				if value is None:
					raise SimplifiedCoverageError("coverage", "Required session is absent after update", session_date=session.session_date)
				if not value[0]:
					raise SimplifiedCoverageError("raw", "Required raw session is not complete", session_date=session.session_date)
				if not value[1]:
					raise SimplifiedCoverageError("delta", "Required derived session is not complete", session_date=session.session_date)


__all__ = [
	"RequiredSessionScope", "SimplifiedCloseRepository", "SimplifiedCoverageError", "SimplifiedEligibilityRepository",
	"SimplifiedReadinessResult", "SimplifiedReadinessWorkflow", "boundary_for_scope", "database_revision", "required_session_scope",
]
