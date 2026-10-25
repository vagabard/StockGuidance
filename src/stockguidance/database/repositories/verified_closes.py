from stockguidance.config.source import PriceAdjustment, SourceConfig
from stockguidance.database.repositories.close_projection import project_closes
from stockguidance.database.repositories.closes import CloseData, CloseQuery
from stockguidance.database.repositories.validation import DatabaseInspection, ValidationRepository
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore
from stockguidance.market_data.canonical_store import archive_store
from stockguidance.market_data.securities import security_metadata, is_test_security
from stockguidance.market_data.closes import CloseIssue, PriceState


class VerifiedCloseRepository:
	"""Read-only projection of inspected raw data and request-scoped source evidence."""

	def __init__(self, source: SourceConfig, repository: ValidationRepository) -> None:
		self.source, self.repository = source, repository

	def read(self, query: CloseQuery) -> CloseData:
		return self.inspect(query)[0]

	def inspect(self, query: CloseQuery) -> tuple[CloseData, DatabaseInspection]:
		if query.scope.boundary.source != self.source.identity:
			raise ValueError("Close repository and request sources differ")
		inspection = self.repository.inspect(query.scope)
		allowed = set(query.scope.sessions)
		if any(snapshot.session not in allowed for snapshot in inspection.snapshots) or len({snapshot.session for snapshot in inspection.snapshots}) != len(inspection.snapshots):
			raise ValueError("Close repository returned duplicate or out-of-scope sessions")
		connect = getattr(self.repository, "connect", None)
		store = ManifestStore(self.source.data_root) if connect is None else archive_store(self.source, connect)
		manifests = []
		trust_database = self.source.identity.adjustment is PriceAdjustment.UNADJUSTED
		for session in (() if trust_database else query.scope.sessions):
			try:
				manifest = store.load(self.source.identity, session)
			except InvalidManifest:
				manifest = None
			if manifest is not None:
				manifests.append(manifest)
		data = project_closes(query, inspection, tuple(manifests), trust_database=trust_database)
		metadata = security_metadata(self.source.data_root)
		symbols = dict(inspection.ticker_symbols)
		excluded = tuple(item for item in data.observations if is_test_security(metadata, symbols.get(item.ticker_id, ""), item.timing.session.session_date))
		if excluded:
			data = CloseData(query, tuple(item for item in data.observations if item not in excluded),
				data.issues + tuple(CloseIssue(item.ticker_id, item.timing.session, PriceState.INVALID, "Verified exchange test security is excluded from trading and training") for item in excluded))
		return data, inspection
