from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date, require_text
from stockguidance.market_data.history import (
	EvidenceQuality,
	DiscoveryMetadata,
	HistoryBoundary,
	HistoryProvenance,
	LocalCoverageInventory,
	LocalObservation,
	establish_history_boundary,
)
from stockguidance.market_data.providers.contracts import (
	CapabilityState,
	ProviderCapability,
	unknown_capability,
)
from stockguidance.market_data.publication import DailyPublicationPolicy


class DiagnosticSeverity(StrEnum):
	INFO = "info"
	WARNING = "warning"
	ERROR = "error"


@dataclass(frozen=True)
class DiscoveryDiagnostic:
	code: str
	severity: DiagnosticSeverity
	detail: str

	def __post_init__(self) -> None:
		require_text(self.code, "discovery diagnostic code")
		if not isinstance(self.severity, DiagnosticSeverity):
			raise ValueError("Discovery diagnostic severity must be typed")
		require_text(self.detail, "discovery diagnostic detail")


@dataclass(frozen=True)
class DiscoveryRequest:
	"""Optional user bounds; omission asks discovery to resolve the safe scope."""

	source: SourceIdentity
	start: date | None = None
	end: date | None = None
	as_of: datetime | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("Discovery request requires a SourceIdentity")
		if self.start is not None:
			require_date(self.start, "requested discovery start")
		if self.end is not None:
			require_date(self.end, "requested discovery end")
		if self.start is not None and self.end is not None and self.start > self.end:
			raise ValueError("Requested discovery start must not follow end")
		if self.as_of is not None:
			require_aware_datetime(self.as_of, "discovery cutoff")


@dataclass(frozen=True)
class HistoryOverride:
	"""An optional in-memory lower-bound override; it is never persisted here."""

	source: SourceIdentity
	history_start: date
	evidence: str

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("History override requires a SourceIdentity")
		require_date(self.history_start, "override history start")
		require_text(self.evidence, "override history evidence")


class HistoryOverrideConflict(ValueError):
	def __init__(self, detail: str, diagnostics: tuple[DiscoveryDiagnostic, ...] = ()) -> None:
		super().__init__(detail)
		self.diagnostics = diagnostics


@dataclass(frozen=True)
class _FixedClock:
	value: datetime

	def now(self) -> datetime:
		return self.value


@dataclass(frozen=True)
class ObservedLocalBounds:
	source: SourceIdentity
	calendar_id: str
	first_observed: date | None
	last_observed: date | None
	first_quality: EvidenceQuality | None
	last_quality: EvidenceQuality | None
	observed_sessions: tuple[date, ...]
	verified_sessions: tuple[date, ...]

	@property
	def observed_first(self) -> date | None:
		return self.first_observed

	@property
	def observed_last(self) -> date | None:
		return self.last_observed


