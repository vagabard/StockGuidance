from dataclasses import dataclass
from datetime import datetime

from stockguidance.calendar.sessions import require_next_session
from stockguidance.market_data.availability import ObservationTiming


@dataclass(frozen=True)
class LabelTiming:
	entry_close: ObservationTiming
	exit_close: ObservationTiming

	def __post_init__(self) -> None:
		if not isinstance(self.entry_close, ObservationTiming) or not isinstance(self.exit_close, ObservationTiming):
			raise ValueError("Label timing requires daily close observations")
		require_next_session(self.entry_close.session, self.exit_close.session)

	def is_mature_at(self, training_cutoff: datetime) -> bool:
		return self.entry_close.is_available_at(training_cutoff) and self.exit_close.is_available_at(training_cutoff)
