from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Iterable

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date, require_enum, require_text


class HistoryProvenance(StrEnum):
	CONFIGURATION = "configuration"
	SOURCE_HISTORY = "source_history"
	EXPLICIT_BOUNDARY = "explicit_boundary"


class UnknownHistoryBoundary(ValueError):
	pass


class EvidenceQuality(StrEnum):
	VERIFIED = "verified"
	UNVERIFIED = "unverified"
	UNKNOWN = "unknown"
	CORRUPT = "corrupt"


@dataclass(frozen=True)
class LocalObservation:
	"""One retained observation, with source and receipt evidence kept explicit."""

	source: SourceIdentity
	calendar_id: str
	session_date: date
	quality: EvidenceQuality
	evidence: str
	source_bound: bool
	available_at: datetime | None = None
	content_id: str | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("Local observation requires a SourceIdentity")
		require_text(self.calendar_id, "calendar_id")
		require_date(self.session_date, "local session date")
		require_enum(self.quality, EvidenceQuality, "local evidence quality")
		require_text(self.evidence, "local evidence")
		if type(self.source_bound) is not bool:
			raise ValueError("source_bound must be a boolean")
		if self.available_at is not None:
			require_aware_datetime(self.available_at, "local available_at")
		if self.content_id is not None:
			require_text(self.content_id, "local content_id")


@dataclass(frozen=True)
class InventoryIssue:
	"""A bounded inventory problem that must not become coverage evidence."""

	calendar_id: str
	detail: str
	session_date: date | None = None
	path: str | None = None

	def __post_init__(self) -> None:
		require_text(self.calendar_id, "inventory calendar_id")
		require_text(self.detail, "inventory issue")
		if self.session_date is not None:
			require_date(self.session_date, "inventory issue session date")
		if self.path is not None:
			require_text(self.path, "inventory issue path")


