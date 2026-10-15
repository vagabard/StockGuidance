from datetime import datetime, timezone
from typing import Protocol

from stockguidance.config.validation import require_aware_datetime


class Clock(Protocol):
	def now(self) -> datetime: ...


class SystemClock:
	def now(self) -> datetime:
		return datetime.now(timezone.utc)


def decision_cutoff(clock: Clock, requested: datetime | None = None) -> datetime:
	now = clock.now()
	require_aware_datetime(now, "clock time")
	if requested is None:
		return now
	require_aware_datetime(requested, "decision cutoff")
	if requested > now:
		raise ValueError("Decision cutoff cannot exceed the injected clock")
	return requested
