from dataclasses import dataclass
from time import perf_counter
from datetime import date, datetime, timedelta

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_date
from stockguidance.database.repositories.market_data import MarketDataRepository
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.coverage_planning import plan_coverage
from stockguidance.market_data.dependencies import dependency_scope
from stockguidance.market_data.discovery import CoverageDiscoveryResult
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.identity import content_id, data_configuration_key
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
from stockguidance.workflows.progress import ProgressReporter


_FULL_VERIFY_SESSION_LIMIT = 32


@dataclass(frozen=True)
class UpdateSettings:
	start: date
	end: date
	publication: DailyPublicationPolicy
	corrections: CorrectionPolicy
	repair_plan: RepairPlan | None = None
	discovery: CoverageDiscoveryResult | None = None

	def __post_init__(self) -> None:
		require_date(self.start, "update start")
		require_date(self.end, "update end")
		if self.start > self.end or not isinstance(self.publication, DailyPublicationPolicy) or not isinstance(self.corrections, CorrectionPolicy):
			raise ValueError("Invalid update bounds or policies")
		if self.repair_plan is not None and not isinstance(self.repair_plan, RepairPlan):
			raise ValueError("Update repair plan must be a typed validation plan")
		if self.discovery is not None and not isinstance(self.discovery, CoverageDiscoveryResult):
			raise ValueError("Update discovery must be a typed coverage result")


def _failure(code: FailureCode, session_date: date | None = None) -> StageFailure:
	suffix = "" if session_date is None else f" for {session_date.isoformat()}"
	return StageFailure(WorkflowStage.REFRESH, code, "Required market-data reconciliation failed" + suffix, True)


def update_market_data(source: SourceConfig, settings: UpdateSettings, boundary: HistoryBoundary | None, calendar: SessionCalendar, provider: MarketDataProvider, repository: MarketDataRepository, clock: Clock, *, reporter: ProgressReporter | None = None) -> UpdateResult:
	"""Reconcile a live snapshot; historical cutoffs belong to read-only planners."""
	started = decision_cutoff(clock)
	reporter = reporter or ProgressReporter()
	capability = settings.discovery
	capability_resolved = capability is None or capability.provider_is_fresh
	if settings.repair_plan is not None:
		settings.repair_plan.validate_for_update(
			source.identity, settings.start, settings.end, started,
			fetchable_start=None if capability is None else capability.fetchable_start,
			fetchable_end=None if capability is None else capability.fetchable_end,
			capability_resolved=capability_resolved,
		)
	request = MarketDataRequest(source.identity, settings.start, settings.end, started)
	reader = SnapshotCoverageReader(repository)
	try:
		plan = reporter.measure("plan coverage and read session metadata", lambda: plan_coverage(
			source, request, boundary, calendar, settings.publication, reader, clock,
			fetchable_start=None if capability is None else capability.fetchable_start,
			fetchable_end=None if capability is None else capability.fetchable_end,
			capability_resolved=capability_resolved,
			capability_state=None if capability is None else capability.provider.state.value,
			capability_detail=None if capability is None else capability.provider.detail,
		))
	except ValueError:
		raise StageExecutionError(_failure(FailureCode.INVALID_REQUEST)) from None
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	store = ManifestStore(source.data_root)
	failures: list[StageFailure] = []
	failed_dates: set[date] = set()
	stale_refreshes: set[date] = set()
	downloaded = 0
	if reader.has_orphans:
		failures.append(_failure(FailureCode.STORAGE))
		failed_dates.update(item.session.session_date for item in plan.sessions)
	try:
		force_refresh_dates = frozenset(repair.session_date for repair in settings.repair_plan.items if repair.action is RepairAction.REFRESH_SOURCE) if settings.repair_plan is not None else frozenset()
		work = reporter.measure("build update plan", lambda: plan_updates(plan, reader.metadata, store, settings.corrections, started, force_refresh_dates))
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	target_query = _target_query(plan.boundary, plan.query, tuple(item.session for item in work), started)
	try:
		target_snapshots = () if target_query is None else reporter.measure("read targeted session records", lambda: repository.inspect(target_query))
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	by_date = {snapshot.session.session_date: snapshot for snapshot in target_snapshots}
	session_started = perf_counter()
	for index, item in enumerate(work, 1):
		try:
			if item.refresh_blocked:
				failures.append(_failure(FailureCode.PROVIDER, item.session.session_date))
				failed_dates.add(item.session.session_date)
				stale_refreshes.add(item.session.session_date)
				if item.manifest is None or item.manifest.download.response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING}:
					continue
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
		if reporter.enabled and (index == 1 or index == len(work) or index % 25 == 0):
			reporter.sessions(index, len(work), downloaded=downloaded, failures=len(failures), started=session_started)
	scope = dependency_scope(plan, calendar, settings.publication, decision_cutoff(clock))
	affected_dates = {item.session.session_date for item in work} | failed_dates
	final_query = _dependency_query(plan.boundary, scope.query, affected_dates, decision_cutoff(clock))
	full_verify = len(plan.sessions) <= _FULL_VERIFY_SESSION_LIMIT
	if full_verify and scope.query is not None:
		final_query = scope.query
	try:
		snapshots = () if final_query is None else reporter.measure("read affected records and dependencies", lambda: repository.inspect(final_query))
		eligible = tuple(snapshot for snapshot in snapshots if snapshot.receipt is not None and snapshot.receipt.available_at <= clock.now())
		affected_sequences = {session.sequence for session in scope.query.sessions if session.session_date in affected_dates} if scope.query is not None else set()
		repair_dates = scope.repair_dates if full_verify and not affected_dates else ({session.session_date for session in scope.query.sessions if any(abs(session.sequence - sequence) <= 1 for sequence in affected_sequences)} if scope.query is not None else set())
		for repair in derived_repairs(eligible, repair_dates):
			try:
				if final_query is not None:
					repository.replace_derived(final_query, repair)
			except Exception:
				failures.append(_failure(FailureCode.STORAGE, repair.snapshot.session.session_date))
				failed_dates.add(repair.snapshot.session.session_date)
		snapshots = () if final_query is None else reporter.measure("reload affected records and dependencies", lambda: repository.inspect(final_query))
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	completed = decision_cutoff(clock)
	final_request = MarketDataRequest(source.identity, request.start, request.end, completed)
	try:
		final_reader = SnapshotCoverageReader(repository)
		final_plan = plan_coverage(
			source, final_request, boundary, calendar, settings.publication, final_reader, clock,
			fetchable_start=None if capability is None else capability.fetchable_start,
			fetchable_end=None if capability is None else capability.fetchable_end,
			capability_resolved=capability_resolved,
			capability_state=None if capability is None else capability.provider.state.value,
			capability_detail=None if capability is None else capability.provider.detail,
		)
	except Exception:
		raise StageExecutionError(_failure(FailureCode.STORAGE)) from None
	combined = {snapshot.session.session_date: snapshot for snapshot in snapshots}
	combined.update((snapshot.session.session_date, snapshot) for snapshot in final_reader.snapshots)
	snapshots = tuple(combined[key] for key in sorted(combined))
	try:
		integrity = () if final_query is None else reporter.measure("check affected database integrity", lambda: repository.inspect_integrity(final_query))
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
	metadata_by_date = {item.session.session_date: item for item in final_reader.metadata}
	for assessment in final_plan.sessions:
		state, detail = assessment.state, assessment.detail
		if assessment.expected_available_at is not None and assessment.expected_available_at <= completed and assessment.session.closes_at <= completed:
			try:
				manifest = store.load(source.identity, assessment.session)
				snapshot = by_date.get(assessment.session.session_date)
				metadata = metadata_by_date.get(assessment.session.session_date)
				if assessment.session.session_date not in affected_dates and _trusted_complete(source, assessment, manifest, metadata, completed):
					findings = ()
				else:
					findings = assess_session(source.identity, assessment, manifest, snapshot, index, completed)
				findings += tuple(finding for finding in integrity if finding.session_date in {None, assessment.session.session_date})
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
	revision_rows = []
	for item in final_plan.sessions:
		session_date = item.session.session_date
		snapshot = by_date.get(session_date)
		metadata = metadata_by_date.get(session_date)
		if snapshot is not None:
			revision_rows.append((session_date.isoformat(), records_id(snapshot.records), derived_id(snapshot.derived), None if snapshot.receipt is None else snapshot.receipt.derived_dependency_id))
		elif metadata is not None and metadata.receipt is not None:
			revision_rows.append((session_date.isoformat(), metadata.receipt.raw_id, metadata.receipt.derived_content_id, metadata.receipt.derived_dependency_id))
	revision = content_id([data_configuration_key(source.data_configuration), LEGACY_DERIVED_SPEC, revision_rows])
	result = UpdateResult(
		plan, CoverageResult(final_request, tuple(assessments)), tuple(manifests), closed, revision, tuple(failures), downloaded,
		capability, tuple(sorted(stale_refreshes)),
	)
	try:
		reporter.measure("persist update result", lambda: persist_update_result(source, settings.publication, settings.corrections, result))
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


