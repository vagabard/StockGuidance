from datetime import timedelta

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.features.history_window import WindowState, plan_feature_history
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.coverage import CoveragePlan
from stockguidance.market_data.publication import DailyPublicationPolicy


def feature_query(plan: CoveragePlan, query: SessionQuery | None, specification: FeatureSpec | None, calendar: SessionCalendar, publication: DailyPublicationPolicy) -> SessionQuery | None:
	if query is None or specification is None:
		return query
	sessions = set(query.sessions)
	for item in plan.sessions:
		entry = item.session
		if entry.session_date <= calendar.start or entry.session_date >= calendar.end or item.expected_available_at is None or item.expected_available_at > plan.request.as_of:
			continue
		previous = calendar.shift(entry, -1)
		cutoff = min(entry.closes_at - timedelta(microseconds=1), plan.request.as_of)
		if cutoff < previous.closes_at:
			continue
		timing = DecisionTiming(previous, entry, calendar.shift(entry, 1), cutoff)
		window = plan_feature_history(calendar, plan.boundary, specification, timing, publication)
		if window.state is WindowState.EXPECTED_AVAILABLE:
			sessions.update(window.sessions)
	return SessionQuery(query.boundary, tuple(sorted(sessions, key=lambda session: session.sequence)), query.as_of)
