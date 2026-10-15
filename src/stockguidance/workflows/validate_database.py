from dataclasses import dataclass, replace
from datetime import date, datetime

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_aware_datetime, require_date
from stockguidance.database.repositories.sessions import SessionQuery, StoredSession
from stockguidance.database.repositories.validation import DatabaseInspection, ValidationRepository
from stockguidance.market_data.coverage_planning import plan_coverage
from stockguidance.market_data.dependencies import dependency_scope
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore, SourceManifest
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.market_data.update_planning import SnapshotIndex
from stockguidance.validation.assessment import assess_session, coverage_state
from stockguidance.validation.boundaries import boundary_findings
from stockguidance.validation.coverage import CoverageResult, SessionCoverage
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding
from stockguidance.validation.repair import plan_repairs
from stockguidance.validation.result import ValidationResult


@dataclass(frozen=True)
class ValidationSettings:
	start: date
	end: date
	publication: DailyPublicationPolicy
	as_of: datetime | None = None

	def __post_init__(self) -> None:
		require_date(self.start, "validation start")
		require_date(self.end, "validation end")
		if self.start > self.end or not isinstance(self.publication, DailyPublicationPolicy):
			raise ValueError("Invalid validation bounds or publication policy")
		if self.as_of is not None:
			require_aware_datetime(self.as_of, "validation cutoff")


class _NoStoredSessions:
	def read(self, query: SessionQuery) -> tuple[StoredSession, ...]:
		return ()


def validate_database(source: SourceConfig, settings: ValidationSettings, boundary: HistoryBoundary | None, calendar: SessionCalendar, repository: ValidationRepository, clock: Clock) -> ValidationResult:
	cutoff = decision_cutoff(clock, settings.as_of)
	request = MarketDataRequest(source.identity, settings.start, settings.end, cutoff)
	plan = plan_coverage(source, request, boundary, calendar, settings.publication, _NoStoredSessions(), clock)
	scope = dependency_scope(plan, calendar, settings.publication, cutoff)
	inspection = DatabaseInspection(()) if scope.query is None else repository.inspect(scope.query)
	allowed = set(() if scope.query is None else scope.query.sessions)
	if any(snapshot.session not in allowed for snapshot in inspection.snapshots) or len({snapshot.session for snapshot in inspection.snapshots}) != len(inspection.snapshots) or len({snapshot.date_id for snapshot in inspection.snapshots}) != len(inspection.snapshots):
		raise ValueError("Validation repository returned duplicate or out-of-scope snapshots")
	requested_dates = {item.session.session_date for item in plan.sessions}
	findings = list(inspection.findings)
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
		if item.expected_available_at is not None and item.expected_available_at <= cutoff and item.session.closes_at <= cutoff:
			try:
				manifest = store.load(source.identity, item.session)
			except InvalidManifest:
				findings.append(ValidationFinding(FindingCategory.UNVERIFIED, ValidationCheck.SOURCE, "Source manifest or response content is corrupt", item.session.session_date))
			if manifest is not None:
				manifests.append(manifest)
		findings.extend(assess_session(source.identity, item, manifest, by_date.get(item.session.session_date), index, cutoff))
	findings.extend(boundary_findings(plan, snapshots, index, calendar, settings.publication))
	findings.append(ValidationFinding(FindingCategory.NOT_ASSESSED, ValidationCheck.TARGET, "Numerical close-to-close targets and previous-close model features require Stage 5; derived checks use legacy-open-to-close-v1 only"))
	for manifest in manifests:
		if manifest.download.available_at <= cutoff and (not manifest.import_complete or not manifest.derived_complete):
			findings.append(ValidationFinding(FindingCategory.INCOMPLETE, ValidationCheck.SOURCE, "Manifest import/derived completion markers are unfinished", manifest.download.session.session_date))
	assessments: list[SessionCoverage] = []
	for item in plan.sessions:
		session_findings = tuple(finding for finding in findings if finding.session_date in {None, item.session.session_date} and finding.check is not ValidationCheck.TARGET)
		state = coverage_state(session_findings)
		assessments.append(SessionCoverage(item.session, state, "Request-scoped source keys/content and legacy dependencies verified" if not any(finding.blocking for finding in session_findings) else "See typed findings for missing, invalid, incomplete, or unverified evidence"))
	result_findings = tuple(findings)
	return ValidationResult(plan, CoverageResult(request, tuple(assessments)), result_findings, plan_repairs(source.identity, settings.start, settings.end, cutoff, result_findings), snapshots, tuple(manifests), () if scope.query is None else scope.query.sessions, inspection.record_counts)
