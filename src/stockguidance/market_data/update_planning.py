from dataclasses import dataclass
from datetime import datetime

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceIdentity
from stockguidance.database.repositories.market_data import MarketDataRepository, SessionMetadata, SessionSnapshot
from stockguidance.database.repositories.sessions import SessionQuery, StoredSession
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.coverage import CoveragePlan
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.legacy_derived import LEGACY_DERIVED_SPEC
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore, SourceManifest
from stockguidance.market_data.providers.contracts import ResponseState
from stockguidance.market_data.records import DailyBar, records_id
from stockguidance.validation.coverage import CoverageState
from stockguidance.validation.derived import derived_values_match


class SnapshotCoverageReader:
	def __init__(self, repository: MarketDataRepository) -> None:
		self.repository = repository
		self.metadata: tuple[SessionMetadata, ...] = ()
		self.snapshots: tuple[SessionSnapshot, ...] = ()
		self.cleaned_dates: tuple[date, ...] = ()
		self.has_orphans = False

	def read(self, query: SessionQuery) -> tuple[StoredSession, ...]:
		self.cleaned_dates = self.repository.cleanup_placeholders(query)
		self.has_orphans = self.repository.has_orphans(query)
		self.metadata = self.repository.inspect_metadata(query)
		self.snapshots = ()
		return tuple(StoredSession(item.date_id, item.session.session_date, CoverageState.UNVERIFIED, "Source manifests and dependent rows require reconciliation", None if item.receipt is None else item.receipt.available_at) for item in self.metadata)

	def load(self, query: SessionQuery) -> tuple[SessionSnapshot, ...]:
		self.snapshots = self.repository.inspect(query)
		return self.snapshots


@dataclass(frozen=True)
class UpdateWork:
	session: TradingSession
	manifest: SourceManifest | None
	needs_download: bool
	detail: str
	refresh_blocked: bool = False


def _fetch_authorized(plan: CoveragePlan, session: TradingSession) -> bool:
	if plan.provider_capability_state is None:
		return True
	if plan.provider_capability_state != "verified":
		return False
	return (plan.fetchable_start is None or session.session_date >= plan.fetchable_start) and (plan.fetchable_end is None or session.session_date <= plan.fetchable_end)


def plan_updates(plan: CoveragePlan, metadata: tuple[SessionMetadata, ...], store: ManifestStore, correction: CorrectionPolicy, now: datetime, force_refresh_dates: frozenset[date] = frozenset()) -> tuple[UpdateWork, ...]:
	by_date = {item.session.session_date: item for item in metadata}
	work: list[UpdateWork] = []
	for item in plan.sessions:
		if item.state is CoverageState.NOT_YET_AVAILABLE or item.expected_available_at is None:
			continue
		try:
			manifest = store.load(plan.request.source, item.session)
		except InvalidManifest:
			manifest = None
		stored = by_date.get(item.session.session_date)
		needs_download = manifest is None and _fetch_authorized(plan, item.session)
		detail = "No validated request-scoped source evidence"
		refresh_blocked = False
		if manifest is not None:
			response = manifest.download.response
			refresh_due = response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING} or correction.refresh_due(item.session, manifest.download.available_at, now)
			expected_keys = tuple(sorted(bar.ticker for bar in response.records))
			receipt_matches = stored is not None and stored.receipt is not None and stored.receipt.raw_id == records_id(response.records) and stored.raw_count == len(response.records) and stored.raw_keys == expected_keys
			derived_matches = receipt_matches and stored.receipt is not None and stored.receipt.derived_dependency_id is not None and stored.receipt.derived_content_id is not None and stored.derived_count == len(response.records)
			needs_download = refresh_due and _fetch_authorized(plan, item.session)
			refresh_blocked = refresh_due and not needs_download
			detail = "Provider verification or scheduled correction refresh" if needs_download else "Reconcile persisted evidence with database keys and content"
			if refresh_blocked:
				detail = plan.provider_capability_detail or "Scheduled provider refresh is outside current evidenced provider access"
			if manifest.download.available_at > now:
				needs_download = False
				refresh_blocked = False
				detail = "Source evidence is unavailable at the current clock"
			if not refresh_due and receipt_matches and derived_matches and item.session.session_date not in force_refresh_dates:
				continue
		elif not _fetch_authorized(plan, item.session):
			detail = plan.provider_capability_detail or "Provider fetch capability is unresolved; retained local evidence is audit-only"
		if stored is not None and stored.receipt is not None and stored.receipt.available_at > now:
			continue
		work.append(UpdateWork(item.session, manifest, needs_download, detail, refresh_blocked))
	return tuple(work)


class SnapshotIndex:
	def __init__(self, snapshots: tuple[SessionSnapshot, ...]) -> None:
		self.by_sequence = {item.session.sequence: item for item in snapshots if item.raw_verified}
		self.empty_id = records_id(())

	def neighbors(self, snapshot: SessionSnapshot) -> tuple[tuple[DailyBar, ...], tuple[DailyBar, ...]]:
		prior, following = self.by_sequence.get(snapshot.session.sequence - 1), self.by_sequence.get(snapshot.session.sequence + 1)
		return (() if prior is None else prior.records, () if following is None else following.records)

	def dependency(self, snapshot: SessionSnapshot) -> str:
		prior, following = self.by_sequence.get(snapshot.session.sequence - 1), self.by_sequence.get(snapshot.session.sequence + 1)
		return content_id([LEGACY_DERIVED_SPEC, self.empty_id if prior is None else prior.raw_content_id, snapshot.raw_content_id, self.empty_id if following is None else following.raw_content_id])


def reconciled(source: SourceIdentity, manifest: SourceManifest | None, snapshot: SessionSnapshot | None, index: SnapshotIndex, cutoff: datetime) -> bool:
	if manifest is None or snapshot is None or manifest.source != source or manifest.download.available_at > cutoff:
		return False
	if manifest.download.response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING} or snapshot.receipt is None or snapshot.receipt.available_at > cutoff:
		return False
	prior, following = index.neighbors(snapshot)
	return records_id(manifest.download.response.records) == snapshot.raw_content_id and snapshot.derived_verified(index.dependency(snapshot)) and derived_values_match(snapshot, prior, following)
