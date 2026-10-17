from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime
from stockguidance.market_data.intervals import ObservationInterval, require_daily_interval
from stockguidance.market_data.publication import DailyPublicationPolicy


class AvailabilityPolicy(StrEnum):
	STRICT = "strict"
	RETROSPECTIVE_DAILY_PREVIOUS_CLOSE = "retrospective_daily_previous_close"

	@property
	def retrospective(self) -> bool:
		return self is AvailabilityPolicy.RETROSPECTIVE_DAILY_PREVIOUS_CLOSE

	@property
	def assumption(self) -> str:
		return "calendar_close_plus_evidenced_publication_delay_for_research_only" if self.retrospective else "actual_receipts_at_decision_cutoff"

	def validate_features(self, specification: object) -> None:
		if not isinstance(self, AvailabilityPolicy):
			raise ValueError("Availability requires a typed policy")
		if self.retrospective:
			from stockguidance.features.specification import SignalMode

			if getattr(specification, "signal_mode", None) is not SignalMode.PREVIOUS_CLOSE:
				raise ValueError("Retrospective availability supports previous_close only; entry_open is not permitted")

	def query_as_of(self, decision_cutoff: datetime, data_snapshot: datetime | None) -> datetime:
		require_aware_datetime(decision_cutoff, "decision cutoff")
		if not self.retrospective:
			return decision_cutoff
		if data_snapshot is None:
			raise ValueError("Retrospective availability requires an explicit data_snapshot")
		require_aware_datetime(data_snapshot, "data snapshot")
		if data_snapshot < decision_cutoff:
			raise ValueError("Retrospective data_snapshot must include the decision cutoff")
		return data_snapshot

	def observation_available_at(self, observation: "ObservationTiming", decision_cutoff: datetime, publication: DailyPublicationPolicy | None) -> bool:
		require_aware_datetime(decision_cutoff, "decision cutoff")
		if self is AvailabilityPolicy.STRICT:
			return observation.available_at <= decision_cutoff
		if publication is None or getattr(publication, "after_close", None) is None:
			raise ValueError("Retrospective availability requires evidenced publication delay")
		expected = publication.expected_at(observation.session)
		return expected is not None and expected <= decision_cutoff


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
