from dataclasses import dataclass
from datetime import datetime, timedelta

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_text


@dataclass(frozen=True)
class DailyPublicationPolicy:
	"""Explicit source expectation, not proof of receipt or actual publication.

	None denotes an unknown publication schedule and cannot authorize a fetch.
	Actual observed availability must still be recorded during ingestion.
	"""

	after_close: timedelta | None
	provenance: str

	def __post_init__(self) -> None:
		require_text(self.provenance, "publication provenance")
		if self.after_close is not None and (not isinstance(self.after_close, timedelta) or self.after_close < timedelta(0)):
			raise ValueError("Publication delay must be a nonnegative timedelta or unknown")

	def expected_at(self, session: TradingSession) -> datetime | None:
		return None if self.after_close is None else session.closes_at + self.after_close
