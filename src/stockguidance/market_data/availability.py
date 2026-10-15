from dataclasses import dataclass
from datetime import datetime

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime
from stockguidance.market_data.intervals import ObservationInterval, require_daily_interval


@dataclass(frozen=True)
class ObservationTiming:
	"""Completed daily-bar observation time and its actual publication time."""

	session: TradingSession
	observed_at: datetime
	available_at: datetime
	interval: ObservationInterval = ObservationInterval.DAILY

	def __post_init__(self) -> None:
		if not isinstance(self.session, TradingSession):
			raise ValueError("session must be a TradingSession")
		require_daily_interval(self.interval)
		require_aware_datetime(self.observed_at, "observed_at")
		require_aware_datetime(self.available_at, "available_at")
		if self.observed_at != self.session.closes_at:
			raise ValueError("A completed daily bar is observed at its session close")
		if self.available_at < self.observed_at:
			raise ValueError("A daily bar cannot be available before it is observed")

	def is_available_at(self, cutoff: datetime) -> bool:
		require_aware_datetime(cutoff, "cutoff")
		return self.available_at <= cutoff
