from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date, require_enum, require_text


class HistoryProvenance(StrEnum):
	CONFIGURATION = "configuration"
	SOURCE_HISTORY = "source_history"
	EXPLICIT_BOUNDARY = "explicit_boundary"


class UnknownHistoryBoundary(ValueError):
	pass


@dataclass(frozen=True)
class HistoryBoundary:
	source: SourceIdentity
	calendar_id: str
	history_start: date
	provenance: HistoryProvenance
	evidence: str
	established_at: datetime

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("History source must be a SourceIdentity")
		require_text(self.calendar_id, "calendar_id")
		require_date(self.history_start, "history_start")
		require_enum(self.provenance, HistoryProvenance, "history provenance")
		require_text(self.evidence, "history evidence")
		require_aware_datetime(self.established_at, "established_at")

	def validate_for(self, source: SourceIdentity, calendar: SessionCalendar) -> None:
		if self.source != source or self.calendar_id != calendar.calendar_id:
			raise ValueError("History boundary belongs to a different source/request or calendar")
		calendar.session(self.history_start)


def establish_history_boundary(
	source: SourceIdentity, calendar: SessionCalendar, first_supported_session: date, clock: Clock,
	*, provenance: HistoryProvenance, evidence: str,
) -> HistoryBoundary:
	boundary = HistoryBoundary(source, calendar.calendar_id, first_supported_session, provenance, evidence, decision_cutoff(clock))
	boundary.validate_for(source, calendar)
	return boundary