@dataclass(frozen=True)
class LocalCoverageInventory:
	"""Read-only evidence collected for one source and calendar scope.

	Legacy database rows may be present in observations, but source_bound must be
	False for them. Their dates remain visible without being promoted to verified
	source coverage.
	"""

	source: SourceIdentity
	calendar_id: str
	observations: tuple[LocalObservation, ...] = ()
	issues: tuple[InventoryIssue, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("Local inventory requires a SourceIdentity")
		require_text(self.calendar_id, "inventory calendar_id")
		if not isinstance(self.observations, tuple) or any(not isinstance(item, LocalObservation) for item in self.observations):
			raise ValueError("Local observations must be an immutable tuple")
		if not isinstance(self.issues, tuple) or any(not isinstance(item, InventoryIssue) for item in self.issues):
			raise ValueError("Inventory issues must be an immutable tuple")
		if any(item.source != self.source or item.calendar_id != self.calendar_id for item in self.observations):
			raise ValueError("Local inventory observations have a different source or calendar")
		keys = [(item.session_date, item.source_bound, item.quality, item.content_id) for item in self.observations]
		if len(keys) != len(set(keys)):
			raise ValueError("Local inventory contains duplicate evidence")

	@property
	def observed_dates(self) -> tuple[date, ...]:
		return tuple(sorted({item.session_date for item in self.observations}))

	@property
	def verified_dates(self) -> tuple[date, ...]:
		return tuple(sorted({item.session_date for item in self.observations if item.source_bound and item.quality is EvidenceQuality.VERIFIED}))

	@property
	def unverified_dates(self) -> tuple[date, ...]:
		return tuple(sorted(set(self.observed_dates) - set(self.verified_dates)))

	@property
	def observed_first(self) -> date | None:
		return self.observed_dates[0] if self.observed_dates else None

	@property
	def observed_last(self) -> date | None:
		return self.observed_dates[-1] if self.observed_dates else None

	@property
	def verified_first(self) -> date | None:
		return self.verified_dates[0] if self.verified_dates else None

	@property
	def verified_last(self) -> date | None:
		return self.verified_dates[-1] if self.verified_dates else None

	@classmethod
	def merge(cls, inventories: Iterable["LocalCoverageInventory"]) -> "LocalCoverageInventory":
		items = tuple(inventories)
		if not items:
			raise ValueError("At least one local inventory is required")
		first = items[0]
		if any(item.source != first.source or item.calendar_id != first.calendar_id for item in items):
			raise ValueError("Local inventories have different source or calendar scopes")
		observations: list[LocalObservation] = []
		issues: list[InventoryIssue] = []
		seen: set[tuple[date, bool, EvidenceQuality, str | None]] = set()
		for item in items:
			for observation in item.observations:
				key = (observation.session_date, observation.source_bound, observation.quality, observation.content_id)
				if key not in seen:
					seen.add(key)
					observations.append(observation)
			issues.extend(item.issues)
		return cls(first.source, first.calendar_id, tuple(observations), tuple(issues))


@dataclass(frozen=True)
class DiscoveryMetadata:
	"""Versioned non-secret discovery cache; v1 boundaries remain separate."""

	source: SourceIdentity
	calendar_id: str
	recorded_at: datetime
	observed_sessions: tuple[date, ...]
	verified_sessions: tuple[date, ...]
	holes: tuple[date, ...]
	provider_state: str
	provider_checked_at: datetime
	provider_provenance: str
	provider_earliest: date | None = None
	provider_latest: date | None = None
	provider_freshness: timedelta | None = None
	provider_earliest_observed_success: date | None = None
	schema_version: int = 2

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("Discovery metadata requires a SourceIdentity")
		require_text(self.calendar_id, "metadata calendar_id")
		require_aware_datetime(self.recorded_at, "metadata recorded_at")
		require_aware_datetime(self.provider_checked_at, "metadata provider_checked_at")
		for name, values in (("observed_sessions", self.observed_sessions), ("verified_sessions", self.verified_sessions), ("holes", self.holes)):
			if not isinstance(values, tuple) or tuple(sorted(set(values))) != values:
				raise ValueError(f"Metadata {name} must be sorted unique session dates")
			for item in values:
				require_date(item, f"metadata {name} date")
		if set(self.verified_sessions) - set(self.observed_sessions):
			raise ValueError("Metadata verified sessions must be observed sessions")
		if not isinstance(self.provider_state, str) or not self.provider_state:
			raise ValueError("Metadata provider state must be nonempty")
		require_text(self.provider_provenance, "metadata provider provenance")
		if self.provider_freshness is not None and (not isinstance(self.provider_freshness, timedelta) or self.provider_freshness < timedelta(0)):
			raise ValueError("Metadata provider freshness must be nonnegative")
		if type(self.schema_version) is not int or self.schema_version != 2:
			raise ValueError("Unsupported discovery metadata version")


@dataclass(frozen=True)
class HistoryBoundary:
	source: SourceIdentity
	calendar_id: str
	history_start: date
	provenance: HistoryProvenance
	evidence: str
	established_at: datetime

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("History source must be a SourceIdentity")
		require_text(self.calendar_id, "calendar_id")
		require_date(self.history_start, "history_start")
		require_enum(self.provenance, HistoryProvenance, "history provenance")
		require_text(self.evidence, "history evidence")
		require_aware_datetime(self.established_at, "established_at")

	def validate_for(self, source: SourceIdentity, calendar: SessionCalendar) -> None:
		if self.source != source or self.calendar_id != calendar.calendar_id:
			raise ValueError("History boundary belongs to a different source/request or calendar")
		calendar.session(self.history_start)


def establish_history_boundary(
	source: SourceIdentity, calendar: SessionCalendar, first_supported_session: date, clock: Clock,
	*, provenance: HistoryProvenance, evidence: str,
) -> HistoryBoundary:
	boundary = HistoryBoundary(source, calendar.calendar_id, first_supported_session, provenance, evidence, decision_cutoff(clock))
	boundary.validate_for(source, calendar)
	return boundary