def _target_query(boundary: HistoryBoundary, scope: SessionQuery | None, sessions: tuple[TradingSession, ...], cutoff: datetime) -> SessionQuery | None:
	if scope is None or not sessions:
		return None
	target_dates = {session.session_date for session in sessions}
	selected = tuple(session for session in scope.sessions if session.session_date in target_dates)
	return None if not selected else SessionQuery(boundary, selected, cutoff)


def _dependency_query(boundary: HistoryBoundary, scope: SessionQuery | None, affected_dates: set[date], cutoff: datetime) -> SessionQuery | None:
	if scope is None or not affected_dates:
		return None
	affected_sequences = {session.sequence for session in scope.sessions if session.session_date in affected_dates}
	selected = tuple(session for session in scope.sessions if any(abs(session.sequence - sequence) <= 2 for sequence in affected_sequences))
	return None if not selected else SessionQuery(boundary, selected, cutoff)


def _trusted_complete(source: SourceConfig, item, manifest: SourceManifest | None, metadata, cutoff: datetime) -> bool:
	if manifest is None or metadata is None or metadata.receipt is None or manifest.source != source.identity or manifest.download.session != item.session:
		return False
	if manifest.download.available_at > cutoff or metadata.receipt.available_at > cutoff:
		return False
	if manifest.download.response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING}:
		return False
	expected_keys = tuple(sorted(bar.ticker for bar in manifest.download.response.records))
	return metadata.receipt.raw_id == records_id(manifest.download.response.records) and metadata.raw_count == len(manifest.download.response.records) and metadata.raw_keys == expected_keys and metadata.derived_count == len(manifest.download.response.records) and metadata.receipt.derived_dependency_id is not None and metadata.receipt.derived_content_id is not None
