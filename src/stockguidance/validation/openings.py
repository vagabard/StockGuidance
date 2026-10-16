from datetime import date, datetime

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.openings import OpeningQuery, OpeningRepository
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding


def validate_opening_coverage(features: FeatureSpec, boundary: HistoryBoundary, calendar: SessionCalendar, start: date, end: date, cutoff: datetime, repository: OpeningRepository) -> tuple[ValidationFinding, ...]:
	if max(start, boundary.history_start) > end:
		return ()
	first_usable = calendar.shift(calendar.session(boundary.history_start), features.lookback_sessions)
	sessions = tuple(session for session in calendar.sessions(max(start, boundary.history_start), end) if session.sequence >= first_usable.sequence)
	if not sessions:
		return ()
	query = OpeningQuery(boundary, sessions, features.ticker_ids, cutoff)
	data = repository.read_openings(query)
	if data.query != query:
		raise ValueError("Opening coverage repository changed scope")
	return tuple(ValidationFinding(FindingCategory(issue.state.value), ValidationCheck.OPENING, issue.detail, issue.session.session_date, (f"ticker_id={issue.ticker_id}",), dependency_session_date=issue.session.session_date) for issue in data.issues)
