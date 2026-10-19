from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_aware_datetime, require_date
from stockguidance.database.repositories.zscores import ZScoreRepository, ZScoreInputQuery
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.identity import source_key
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.results import ZScoreResult, ZScoreRow, ZScoreStatus
from stockguidance.zscores.specification import DELTA_DEFINITION, ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.storage import ZScoreSeriesDefinition


@dataclass(frozen=True)
class ZScoreUpdateSettings:
	start: date
	end: date
	windows: tuple[int, ...]
	publication: DailyPublicationPolicy
	initialize_schema: bool = False

	def __post_init__(self) -> None:
		require_date(self.start, "z-score update start")
		require_date(self.end, "z-score update end")
		if self.start > self.end:
			raise ValueError("Z-score update start must not follow end")
		if not isinstance(self.windows, tuple) or not self.windows or len(set(self.windows)) != len(self.windows) or any(type(window) is not int or window < 2 for window in self.windows):
			raise ValueError("Z-score windows must be distinct integers of at least 2")
		if not isinstance(self.publication, DailyPublicationPolicy) or self.publication.after_close is None:
			raise ValueError("Z-score updates require an evidenced publication delay")
		if type(self.initialize_schema) is not bool:
			raise ValueError("Invalid z-score schema initialization flag")


@dataclass(frozen=True)
class ZScoreSkippedSession:
	session: TradingSession
	reason: str


@dataclass(frozen=True)
class ZScoreResultKey:
	family: ZScoreFamily
	window: int
	session: TradingSession


@dataclass(frozen=True)
class ZScoreUpdateResult:
	requested_sessions: tuple[TradingSession, ...]
	effective_sessions: tuple[TradingSession, ...]
	warmup_sessions: tuple[TradingSession, ...]
	cutoff: datetime
	windows: tuple[int, ...]
	initialized_schema: bool
	skipped_sessions: tuple[ZScoreSkippedSession, ...]
	written_sessions: tuple[TradingSession, ...]
	skipped_results: tuple[ZScoreResultKey, ...]
	rebuilt_results: tuple[ZScoreResultKey, ...]
	status_counts: tuple[tuple[ZScoreFamily, int, ZScoreStatus, int], ...]
	failures: tuple[str, ...]

	@property
	def ready_count(self) -> int:
		return sum(count for _, _, status, count in self.status_counts if status is ZScoreStatus.READY)

	@property
	def unavailable_count(self) -> int:
		return sum(count for _, _, status, count in self.status_counts if status is not ZScoreStatus.READY)

	@property
	def complete(self) -> bool:
		if self.failures:
			return False
		if any(item.reason == "not_yet_available_at_cutoff" for item in self.skipped_sessions):
			return False
		return not any(status in {ZScoreStatus.MISSING_INPUT, ZScoreStatus.UNVERIFIED_INPUT, ZScoreStatus.INVALID_INPUT} for _, _, status, _ in self.status_counts)

	@property
	def exit_code(self) -> int:
		return 1 if self.failures else 0 if self.complete else 2


def _rows_match(expected: tuple[ZScoreRow, ...], stored: tuple[ZScoreRow, ...]) -> bool:
	def key(row: ZScoreRow) -> tuple[int, int, float | None, ZScoreStatus]:
		return row.ticker_id, row.window, row.score, row.status

	return sorted(map(key, expected)) == sorted(map(key, stored))


def _definition(source: SourceConfig, calendar: SessionCalendar, family: ZScoreFamily, specification: ZScoreSpecification, window: int) -> ZScoreSeriesDefinition:
	return ZScoreSeriesDefinition(
		 source_key(source.identity), calendar.calendar_id, family, window,
		specification.calculation_version, specification.precision, specification.delta_definition,
	)


def _status_counts(results: tuple[tuple[ZScoreFamily, int, ZScoreResult], ...]) -> tuple[tuple[ZScoreFamily, int, ZScoreStatus, int], ...]:
	counts: Counter[tuple[ZScoreFamily, int, ZScoreStatus]] = Counter()
	for family, window, result in results:
		for row in result.rows:
			counts[(family, window, row.status)] += 1
	return tuple((family, window, status, counts[(family, window, status)]) for family, window, status in sorted(counts, key=lambda item: (item[0].value, item[1], int(item[2]))))


