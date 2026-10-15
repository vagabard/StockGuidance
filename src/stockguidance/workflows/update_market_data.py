from dataclasses import dataclass
from datetime import date, timedelta

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_date
from stockguidance.database.repositories.market_data import MarketDataRepository
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.coverage_planning import plan_coverage
from stockguidance.market_data.dependencies import dependency_scope
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.identity import content_id, source_key
from stockguidance.market_data.ingestion import derived_repairs, import_if_needed
from stockguidance.market_data.legacy_derived import LEGACY_DERIVED_SPEC, derived_id
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore, SourceManifest
from stockguidance.market_data.providers.contracts import DownloadedSession, MarketDataProvider, ResponseState
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.records import records_id
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.market_data.update_planning import SnapshotCoverageReader, SnapshotIndex, plan_updates
from stockguidance.validation.assessment import assess_session, coverage_state
from stockguidance.validation.coverage import CoverageResult, CoverageState, SessionCoverage
from stockguidance.validation.repair import RepairAction, RepairPlan
from stockguidance.workflows.failures import FailureCode, StageExecutionError, StageFailure, WorkflowStage
from stockguidance.workflows.update_result import UpdateResult
from stockguidance.workflows.update_metadata import persist_update_result


@dataclass(frozen=True)
class UpdateSettings:
	start: date
	end: date
	publication: DailyPublicationPolicy
	corrections: CorrectionPolicy
	repair_plan: RepairPlan | None = None

	def __post_init__(self) -> None:
		require_date(self.start, "update start")
		require_date(self.end, "update end")
		if self.start > self.end or not isinstance(self.publication, DailyPublicationPolicy) or not isinstance(self.corrections, CorrectionPolicy):
			raise ValueError("Invalid update bounds or policies")
		if self.repair_plan is not None and not isinstance(self.repair_plan, RepairPlan):
			raise ValueError("Update repair plan must be a typed validation plan")


def _failure(code: FailureCode, session_date: date | None = None) -> StageFailure:
	suffix = "" if session_date is None else f" for {session_date.isoformat()}"
	return StageFailure(WorkflowStage.REFRESH, code, "Required market-data reconciliation failed" + suffix, True)


