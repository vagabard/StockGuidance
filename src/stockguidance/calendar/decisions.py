from dataclasses import dataclass
from datetime import datetime

from stockguidance.calendar.sessions import TradingSession, require_next_session
from stockguidance.config.validation import require_aware_datetime


@dataclass(frozen=True)
class DecisionTiming:
	"""A pre-close decision, a same-close entry assumption, and next-session exit."""

	previous_session: TradingSession
	entry_session: TradingSession
	exit_session: TradingSession
	decision_cutoff: datetime

	def __post_init__(self) -> None:
		require_next_session(self.previous_session, self.entry_session)
		require_next_session(self.entry_session, self.exit_session)
		require_aware_datetime(self.decision_cutoff, "decision_cutoff")
		if not self.previous_session.closes_at <= self.decision_cutoff < self.entry_time:
			raise ValueError("Decision cutoff must follow the previous close and precede the entry close")

	@property
	def entry_time(self) -> datetime:
		return self.entry_session.closes_at

	@property
	def exit_time(self) -> datetime:
		return self.exit_session.closes_at