def update_zscores(
	source: SourceConfig,
	settings: ZScoreUpdateSettings,
	boundary: HistoryBoundary,
	calendar: SessionCalendar,
	repository: ZScoreRepository,
	clock: Clock,
	*,
	initialized_schema: bool = False,
) -> ZScoreUpdateResult:
	if not isinstance(source, SourceConfig) or not isinstance(boundary, HistoryBoundary):
		raise ValueError("Invalid z-score update dependencies")
	if boundary.source != source.identity:
		raise ValueError("Z-score history boundary belongs to a different source")
	boundary.validate_for(source.identity, calendar)
	cutoff = decision_cutoff(clock)
	require_aware_datetime(cutoff, "z-score update cutoff")
	requested = calendar.sessions(settings.start, settings.end)
	max_window = max(settings.windows)
	skipped: list[ZScoreSkippedSession] = []
	effective: list[TradingSession] = []
	for session in requested:
		expected_at = settings.publication.expected_at(session)
		if session.session_date < boundary.history_start:
			skipped.append(ZScoreSkippedSession(session, "before_supported_history_boundary"))
		elif session.closes_at > cutoff or expected_at is None or expected_at > cutoff:
			skipped.append(ZScoreSkippedSession(session, "not_yet_available_at_cutoff"))
		else:
			effective.append(session)
	if not effective:
		return ZScoreUpdateResult(
			requested, (), (), cutoff, settings.windows, initialized_schema, tuple(skipped), (), (), (), (), (),
		)
	first_required = calendar.shift(effective[0], -max_window)
	input_start = max(first_required.session_date, boundary.history_start)
	input_sessions = calendar.sessions(input_start, effective[-1].session_date)
	input_sessions = tuple(session for session in input_sessions if session.closes_at <= cutoff)
	if not input_sessions or input_sessions[-1] != effective[-1]:
		raise ValueError("Z-score input scope does not contain the effective requested sessions")
	from stockguidance.database.repositories.sessions import SessionQuery

	query_scope = SessionQuery(boundary, input_sessions, cutoff)
	try:
		ticker_ids = repository.list_ticker_ids()
	except Exception:
		return ZScoreUpdateResult(
			requested, tuple(effective), input_sessions, cutoff, settings.windows, initialized_schema,
			tuple(skipped), (), (), (), (), ("Z-score ticker mappings could not be read",),
		)
	if not ticker_ids:
		raise ValueError("No ticker mappings are available for z-score computation")
	computed_at = cutoff
	all_results: list[tuple[ZScoreFamily, int, ZScoreResult]] = []
	skipped_results: list[ZScoreResultKey] = []
	rebuilt_results: list[ZScoreResultKey] = []
	written_sessions: set[TradingSession] = set()
	failures: list[str] = []
	for family in (ZScoreFamily.PRICE, ZScoreFamily.PRICE_DELTA):
		specification = ZScoreSpecification(family, settings.windows, delta_definition=DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
		try:
			loaded = repository.load_inputs(ZScoreInputQuery(query_scope, family, ticker_ids, tuple(effective)))
			result = calculate_zscores(specification, loaded.input)
		except Exception:
			failures.append(f"{family.value} z-score inputs or calculation failed")
			continue
		for window in settings.windows:
			definition = _definition(source, calendar, family, specification, window)
			rows_by_session = {session: tuple(row for row in result.rows if row.window == window and row.session == session) for session in effective}
			dependencies = {dependency.session: dependency for dependency in loaded.dependencies if dependency.session in effective}
			stale_dependencies = []
			stale_keys: list[ZScoreResultKey] = []
			for session in effective:
				dependency = dependencies.get(session)
				if dependency is None:
					raise ValueError("Z-score dependency scope does not contain an effective session")
				key = ZScoreResultKey(family, window, session)
				try:
					verified = repository.read_verified(definition, dependency)
					if _rows_match(rows_by_session[session], verified.rows):
						skipped_results.append(key)
						continue
				except (KeyError, ValueError):
					pass
				stale_dependencies.append(dependency)
				stale_keys.append(key)
			if stale_dependencies:
				try:
					repository.publish(definition, result, tuple(stale_dependencies), computed_at)
				except Exception:
					failures.append(f"{family.value} z-score window {window} publication failed")
				else:
					rebuilt_results.extend(stale_keys)
					written_sessions.update(dependency.session for dependency in stale_dependencies)
			all_results.append((family, window, result))
	return ZScoreUpdateResult(
		requested, tuple(effective), input_sessions, cutoff, settings.windows, initialized_schema,
		tuple(skipped), tuple(sorted(written_sessions, key=lambda session: session.sequence)), tuple(skipped_results), tuple(rebuilt_results),
		_status_counts(tuple(all_results)), tuple(failures),
	)
