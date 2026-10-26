from stockguidance.database.repositories.closes import CloseData, CloseQuery
from stockguidance.database.repositories.validation import DatabaseInspection
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.closes import CloseIssue, CloseObservation, PriceState
from stockguidance.market_data.manifests import SourceManifest
from stockguidance.market_data.providers.contracts import ResponseState
from stockguidance.market_data.records import records_id
from stockguidance.validation.findings import ValidationCheck
from stockguidance.market_data.quality import QualityFlag


def project_closes(query: CloseQuery, inspection: DatabaseInspection, manifests: tuple[SourceManifest, ...], *, trust_database: bool = False) -> CloseData:
	snapshots = {snapshot.session: snapshot for snapshot in inspection.snapshots}
	by_session = {manifest.download.session: manifest for manifest in manifests}
	symbols = dict(inspection.ticker_symbols)
	observations: list[CloseObservation] = []
	issues: list[CloseIssue] = []
	for session in query.scope.sessions:
		snapshot, manifest = snapshots.get(session), by_session.get(session)
		bars = {} if snapshot is None else {bar.ticker: bar for bar in snapshot.records}
		for ticker_id in query.ticker_ids:
			symbol = symbols.get(ticker_id)
			quality = None if snapshot is None else next((record for record in snapshot.quality if record.observation.ticker == symbol), None)
			if quality is not None and snapshot is not None:
				values, flags = quality.usable(query.quality_policy, query.scope.as_of)
				if values is not None and snapshot.receipt is not None and snapshot.raw_verified:
					available = max(snapshot.receipt.available_at, quality.available_at or quality.observation.available_at)
					if flags & QualityFlag.FUTURE_LEAKAGE:
						available = max(snapshot.receipt.available_at, quality.observation.available_at)
					observations.append(CloseObservation(ticker_id, float(str(values[3])), ObservationTiming(session, session.closes_at, available), quality.revision, flags, quality.gap_length, (quality.revision,)))
				else:
					issues.append(CloseIssue(ticker_id, session, PriceState.INVALID, "Bad or interpolated observation is excluded by the selected quality/availability policy", flags, quality.gap_length, (quality.revision,)))
				continue
			state, detail = PriceState.UNVERIFIED, "Request-scoped source or raw import evidence is unverified"
			if any(finding.blocking and finding.session_date in {None, session.session_date} and finding.check in {ValidationCheck.SCHEMA, ValidationCheck.DUPLICATE, ValidationCheck.ORPHAN, ValidationCheck.RAW_VALUES} for finding in inspection.findings):
				state, detail = PriceState.INVALID, "Raw records or mapping integrity is invalid"
			elif snapshot is None:
				state, detail = PriceState.MISSING, "Expected session is missing from storage"
			elif (snapshot.receipt is not None and snapshot.receipt.available_at > query.scope.as_of) or (manifest is not None and manifest.download.available_at > query.scope.as_of):
				state, detail = PriceState.NOT_YET_AVAILABLE, "Actual source/import receipt is after the cutoff"
			elif snapshot.raw_verified and snapshot.receipt is not None and (trust_database or (manifest is not None and manifest.source == query.scope.boundary.source and manifest.download.response.state in {ResponseState.COMPLETE, ResponseState.NO_TRADING} and records_id(manifest.download.response.records) == (snapshot.original_content_id or snapshot.raw_content_id))):
				if symbol is not None and symbol in bars:
					available_at = snapshot.receipt.available_at if manifest is None else max(snapshot.receipt.available_at, manifest.download.available_at)
					observations.append(CloseObservation(ticker_id, float(bars[symbol].close), ObservationTiming(session, session.closes_at, available_at), snapshot.raw_content_id))
					continue
				state, detail = PriceState.EXPECTED_ABSENCE, "Ticker absent from a verified provider response; no synthetic price or lifetime is inferred"
			issues.append(CloseIssue(ticker_id, session, state, detail))
	return CloseData(query, tuple(observations), tuple(issues))
