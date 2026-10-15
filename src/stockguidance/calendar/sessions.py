from dataclasses import dataclass
from datetime import date, datetime

from stockguidance.config.validation import require_aware_datetime, require_date, require_text


@dataclass(frozen=True)
class TradingSession:
	"""A calendar-supplied session; sequence is a calendar ordinal, never a DB ID.

	The calendar adapter must use one stable ordinal origin for a calendar_id.
	No exchange calendar, timezone, or historical lower bound is guessed here.
	"""

	calendar_id: str
	session_date: date
	sequence: int
	opens_at: datetime
	closes_at: datetime

	def __post_init__(self) -> None:
		require_text(self.calendar_id, "calendar_id")
		require_date(self.session_date, "session_date")
		if type(self.sequence) is not int:
			raise ValueError("sequence must be a calendar integer ordinal")
		require_aware_datetime(self.opens_at, "opens_at")
		require_aware_datetime(self.closes_at, "closes_at")
		if self.opens_at >= self.closes_at:
			raise ValueError("Session opening must precede its closing")


def require_next_session(previous: TradingSession, following: TradingSession) -> None:
	if not isinstance(previous, TradingSession) or not isinstance(following, TradingSession):
		raise ValueError("Session timing requires TradingSession values")
	if following.calendar_id != previous.calendar_id:
		raise ValueError("Sessions must use the same calendar")
	if following.sequence != previous.sequence + 1:
		raise ValueError("Expected the next trading session, not a calendar-day or database-ID offset")
	if following.session_date <= previous.session_date or following.opens_at <= previous.closes_at:
		raise ValueError("Consecutive session dates and timestamps must increase")
