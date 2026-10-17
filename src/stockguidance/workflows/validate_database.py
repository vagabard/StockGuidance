from dataclasses import dataclass, replace
from datetime import date, datetime

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_aware_datetime, require_date
from stockguidance.database.repositories.sessions import SessionQuery, StoredSession
from stockguidance.database.repositories.validation import DatabaseInspection, ValidationRepository
from stockguidance.features.specification import FeatureSpec, SignalMode
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.market_data.openings import OpeningRepository
from stockguidance.market_data.coverage_planning import plan_coverage
from stockguidance.market_data.dependencies import dependency_scope
from stockguidance.market_data.discovery import CoverageDiscoveryResult
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore, SourceManifest
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.market_data.update_planning import SnapshotIndex
from stockguidance.market_data.coverage import CoveragePlan, SessionUpdate, UpdateAction
from stockguidance.validation.assessment import assess_session, coverage_state
from stockguidance.validation.boundaries import boundary_findings
from stockguidance.validation.close_samples import validate_close_samples
from stockguidance.validation.coverage import CoverageState
from stockguidance.validation.feature_scope import feature_query
from stockguidance.validation.coverage import CoverageResult, SessionCoverage
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding
from stockguidance.validation.repair import plan_repairs
from stockguidance.validation.result import ValidationResult
from stockguidance.validation.openings import validate_opening_coverage
from stockguidance.targets.specification import TargetSpec


@dataclass(frozen=True)
class ValidationSettings:
	start: date | None
	end: date | None
	publication: DailyPublicationPolicy
	as_of: datetime | None = None
	features: FeatureSpec | None = None
	target: TargetSpec = TargetSpec()

	def __post_init__(self) -> None:
		if self.start is not None:
			require_date(self.start, "validation start")
		if self.end is not None:
			require_date(self.end, "validation end")
		if self.start is not None and self.end is not None and self.start > self.end or not isinstance(self.publication, DailyPublicationPolicy):
			raise ValueError("Invalid validation bounds or publication policy")
		if self.as_of is not None:
			require_aware_datetime(self.as_of, "validation cutoff")
		if (self.features is not None and not isinstance(self.features, FeatureSpec)) or not isinstance(self.target, TargetSpec):
			raise ValueError("Validation requires explicit feature/target contracts")


class _NoStoredSessions:
	def read(self, query: SessionQuery) -> tuple[StoredSession, ...]:
		return ()


def _query_boundary(source: SourceConfig, calendar: SessionCalendar, discovery: CoverageDiscoveryResult, cutoff: datetime, scope_start: date | None) -> HistoryBoundary:
	boundary = discovery.history_boundary
	if boundary is not None and (scope_start is None or scope_start >= boundary.history_start):
		return boundary
	sessions = calendar.sessions(calendar.start, calendar.end)
	if not sessions:
		raise ValueError("Validation calendar has no exchange sessions")
	return HistoryBoundary(source.identity, calendar.calendar_id, sessions[0].session_date, HistoryProvenance.SOURCE_HISTORY, "Read-only query floor; earliest retained source history remains unresolved", cutoff)


def _plan_from_discovery(source: SourceConfig, settings: ValidationSettings, discovery: CoverageDiscoveryResult, calendar: SessionCalendar, cutoff: datetime) -> CoveragePlan:
	scope_sessions = discovery.retained.expected_sessions
	scope_start = None if not scope_sessions else scope_sessions[0].session_date
	scope_end = None if not scope_sessions else scope_sessions[-1].session_date
	calendar_sessions = calendar.sessions(calendar.start, calendar.end)
	default_start = scope_start or discovery.requested_start or (calendar_sessions[0].session_date if calendar_sessions else cutoff.date())
	default_end = scope_end or discovery.requested_end or (discovery.latest_finalized_session.session_date if discovery.latest_finalized_session is not None else default_start)
	request = MarketDataRequest(source.identity, settings.start or default_start, settings.end or default_end, cutoff)
	boundary = _query_boundary(source, calendar, discovery, cutoff, scope_start)
	updates: list[SessionUpdate] = []
	for session in scope_sessions:
		expected = settings.publication.expected_at(session)
		if session.closes_at > cutoff or (expected is not None and expected > cutoff):
			state = CoverageState.NOT_YET_AVAILABLE
			detail = "Daily close or expected publication is after the cutoff"
		else:
			state = CoverageState.MISSING
			detail = "Expected retained session requires local evidence reconciliation"
		updates.append(SessionUpdate(session, state, UpdateAction.NONE, expected, None, detail))
	query_sessions = tuple(session for session in scope_sessions if session.closes_at <= cutoff)
	query = None if not query_sessions else SessionQuery(boundary, query_sessions, cutoff)
	observed = [item for item in discovery.local_bounds.observed_sessions if scope_start is not None and scope_end is not None and scope_start <= item <= scope_end]
	return CoveragePlan(
		request, boundary, calendar.version, tuple(updates), query,
		discovery.latest_finalized_session, discovery.latest_expected_published_session, (),
		observed[0] if observed else None, observed[-1] if observed else None,
		False, request.end > cutoff.astimezone(calendar.timezone).date(),
	)


