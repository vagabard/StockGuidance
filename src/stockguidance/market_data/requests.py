from dataclasses import dataclass
from datetime import date, datetime

from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date


@dataclass(frozen=True)
class MarketDataRequest:
	"""Inclusive requested dates and an independent information-availability cutoff.

	Dates are requested coverage, not a claim that supported history or publication
	has been established. Stage 2 resolves those boundaries and plans the queries.
	"""

	source: SourceIdentity
	start: date
	end: date
	as_of: datetime

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("source must be a SourceIdentity")
		require_date(self.start, "start")
		require_date(self.end, "end")
		require_aware_datetime(self.as_of, "as_of")
		if self.start > self.end:
			raise ValueError("Requested start must not follow end")
