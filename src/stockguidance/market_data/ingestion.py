from datetime import date

from stockguidance.database.repositories.market_data import DerivedUpdate, MarketDataRepository, SessionSnapshot
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.legacy_derived import legacy_rows
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.market_data.records import records_id
from stockguidance.market_data.update_planning import SnapshotIndex


def import_if_needed(repository: MarketDataRepository, query: SessionQuery, download: DownloadedSession, snapshot: SessionSnapshot | None) -> None:
	if snapshot is None or not snapshot.raw_verified or records_id(snapshot.records) != records_id(download.response.records):
		repository.import_session(query, download)


def derived_repairs(snapshots: tuple[SessionSnapshot, ...], repair_dates: frozenset[date]) -> tuple[DerivedUpdate, ...]:
	updates: list[DerivedUpdate] = []
	index = SnapshotIndex(snapshots)
	for snapshot in snapshots:
		if not snapshot.raw_verified or snapshot.session.session_date not in repair_dates:
			continue
		dependency = index.dependency(snapshot)
		if not snapshot.derived_verified(dependency):
			prior, following = index.neighbors(snapshot)
			updates.append(DerivedUpdate(snapshot, legacy_rows(prior, snapshot.records, following), dependency))
	return tuple(updates)