def validate_database(source: SourceConfig, settings: ValidationSettings, boundary: HistoryBoundary | None, calendar: SessionCalendar, repository: ValidationRepository, clock: Clock, *, openings: OpeningRepository | None = None, discovery: CoverageDiscoveryResult | None = None) -> ValidationResult:
	if settings.features is not None and (settings.features.adjustment is not source.identity.adjustment or settings.features.interval is not source.identity.interval):
		raise ValueError("Validation feature specification and source differ")
	cutoff = decision_cutoff(clock, settings.as_of)
	if discovery is None:
		if settings.start is None or settings.end is None:
			raise ValueError("Validation bounds are required without coverage discovery")
		request = MarketDataRequest(source.identity, settings.start, settings.end, cutoff)
		plan = plan_coverage(source, request, boundary, calendar, settings.publication, _NoStoredSessions(), clock)
	else:
		plan = _plan_from_discovery(source, settings, discovery, calendar, cutoff)
	findings: list[ValidationFinding] = []
	if discovery is not None and (discovery.effective_start is None or discovery.effective_end is None):
		findings.append(ValidationFinding(FindingCategory.UNVERIFIED, ValidationCheck.HISTORY, "Effective retained validation scope is unresolved; local observations were not promoted to an earliest-source claim"))
	scope = dependency_scope(plan, calendar, settings.publication, cutoff)
	scope = replace(scope, query=feature_query(plan, scope.query, settings.features, calendar, settings.publication))
	inspection = DatabaseInspection(()) if scope.query is None else repository.inspect(scope.query)
	allowed = set(() if scope.query is None else scope.query.sessions)
	if any(snapshot.session not in allowed for snapshot in inspection.snapshots) or len({snapshot.session for snapshot in inspection.snapshots}) != len(inspection.snapshots) or len({snapshot.date_id for snapshot in inspection.snapshots}) != len(inspection.snapshots):
		raise ValueError("Validation repository returned duplicate or out-of-scope snapshots")
	requested_dates = {item.session.session_date for item in plan.sessions}
	findings.extend(inspection.findings)
	snapshots = inspection.snapshots
	observed = sorted(snapshot.session.session_date for snapshot in snapshots if snapshot.records and snapshot.session.session_date in requested_dates)
	plan = replace(plan, observed_first_in_scope=observed[0] if observed else None, observed_last_in_scope=observed[-1] if observed else None)
	eligible = tuple(snapshot for snapshot in snapshots if snapshot.receipt is not None and snapshot.receipt.available_at <= cutoff)
	index = SnapshotIndex(eligible)
	by_date = {snapshot.session.session_date: snapshot for snapshot in snapshots}
	store = ManifestStore(source.data_root)
	manifests: list[SourceManifest] = []
	for item in plan.sessions:
		manifest = None
		if item.session.closes_at <= cutoff and (item.expected_available_at is None or item.expected_available_at <= cutoff):
			try:
				manifest = store.load(source.identity, item.session)
			except InvalidManifest:
				findings.append(ValidationFinding(FindingCategory.UNVERIFIED, ValidationCheck.SOURCE, "Source manifest or response content is corrupt", item.session.session_date))
			if manifest is not None:
				manifests.append(manifest)
		findings.extend(assess_session(source.identity, item, manifest, by_date.get(item.session.session_date), index, cutoff))
	findings.extend(boundary_findings(plan, snapshots, index, calendar, settings.publication))
	for session in (() if scope.query is None else scope.query.sessions):
		if session.session_date in requested_dates:
			continue
		try:
			context_manifest = store.load(source.identity, session)
		except InvalidManifest:
			context_manifest = None
		if context_manifest is not None:
			manifests.append(context_manifest)
	opening_repository = openings or OpeningStore(source.data_root)
	if settings.features is not None and settings.features.signal_mode is SignalMode.ENTRY_OPEN:
		findings.extend(validate_opening_coverage(settings.features, plan.boundary, calendar, plan.request.start, plan.request.end, cutoff, opening_repository))
	numerical_findings, samples = validate_close_samples(plan, inspection, tuple(manifests), calendar, settings.publication, settings.features, settings.target, opening_repository)
	findings.extend(numerical_findings)
	for manifest in manifests:
		if manifest.download.available_at <= cutoff and (not manifest.import_complete or not manifest.derived_complete):
			findings.append(ValidationFinding(FindingCategory.INCOMPLETE, ValidationCheck.SOURCE, "Manifest import/derived completion markers are unfinished", manifest.download.session.session_date))
	assessments: list[SessionCoverage] = []
	for item in plan.sessions:
		session_findings = tuple(finding for finding in findings if finding.session_date in {None, item.session.session_date})
		state = coverage_state(session_findings)
		assessments.append(SessionCoverage(item.session, state, "Request-scoped source keys/content and legacy dependencies verified" if not any(finding.blocking for finding in session_findings) else "See typed findings for missing, invalid, incomplete, or unverified evidence"))
	result_findings = tuple(findings)
	repairs = plan_repairs(
		source.identity, plan.request.start, plan.request.end, cutoff, result_findings,
		fetchable_start=None if discovery is None else discovery.fetchable_start,
		fetchable_end=None if discovery is None else discovery.fetchable_end,
		capability_resolved=True if discovery is None else discovery.provider_is_fresh,
	)
	return ValidationResult(plan, CoverageResult(plan.request, tuple(assessments)), result_findings, repairs, snapshots, tuple(manifests), () if scope.query is None else scope.query.sessions, inspection.record_counts, samples, settings.features, discovery)
