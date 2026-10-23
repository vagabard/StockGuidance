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
		if not isinstance(self.recent_window, timedelta) or self.recent_window <= timedelta(0) or not isinstance(self.recent_recheck, timedelta) or self.recent_recheck < timedelta(0) or not isinstance(self.archive_recheck, timedelta) or self.archive_recheck <= timedelta(0):
			raise ValueError("Correction window and archive interval must be positive; recent interval may be zero")

	def refresh_due(self, session: TradingSession, verified_at: datetime, now: datetime) -> bool:
		interval = self.recent_recheck if now - session.closes_at <= self.recent_window else self.archive_recheck
		return now - verified_at >= interval
