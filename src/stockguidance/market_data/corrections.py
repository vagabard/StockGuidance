from dataclasses import dataclass
from datetime import datetime, timedelta

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_text


@dataclass(frozen=True)
class CorrectionPolicy:
	recent_window: timedelta
	recent_recheck: timedelta
	archive_recheck: timedelta
	provenance: str

	def __post_init__(self) -> None:
		require_text(self.provenance, "correction policy provenance")
		if any(not isinstance(value, timedelta) or value <= timedelta(0) for value in (self.recent_window, self.recent_recheck, self.archive_recheck)):
			raise ValueError("Correction windows and recheck intervals must be positive")

	def refresh_due(self, session: TradingSession, verified_at: datetime, now: datetime) -> bool:
		interval = self.recent_recheck if now - session.closes_at <= self.recent_window else self.archive_recheck
		return now - verified_at >= interval
