from datetime import date

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.sessions import SessionQuery, SessionRepository, StoredSession
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.coverage import CoveragePlan, SessionUpdate, UpdateAction
from stockguidance.market_data.history import HistoryBoundary, UnknownHistoryBoundary
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.validation.coverage import CoverageState


def _assessment(session: TradingSession, request: MarketDataRequest, publication: DailyPublicationPolicy, stored: StoredSession | None) -> SessionUpdate:
	expected = publication.expected_at(session)
	if session.closes_at > request.as_of or (expected is not None and expected > request.as_of):
		return SessionUpdate(session, CoverageState.NOT_YET_AVAILABLE, UpdateAction.NONE, expected, None, "Daily close or expected publication is after the cutoff")
	if expected is None:
		return SessionUpdate(session, CoverageState.UNVERIFIED, UpdateAction.NONE, None, None, "Provider publication schedule is unknown; no request authorized")
	if stored is None:
		return SessionUpdate(session, CoverageState.MISSING, UpdateAction.FETCH, expected, None, "Expected published session is absent from storage")
	if stored.available_at is not None and stored.available_at > request.as_of:
		return SessionUpdate(session, CoverageState.NOT_YET_AVAILABLE, UpdateAction.NONE, expected, stored.date_id, "Stored observation was not available by the cutoff")
	actions = {
		CoverageState.COMPLETE: UpdateAction.NONE, CoverageState.MISSING: UpdateAction.FETCH,
		CoverageState.INCOMPLETE: UpdateAction.REPAIR, CoverageState.INCONSISTENT: UpdateAction.REPAIR,
		CoverageState.NOT_YET_AVAILABLE: UpdateAction.NONE, CoverageState.UNVERIFIED: UpdateAction.VERIFY,
	}
	return SessionUpdate(session, stored.state, actions[stored.state], expected, stored.date_id, stored.detail)


def plan_coverage(
	source: SourceConfig, request: MarketDataRequest, boundary: HistoryBoundary | None,
	calendar: SessionCalendar, publication: DailyPublicationPolicy, repository: SessionRepository, clock: Clock,
) -> CoveragePlan:
	"""Resolve all requested sessions before a bounded, read-only repository call.

	No missing session requires an existing date ID. Stage 3 resolves newly
	ingested IDs by reading again after its ingestion transaction completes.
	"""
	if boundary is None:
		raise UnknownHistoryBoundary("First supported session is unknown; supply and persist a boundary with provenance before planning")
	if source.identity != request.source:
		raise ValueError("Configured source and coverage request identities differ")
	boundary.validate_for(request.source, calendar)
	cutoff = decision_cutoff(clock, request.as_of)
	start = max(request.start, boundary.history_start)
	cutoff_day = cutoff.astimezone(calendar.timezone).date()
	end = min(request.end, cutoff_day)
	expected = calendar.sessions(start, end) if start <= end else ()
	finalized = tuple(session for session in expected if session.closes_at <= cutoff)
	available = tuple(session for session in finalized if (published := publication.expected_at(session)) is not None and published <= cutoff)
	query = SessionQuery(boundary, available, cutoff) if available else None
	rows = repository.read(query) if query is not None else ()
	by_date: dict[date, StoredSession] = {}
	seen_ids: set[int] = set()
	allowed = {session.session_date: session for session in available}
	observations: list[ObservationTiming] = []
	if not isinstance(rows, tuple):
		raise ValueError("Session repository must return an immutable tuple")
	for row in rows:
		if not isinstance(row, StoredSession) or row.session_date not in allowed:
			raise ValueError("Repository returned a session outside the bounded query")
		if row.session_date in by_date or row.date_id in seen_ids:
			raise ValueError("Repository returned duplicate dates or IDs")
		by_date[row.session_date] = row
		seen_ids.add(row.date_id)
		if row.available_at is not None:
			observation = ObservationTiming(allowed[row.session_date], allowed[row.session_date].closes_at, row.available_at)
			if row.state is not CoverageState.MISSING and observation.is_available_at(cutoff):
				observations.append(observation)
	updates = tuple(_assessment(session, request, publication, by_date.get(session.session_date)) for session in expected)
	observed_dates = sorted(row.session_date for row in rows if row.state is not CoverageState.MISSING)
	return CoveragePlan(
		request, boundary, calendar.version, updates, query,
		finalized[-1] if finalized else None, available[-1] if available else None,
		tuple(sorted(observations, key=lambda item: item.session.session_date)),
		observed_dates[0] if observed_dates else None, observed_dates[-1] if observed_dates else None,
		request.start < boundary.history_start, request.end > cutoff_day,
	)
