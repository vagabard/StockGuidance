from dataclasses import dataclass
from datetime import datetime

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceIdentity
from stockguidance.database.repositories.market_data import MarketDataRepository, SessionSnapshot
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
		self.snapshots: tuple[SessionSnapshot, ...] = ()

	def read(self, query: SessionQuery) -> tuple[StoredSession, ...]:
		self.snapshots = self.repository.inspect(query)
		return tuple(StoredSession(snapshot.date_id, snapshot.session.session_date, CoverageState.UNVERIFIED, "Source manifests and dependent rows require reconciliation", None if snapshot.receipt is None else snapshot.receipt.available_at) for snapshot in self.snapshots)


@dataclass(frozen=True)
class UpdateWork:
	session: TradingSession
	manifest: SourceManifest | None
	needs_download: bool
	detail: str


def plan_updates(plan: CoveragePlan, snapshots: tuple[SessionSnapshot, ...], store: ManifestStore, correction: CorrectionPolicy, now: datetime) -> tuple[UpdateWork, ...]:
	by_date = {snapshot.session.session_date: snapshot for snapshot in snapshots}
	work: list[UpdateWork] = []
	for item in plan.sessions:
		if item.state is CoverageState.NOT_YET_AVAILABLE or item.expected_available_at is None:
			continue
		try:
			manifest = store.load(plan.request.source, item.session)
		except InvalidManifest:
			manifest = None
		snapshot = by_date.get(item.session.session_date)
		needs_download = manifest is None
		detail = "No validated request-scoped source evidence"
		if manifest is not None:
			response = manifest.download.response
			needs_download = response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING} or correction.refresh_due(item.session, manifest.download.available_at, now)
			detail = "Provider verification or scheduled correction refresh" if needs_download else "Reconcile persisted evidence with database keys and content"
			if manifest.download.available_at > now:
				needs_download = False
				detail = "Source evidence is unavailable at the current clock"
		if snapshot is not None and snapshot.receipt is not None and snapshot.receipt.available_at > now:
			continue
		work.append(UpdateWork(item.session, manifest, needs_download, detail))
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
