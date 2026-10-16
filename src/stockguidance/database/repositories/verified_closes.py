from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.close_projection import project_closes
from stockguidance.database.repositories.closes import CloseData, CloseQuery
from stockguidance.database.repositories.validation import DatabaseInspection, ValidationRepository
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore


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
		store = ManifestStore(self.source.data_root)
		manifests = []
		for session in query.scope.sessions:
			try:
				manifest = store.load(self.source.identity, session)
			except InvalidManifest:
				manifest = None
			if manifest is not None:
				manifests.append(manifest)
		return project_closes(query, inspection, tuple(manifests)), inspection
