from datetime import date, datetime

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime


def resolve_decision(calendar: SessionCalendar, entry_date: date, clock: Clock, *, as_of: datetime | None = None) -> DecisionTiming:
	cutoff = decision_cutoff(clock, as_of)
	entry = calendar.session(entry_date)
	return DecisionTiming(calendar.shift(entry, -1), entry, calendar.shift(entry, 1), cutoff)


def latest_finalized_session(calendar: SessionCalendar, cutoff: datetime, *, not_before: date) -> TradingSession | None:
	require_aware_datetime(cutoff, "cutoff")
	end = min(calendar.end, cutoff.astimezone(calendar.timezone).date())
	if end < not_before:
		return None
	for session in reversed(calendar.sessions(not_before, end)):
		if session.closes_at <= cutoff:
			return session
	return None