def update_market_data(source: SourceConfig, settings: UpdateSettings, boundary: HistoryBoundary | None, calendar: SessionCalendar, provider: MarketDataProvider, repository: MarketDataRepository, clock: Clock) -> UpdateResult:
	"""Reconcile a live snapshot; historical cutoffs belong to read-only planners."""
	started = decision_cutoff(clock)
	if settings.repair_plan is not None:
		settings.repair_plan.validate_for_update(source.identity, settings.start, settings.end, started)
	request = MarketDataRequest(source.identity, settings.start, settings.end, started)
	reader = SnapshotCoverageReader(repository)
	try:
		plan = plan_coverage(source, request, boundary, calendar, settings.publication, reader, clock)
	except ValueError:
		raise StageExecutionError(_failure(FailureCode.INVALID_REQUEST)) from None
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	store = ManifestStore(source.data_root)
	failures: list[StageFailure] = []
	failed_dates: set[date] = set()
	downloaded = 0
	try:
		work = plan_updates(plan, reader.snapshots, store, settings.corrections, started)
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	by_date = {snapshot.session.session_date: snapshot for snapshot in reader.snapshots}
	for item in work:
		try:
			manifest = item.manifest
			force_refresh = settings.repair_plan is not None and any(repair.action is RepairAction.REFRESH_SOURCE and repair.session_date == item.session.session_date for repair in settings.repair_plan.items)
			if item.needs_download or force_refresh:
				try:
					response = provider.fetch(source, item.session)
				except Exception:
					raise StageExecutionError(_failure(FailureCode.PROVIDER, item.session.session_date)) from None
				manifest = store.save_download(source.identity, DownloadedSession(item.session, response, decision_cutoff(clock)))
				downloaded += 1
			if manifest is None or manifest.download.available_at > clock.now() or manifest.download.response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING}:
				continue
			query = SessionQuery(plan.boundary, plan.query.sessions, decision_cutoff(clock)) if plan.query is not None else None
			if query is not None:
				import_if_needed(repository, query, manifest.download, by_date.get(item.session.session_date))
				store.mark_imported(manifest)
		except Exception as error:
			failures.append(error.failure if isinstance(error, StageExecutionError) else _failure(FailureCode.STORAGE, item.session.session_date))
			failed_dates.add(item.session.session_date)
	scope = dependency_scope(plan, calendar, settings.publication, decision_cutoff(clock))
	final_query = scope.query
	try:
		snapshots = () if final_query is None else repository.inspect(final_query)
		eligible = tuple(snapshot for snapshot in snapshots if snapshot.receipt is not None and snapshot.receipt.available_at <= clock.now())
		for repair in derived_repairs(eligible, scope.repair_dates):
			try:
				if final_query is not None:
					repository.replace_derived(final_query, repair)
			except Exception:
				failures.append(_failure(FailureCode.STORAGE, repair.snapshot.session.session_date))
				failed_dates.add(repair.snapshot.session.session_date)
		snapshots = () if final_query is None else repository.inspect(final_query)
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	completed = decision_cutoff(clock)
	final_request = MarketDataRequest(source.identity, request.start, request.end, completed)
	try:
		final_reader = SnapshotCoverageReader(repository)
		final_plan = plan_coverage(source, final_request, boundary, calendar, settings.publication, final_reader, clock)
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	combined = {snapshot.session.session_date: snapshot for snapshot in snapshots}
	combined.update((snapshot.session.session_date, snapshot) for snapshot in final_reader.snapshots)
	snapshots = tuple(combined[key] for key in sorted(combined))
	try:
		integrity_query = dependency_scope(final_plan, calendar, settings.publication, completed).query
		integrity = () if integrity_query is None else repository.inspect_integrity(integrity_query)
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	requested_dates = {item.session.session_date for item in final_plan.sessions}
	if any(finding.blocking and finding.session_date not in requested_dates for finding in integrity):
		failures.append(_failure(FailureCode.STORAGE))
	eligible = tuple(snapshot for snapshot in snapshots if snapshot.receipt is not None and snapshot.receipt.available_at <= completed)
	index = SnapshotIndex(eligible)
	assessments: list[SessionCoverage] = []
	manifests: list[SourceManifest] = []
	by_date = {snapshot.session.session_date: snapshot for snapshot in snapshots}
	for assessment in final_plan.sessions:
		state, detail = assessment.state, assessment.detail
		if assessment.expected_available_at is not None and assessment.expected_available_at <= completed and assessment.session.closes_at <= completed:
			try:
				manifest = store.load(source.identity, assessment.session)
				snapshot = by_date.get(assessment.session.session_date)
				findings = assess_session(source.identity, assessment, manifest, snapshot, index, completed) + tuple(finding for finding in integrity if finding.session_date in {None, assessment.session.session_date})
				if assessment.session.session_date not in failed_dates and not findings:
					state, detail = CoverageState.COMPLETE, "Request-scoped source keys/content, raw import, and legacy derived dependencies verified"
					if manifest is not None:
						manifests.append(store.mark_complete(manifest))
				else:
					state, detail = coverage_state(findings) if findings else CoverageState.INCOMPLETE, "Required source/import/derived evidence is absent, unavailable, or failed"
					if manifest is not None:
						manifests.append(manifest)
			except InvalidManifest:
				state, detail = CoverageState.UNVERIFIED, "Source manifest or downloaded content is corrupt"
			except Exception:
				failures.append(_failure(FailureCode.STORAGE, assessment.session.session_date))
				state, detail = CoverageState.INCOMPLETE, "Could not persist verified completion"
		assessments.append(SessionCoverage(assessment.session, state, detail))
	closed = _closed_dates(request.start, min(request.end, completed.astimezone(calendar.timezone).date()), plan.boundary.history_start, {item.session.session_date for item in final_plan.sessions})
	revision = content_id([source_key(source.identity), LEGACY_DERIVED_SPEC, [(snapshot.session.session_date.isoformat(), records_id(snapshot.records), derived_id(snapshot.derived), None if snapshot.receipt is None else snapshot.receipt.derived_dependency_id) for snapshot in snapshots]])
	result = UpdateResult(plan, CoverageResult(final_request, tuple(assessments)), tuple(manifests), closed, revision, tuple(failures), downloaded)
	try:
		persist_update_result(source, settings.publication, settings.corrections, result)
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	return result


def _closed_dates(start: date, end: date, history_start: date, sessions: set[date]) -> tuple[date, ...]:
	current = max(start, history_start)
	closed: list[date] = []
	while current <= end:
		if current not in sessions:
			closed.append(current)
		current += timedelta(days=1)
	return tuple(closed)
