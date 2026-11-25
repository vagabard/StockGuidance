"""Resolve recommendation periods once, before source preparation or fitting."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

from stockguidance.calendar.sessions import TradingSession
from stockguidance.calendar.protocols import SessionCalendar


def resolve_start(expression: str, end: date) -> date:
	match = re.fullmatch(r"-(\d+)([yd])", expression)
	if not match:
		return date.fromisoformat(expression)
	amount = int(match[1])
	if match[2] == "d":
		return end - timedelta(days=amount)
	try:
		return end.replace(year=end.year-amount)
	except ValueError:
		if end.month == 2 and end.day == 29:
			return date(end.year-amount, 2, 28)
		raise


@dataclass(frozen=True)
class RecommendationPeriod:
	requested_start: date
	requested_end: date
	sessions: tuple[TradingSession, ...]
	entry: TradingSession
	decision_cutoff: datetime
	replay: bool
	start_expression: str = ""
	end_expression: str | None = None

	def to_mapping(self) -> dict[str, str]:
		return {"start_expression":self.start_expression or str(self.requested_start),"end_expression":self.end_expression or "today","requested_start":str(self.requested_start),"requested_end":str(self.requested_end),
			"start_session":str(self.sessions[0].session_date),"data_end_session":str(self.sessions[-1].session_date),
			"entry_session":str(self.entry.session_date),"decision_cutoff":self.decision_cutoff.isoformat(),
			"mode":"historical_replay" if self.replay else "live", "history_policy":"current_revised_dataset"}


@dataclass(frozen=True)
class ReplayClock:
	value: datetime

	def now(self) -> datetime:
		return self.value


def resolve_period(start: str, end: str | None, calendar: SessionCalendar, now: datetime, *, publication_delay: timedelta=timedelta(0), decision_lead: timedelta=timedelta(minutes=30)) -> RecommendationPeriod:
	if now.tzinfo is None:
		raise ValueError("Recommendation clock must be timezone aware")
	if not timedelta(0) < decision_lead < timedelta(days=1):
		raise ValueError("Decision lead must be positive and less than a day")
	requested_end = date.fromisoformat(end) if end else now.astimezone(ZoneInfo("America/New_York")).date()
	requested_start = resolve_start(start, requested_end)
	if requested_start > requested_end:
		raise ValueError("Recommendation start follows its end")
	if requested_end > now.astimezone(ZoneInfo("America/New_York")).date():
		raise ValueError("Recommendation data end cannot be in the future")
	sessions = tuple(s for s in calendar.sessions(requested_start,requested_end) if s.closes_at+publication_delay <= now)
	if not sessions:
		raise ValueError("The selected period has no completed published trading sessions")
	entry = calendar.shift(sessions[-1],1)
	if end is None and entry.closes_at <= now:
		raise ValueError("The latest closed session is not published yet; retry after publication")
	replay = entry.closes_at <= now
	cutoff = entry.closes_at-decision_lead if replay else now
	return RecommendationPeriod(requested_start,requested_end,sessions,entry,cutoff,replay,start,end)
