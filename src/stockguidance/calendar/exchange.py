from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from datetime import date, datetime, timezone
from importlib import import_module
from importlib.metadata import version
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime, require_date, require_text


class _ExchangeBackend(Protocol):
	name: str
	tz: object
	sessions: Iterable[object]

	def session_open(self, session: str) -> object: ...
	def session_close(self, session: str) -> object: ...


def _timestamp(value: object) -> datetime:
	if not isinstance(value, datetime):
		raise ValueError("Exchange calendar returned an invalid timestamp")
	require_aware_datetime(value, "exchange timestamp")
	return datetime.fromtimestamp(value.timestamp(), tz=timezone.utc)


class ExchangeCalendar:
	"""Lazy exchange_calendars adapter with explicit bounds and UTC timestamps.

	Ordinals count exchange sessions relative to 1970-01-01, independently of
	the loaded range or database. That epoch is not a provider history boundary.
	Creating this adapter loads the calendar; importing its module does not.
	"""

	def __init__(self, name: str, start: date, end: date) -> None:
		require_text(name, "calendar name")
		require_date(start, "calendar start")
		require_date(end, "calendar end")
		if start > end:
			raise ValueError("Calendar start must not follow end")
		self.start = start
		self.end = end
		epoch = date(1970, 1, 1)
		loaded_start, loaded_end = min(start, epoch), max(end, epoch)
		library = import_module("exchange_calendars")
		requested_name = name.strip()
		self._backend = cast(_ExchangeBackend, library.get_calendar(requested_name, start=loaded_start.isoformat(), end=loaded_end.isoformat()))
		self.calendar_id = "XNAS" if requested_name.upper() in {"XNAS", "NASDAQ"} and self._backend.name == "XNYS" else self._backend.name
		self.version = version("exchange-calendars")
		self.timezone = ZoneInfo(str(self._backend.tz))
		labels: list[date] = []
		for label in self._backend.sessions:
			if not isinstance(label, datetime):
				raise ValueError("Exchange calendar returned an invalid session label")
			labels.append(label.date())
		if not labels or labels != sorted(set(labels)):
			raise ValueError("Exchange calendar session labels must be unique and chronological")
		self._dates = tuple(labels)
		self._origin = bisect_left(self._dates, epoch)
		self._indices = {session_date: index for index, session_date in enumerate(self._dates)}

	def _check_bounds(self, session_date: date) -> None:
		require_date(session_date, "session date")
		if not self.start <= session_date <= self.end:
			raise ValueError("Date is outside the explicitly loaded calendar range")

	def session(self, session_date: date) -> TradingSession:
		self._check_bounds(session_date)
		if session_date not in self._indices:
			raise ValueError(f"{session_date.isoformat()} is not a {self.calendar_id} trading session")
		return TradingSession(
			self.calendar_id, session_date, self._indices[session_date] - self._origin,
			_timestamp(self._backend.session_open(session_date.isoformat())),
			_timestamp(self._backend.session_close(session_date.isoformat())),
		)

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		self._check_bounds(start)
		self._check_bounds(end)
		if start > end:
			raise ValueError("Session range start must not follow end")
		return tuple(self.session(day) for day in self._dates[bisect_left(self._dates, start):bisect_right(self._dates, end)])

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		if not isinstance(session, TradingSession) or session != self.session(session.session_date):
			raise ValueError("Session does not match this authoritative calendar")
		if type(offset) is not int:
			raise ValueError("Session offset must be an integer")
		index = self._indices[session.session_date] + offset
		if not 0 <= index < len(self._dates):
			raise ValueError("Session offset exceeds the loaded calendar")
		return self.session(self._dates[index])