@dataclass(frozen=True)
class RetainedCoverage:
	source: SourceIdentity
	calendar_id: str
	expected_sessions: tuple[TradingSession, ...]
	observed_sessions: tuple[date, ...]
	verified_sessions: tuple[date, ...]
	missing_sessions: tuple[date, ...]
	unverified_sessions: tuple[date, ...]
	holes: tuple[date, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("Retained coverage requires a SourceIdentity")
		require_text(self.calendar_id, "retained coverage calendar")
		if not isinstance(self.expected_sessions, tuple) or any(not isinstance(item, TradingSession) for item in self.expected_sessions):
			raise ValueError("Expected retained sessions must be an immutable tuple")
		for values in (self.observed_sessions, self.verified_sessions, self.missing_sessions, self.unverified_sessions, self.holes):
			if not isinstance(values, tuple) or any(not isinstance(item, date) for item in values):
				raise ValueError("Retained session dates must be immutable real dates")
		if set(self.verified_sessions) - set(self.observed_sessions):
			raise ValueError("Verified retained sessions must be observed sessions")
		if set(self.missing_sessions) & set(self.observed_sessions):
			raise ValueError("Missing retained sessions cannot also be observed")
		if set(self.holes) != set(self.missing_sessions) | set(self.unverified_sessions):
			raise ValueError("Retained holes must expose missing and unverified sessions")

	@property
	def complete(self) -> bool:
		return bool(self.expected_sessions) and not self.holes


class CapabilityProvider(Protocol):
	def discover_capability(self, source: SourceIdentity, checked_at: datetime) -> ProviderCapability: ...


def discover_provider_capability(provider: object | None, source: SourceIdentity, checked_at: datetime) -> ProviderCapability:
	"""Use only an explicit typed capability adapter; never probe by guessing."""

	if provider is None:
		return unknown_capability(source, checked_at)
	method = getattr(provider, "discover_capability", None)
	if method is None:
		return unknown_capability(source, checked_at, "Provider exposes session fetches but no capability adapter")
	try:
		capability = method(source, checked_at)
	except ValueError:
		raise
	except Exception:
		return ProviderCapability(source, CapabilityState.TRANSIENT_ERROR, checked_at, "adapter-capability", "Capability adapter failed without typed evidence")
	if not isinstance(capability, ProviderCapability) or capability.source != source:
		raise ValueError("Capability adapter returned a different or untyped source scope")
	return capability


def _calendar_sessions(calendar: SessionCalendar, start: date | None, end: date | None) -> tuple[TradingSession, ...]:
	if start is None or end is None:
		return ()
	bounded_start, bounded_end = max(start, calendar.start), min(end, calendar.end)
	if bounded_start > bounded_end:
		return ()
	return calendar.sessions(bounded_start, bounded_end)


def _first_session(calendar: SessionCalendar, requested: date | None) -> TradingSession | None:
	if requested is None:
		return None
	sessions = _calendar_sessions(calendar, max(requested, calendar.start), calendar.end)
	return sessions[0] if sessions else None


def _last_session(calendar: SessionCalendar, requested: date | None) -> TradingSession | None:
	if requested is None:
		return None
	sessions = _calendar_sessions(calendar, calendar.start, min(requested, calendar.end))
	return sessions[-1] if sessions else None


def _session_or_issue(calendar: SessionCalendar, session_date: date, issues: list[DiscoveryDiagnostic]) -> bool:
	try:
		calendar.session(session_date)
		return True
	except ValueError:
		issues.append(DiscoveryDiagnostic("non_session_observation", DiagnosticSeverity.WARNING, f"Local observation {session_date.isoformat()} is not an authoritative exchange session"))
		return False


def _observed_bounds(inventory: LocalCoverageInventory) -> ObservedLocalBounds:
	by_date: dict[date, list[LocalObservation]] = {}
	for observation in inventory.observations:
		by_date.setdefault(observation.session_date, []).append(observation)
	dates = tuple(sorted(by_date))
	def quality_for(session_date: date | None) -> EvidenceQuality | None:
		if session_date is None:
			return None
		items = by_date[session_date]
		if any(item.source_bound and item.quality is EvidenceQuality.VERIFIED for item in items):
			return EvidenceQuality.VERIFIED
		return items[0].quality
	return ObservedLocalBounds(
		inventory.source, inventory.calendar_id, dates[0] if dates else None, dates[-1] if dates else None,
		quality_for(dates[0] if dates else None), quality_for(dates[-1] if dates else None),
		dates, inventory.verified_dates,
	)


def _capability_for_calendar(capability: ProviderCapability, calendar: SessionCalendar, diagnostics: list[DiscoveryDiagnostic]) -> ProviderCapability:
	values: dict[str, date | None] = {}
	for name in ("earliest_session", "latest_session", "earliest_observed_success"):
		value = getattr(capability, name)
		if value is not None:
			try:
				calendar.session(value)
			except ValueError:
				diagnostics.append(DiscoveryDiagnostic("provider_non_session_bound", DiagnosticSeverity.WARNING, f"Provider capability {name} {value.isoformat()} is not an exchange session"))
				value = None
		values[name] = value
	return replace(capability, earliest_session=values["earliest_session"], latest_session=values["latest_session"], earliest_observed_success=values["earliest_observed_success"])


@dataclass(frozen=True)
class CoverageDiscoveryResult:
	source: SourceIdentity
	requested_start: date | None
	requested_end: date | None
	as_of: datetime
	effective_start: date | None
	effective_end: date | None
	latest_finalized_session: TradingSession | None
	latest_expected_published_session: TradingSession | None
	local_bounds: ObservedLocalBounds
	retained: RetainedCoverage
	provider: ProviderCapability
	history_boundary: HistoryBoundary | None
	fetchable_start: date | None
	fetchable_end: date | None
	diagnostics: tuple[DiscoveryDiagnostic, ...]

	@property
	def provider_is_fresh(self) -> bool:
		return self.provider.is_fresh_at(self.as_of)

	@property
	def effective_bounds(self) -> tuple[date | None, date | None]:
		return self.effective_start, self.effective_end

	def metadata_payload(self) -> dict[str, object]:
		return {
			"schema_version": 2,
			"source": {
				"provider": self.source.provider, "endpoint": self.source.endpoint, "market": self.source.market,
				"universe": self.source.universe, "adjustment": self.source.adjustment.value, "interval": self.source.interval.value,
			},
			"calendar_id": self.local_bounds.calendar_id,
			"recorded_at": self.as_of.isoformat(),
			"observed": {
				"first": None if self.local_bounds.first_observed is None else self.local_bounds.first_observed.isoformat(),
				"last": None if self.local_bounds.last_observed is None else self.local_bounds.last_observed.isoformat(),
				"quality_first": None if self.local_bounds.first_quality is None else self.local_bounds.first_quality.value,
				"quality_last": None if self.local_bounds.last_quality is None else self.local_bounds.last_quality.value,
				"sessions": [item.isoformat() for item in self.local_bounds.observed_sessions],
			},
			"retained": {
				"verified_sessions": [item.isoformat() for item in self.retained.verified_sessions],
				"holes": [item.isoformat() for item in self.retained.holes],
			},
			"provider": {
				"state": self.provider.state.value, "checked_at": self.provider.checked_at.isoformat(), "provenance": self.provider.provenance,
				"detail": self.provider.detail, "earliest": None if self.provider.earliest_session is None else self.provider.earliest_session.isoformat(),
				"latest": None if self.provider.latest_session is None else self.provider.latest_session.isoformat(),
				"freshness_seconds": None if self.provider.freshness is None else self.provider.freshness.total_seconds(),
				"earliest_observed_success": None if self.provider.earliest_observed_success is None else self.provider.earliest_observed_success.isoformat(),
			},
			"requested": {"start": None if self.requested_start is None else self.requested_start.isoformat(), "end": None if self.requested_end is None else self.requested_end.isoformat(), "as_of": self.as_of.isoformat()},
			"effective": {"start": None if self.effective_start is None else self.effective_start.isoformat(), "end": None if self.effective_end is None else self.effective_end.isoformat()},
		}

	def metadata(self) -> DiscoveryMetadata:
		return DiscoveryMetadata(
			self.source, self.local_bounds.calendar_id, self.as_of, self.local_bounds.observed_sessions,
			self.retained.verified_sessions, self.retained.holes, self.provider.state.value, self.provider.checked_at,
			self.provider.provenance, self.provider.earliest_session, self.provider.latest_session,
			self.provider.freshness, self.provider.earliest_observed_success,
		)


def discover_coverage(
	source: SourceIdentity,
	calendar: SessionCalendar,
	clock: Clock,
	publication: DailyPublicationPolicy,
	*,
	request: DiscoveryRequest | None = None,
	local_inventory: LocalCoverageInventory | None = None,
	provider: object | None = None,
	provider_capability: ProviderCapability | None = None,
	history_boundary: HistoryBoundary | None = None,
	history_override: HistoryOverride | None = None,
) -> CoverageDiscoveryResult:
	request = request or DiscoveryRequest(source)
	if request.source != source:
		raise ValueError("Discovery request and source identities differ")
	if local_inventory is None:
		local_inventory = LocalCoverageInventory(source, calendar.calendar_id)
	if local_inventory.source != source or local_inventory.calendar_id != calendar.calendar_id:
		raise ValueError("Local inventory and discovery source/calendar differ")
	now = decision_cutoff(clock, request.as_of)
	diagnostics: list[DiscoveryDiagnostic] = []
	for issue in local_inventory.issues:
		diagnostics.append(DiscoveryDiagnostic("local_inventory_issue", DiagnosticSeverity.WARNING, issue.detail))
	valid_observations = tuple(item for item in local_inventory.observations if _session_or_issue(calendar, item.session_date, diagnostics))
	inventory = LocalCoverageInventory(source, calendar.calendar_id, valid_observations, local_inventory.issues)
	local_bounds = _observed_bounds(inventory)

	if history_boundary is not None:
		history_boundary.validate_for(source, calendar)
	if history_override is not None:
		if history_override.source != source:
			raise ValueError("History override belongs to a different source identity")
		candidate = establish_history_boundary(source, calendar, history_override.history_start, _FixedClock(now), provenance=HistoryProvenance.EXPLICIT_BOUNDARY, evidence=history_override.evidence)
		if history_boundary is not None and (history_boundary.history_start != candidate.history_start or history_boundary.calendar_id != candidate.calendar_id):
			diagnostic = DiscoveryDiagnostic("history_override_conflict", DiagnosticSeverity.ERROR, "Explicit history override conflicts with persisted source-bound history metadata")
			raise HistoryOverrideConflict(diagnostic.detail, (diagnostic,))
		if history_boundary is None:
			history_boundary = candidate

	latest_finalized: TradingSession | None = None
	finalized = _calendar_sessions(calendar, calendar.start, min(calendar.end, now.astimezone(calendar.timezone).date()))
	for session in finalized:
		if session.closes_at <= now:
			latest_finalized = session
	latest_expected: TradingSession | None = None
	if publication.after_close is None:
		diagnostics.append(DiscoveryDiagnostic("publication_unknown", DiagnosticSeverity.WARNING, "Publication timing is unknown; no provider fetch is authorized by this result"))
	else:
		for session in finalized:
			if (expected := publication.expected_at(session)) is not None and expected <= now:
				latest_expected = session

	if provider_capability is not None and provider_capability.source != source:
		raise ValueError("Provider capability and discovery source identities differ")
	checked_capability = provider_capability or discover_provider_capability(provider, source, now)
	capability = _capability_for_calendar(checked_capability, calendar, diagnostics)
	if capability.state is CapabilityState.VERIFIED and not capability.is_fresh_at(now):
		capability = replace(capability, state=CapabilityState.STALE)
		diagnostics.append(DiscoveryDiagnostic("provider_capability_stale", DiagnosticSeverity.WARNING, "Provider capability evidence is retained but outside its freshness window"))
	if capability.state is not CapabilityState.VERIFIED:
		diagnostics.append(DiscoveryDiagnostic("provider_capability_unresolved", DiagnosticSeverity.WARNING, f"Provider fetch bounds are not trusted: {capability.state.value}"))

	start_candidates = [value for value in (None if history_boundary is None else history_boundary.history_start, local_bounds.first_observed) if value is not None]
	if capability.is_fresh_at(now) and capability.earliest_session is not None:
		start_candidates.append(capability.earliest_session)
	raw_start = min(start_candidates) if start_candidates else request.start
	if request.start is not None and raw_start is not None:
		raw_start = max(raw_start, request.start)
	raw_end_candidates = [value for value in (request.end, latest_expected.session_date if latest_expected else latest_finalized.session_date if latest_finalized else None) if value is not None]
	raw_end = min(raw_end_candidates) if raw_end_candidates else None
	effective_first = _first_session(calendar, raw_start)
	effective_last = _last_session(calendar, raw_end)
	effective_start = None if effective_first is None else effective_first.session_date
	effective_end = None if effective_last is None else effective_last.session_date
	if effective_start is None or effective_end is None or effective_start > effective_end:
		diagnostics.append(DiscoveryDiagnostic("effective_scope_unknown", DiagnosticSeverity.WARNING, "No bounded effective exchange-session scope was established from local, explicit, or fresh provider evidence"))
	expected_sessions = _calendar_sessions(calendar, effective_start, effective_end)
	expected_dates = {session.session_date for session in expected_sessions}
	observed_dates = tuple(sorted(set(inventory.observed_dates) & expected_dates))
	verified_dates = tuple(sorted(set(inventory.verified_dates) & expected_dates))
	missing_dates = tuple(sorted(expected_dates - set(observed_dates)))
	unverified_dates = tuple(sorted(set(observed_dates) - set(verified_dates)))
	retained = RetainedCoverage(source, calendar.calendar_id, expected_sessions, observed_dates, verified_dates, missing_dates, unverified_dates, tuple(sorted(set(missing_dates) | set(unverified_dates))))
	fetchable_start = capability.earliest_session if capability.is_fresh_at(now) else None
	fetchable_end = capability.latest_session if capability.is_fresh_at(now) else None
	return CoverageDiscoveryResult(
		source, request.start, request.end, now, effective_start, effective_end, latest_finalized, latest_expected,
		local_bounds, retained, capability, history_boundary, fetchable_start, fetchable_end, tuple(diagnostics),
	)


resolve_coverage = discover_coverage
