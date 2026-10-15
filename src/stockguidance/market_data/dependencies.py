from dataclasses import dataclass
from datetime import date, datetime

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.coverage import CoveragePlan
from stockguidance.market_data.publication import DailyPublicationPolicy


@dataclass(frozen=True)
class DependencyScope:
	query: SessionQuery | None
	repair_dates: frozenset[date]


def dependency_scope(plan: CoveragePlan, calendar: SessionCalendar, publication: DailyPublicationPolicy, cutoff: datetime) -> DependencyScope:
	if plan.query is None:
		return DependencyScope(None, frozenset())
	sessions = {session.sequence: session for session in plan.query.sessions}
	repair_dates = {session.session_date for session in plan.query.sessions}
	for edge, direction in ((plan.query.sessions[0], -1), (plan.query.sessions[-1], 1)):
		current = edge
		for distance in (1, 2):
			if (direction < 0 and current.session_date <= max(plan.boundary.history_start, calendar.start)) or (direction > 0 and current.session_date >= calendar.end):
				break
			current = calendar.shift(current, direction)
			expected = publication.expected_at(current)
			if current.session_date < plan.boundary.history_start or expected is None or expected > cutoff or current.closes_at > cutoff:
				break
			sessions[current.sequence] = current
			if distance == 1:
				repair_dates.add(current.session_date)
	return DependencyScope(SessionQuery(plan.boundary, tuple(sessions[key] for key in sorted(sessions)), cutoff), frozenset(repair_dates))
