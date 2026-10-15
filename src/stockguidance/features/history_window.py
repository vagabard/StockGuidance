from dataclasses import dataclass
from enum import StrEnum

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.publication import DailyPublicationPolicy


class WindowState(StrEnum):
	EXPECTED_AVAILABLE = "expected_available"
	INSUFFICIENT_HISTORY = "insufficient_history"
	NOT_YET_AVAILABLE = "not_yet_available"
	UNVERIFIED = "unverified"


@dataclass(frozen=True)
class FeatureHistoryWindow:
	"""Calendar/lookback readiness only; Stage 5 validates actual feature data."""

	state: WindowState
	sessions: tuple[TradingSession, ...]
	first_usable_entry: TradingSession


def plan_feature_history(
	calendar: SessionCalendar, boundary: HistoryBoundary, specification: FeatureSpec,
	timing: DecisionTiming, publication: DailyPublicationPolicy,
) -> FeatureHistoryWindow:
	boundary.validate_for(boundary.source, calendar)
	if timing.entry_session != calendar.session(timing.entry_session.session_date):
		raise ValueError("Decision entry does not match the authoritative calendar")
	first = calendar.shift(calendar.session(boundary.history_start), specification.lookback_sessions)
	if timing.entry_session.sequence < first.sequence:
		return FeatureHistoryWindow(WindowState.INSUFFICIENT_HISTORY, (), first)
	window = tuple(calendar.shift(timing.entry_session, offset) for offset in range(-specification.lookback_sessions, 0))
	publication_times = tuple(publication.expected_at(session) for session in window)
	if any(available_at is None for available_at in publication_times):
		state = WindowState.UNVERIFIED
	elif any(available_at is not None and available_at > timing.decision_cutoff for available_at in publication_times):
		state = WindowState.NOT_YET_AVAILABLE
	else:
		state = WindowState.EXPECTED_AVAILABLE
	return FeatureHistoryWindow(state, window, first)
