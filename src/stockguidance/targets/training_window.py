from dataclasses import dataclass
from datetime import date

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_positive_int
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.availability import LabelTiming


@dataclass(frozen=True)
class TrainingHistoryWindow:
	"""Expected temporal window and separately proven mature price timings.

	Missing mature prices remain visible instead of extending the requested
	window into older data. This is not a numerical sample/dataset builder.
	"""

	entry_sessions: tuple[TradingSession, ...]
	mature_entries: tuple[TradingSession, ...]
	missing_expected_prices: tuple[TradingSession, ...]
	not_yet_available_entries: tuple[TradingSession, ...]
	unverified_entries: tuple[TradingSession, ...]


def plan_training_history(
	calendar: SessionCalendar, boundary: HistoryBoundary, specification: FeatureSpec,
	timing: DecisionTiming, publication: DailyPublicationPolicy, observations: tuple[ObservationTiming, ...],
	*, count: int,
) -> TrainingHistoryWindow:
	require_positive_int(count, "training history count")
	boundary.validate_for(boundary.source, calendar)
	if timing.entry_session != calendar.session(timing.entry_session.session_date):
		raise ValueError("Decision entry does not match the authoritative calendar")
	first = calendar.shift(calendar.session(boundary.history_start), specification.lookback_sessions)
	candidates = calendar.sessions(first.session_date, timing.previous_session.session_date) if first.sequence <= timing.previous_session.sequence else ()
	expected: list[TradingSession] = []
	immature: list[TradingSession] = []
	unverified: list[TradingSession] = []
	for entry in candidates:
		exit_session = calendar.shift(entry, 1)
		publications = (publication.expected_at(entry), publication.expected_at(exit_session))
		if any(available_at is None for available_at in publications):
			unverified.append(entry)
		elif any(available_at is not None and available_at > timing.decision_cutoff for available_at in publications):
			immature.append(entry)
		else:
			expected.append(entry)
	by_date: dict[date, ObservationTiming] = {}
	for observation in observations:
		if not isinstance(observation, ObservationTiming) or observation.session != calendar.session(observation.session.session_date):
			raise ValueError("Observation does not match the authoritative calendar")
		if observation.session.session_date in by_date:
			raise ValueError("Duplicate training observation date")
		by_date[observation.session.session_date] = observation
	selected = tuple(expected[-count:])
	mature: list[TradingSession] = []
	missing: dict[int, TradingSession] = {}
	for entry in selected:
		exit_session = calendar.shift(entry, 1)
		entry_close, exit_close = by_date.get(entry.session_date), by_date.get(exit_session.session_date)
		for session, required_observation in ((entry, entry_close), (exit_session, exit_close)):
			if required_observation is None:
				missing[session.sequence] = session
		if entry_close is not None and exit_close is not None:
			if LabelTiming(entry_close, exit_close).is_mature_at(timing.decision_cutoff):
				mature.append(entry)
			else:
				immature.append(entry)
	return TrainingHistoryWindow(selected, tuple(mature), tuple(missing[key] for key in sorted(missing)), tuple(sorted(immature, key=lambda item: item.sequence)), tuple(unverified))
