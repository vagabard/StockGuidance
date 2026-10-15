from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.validation import DatabaseInspection
from stockguidance.database.repositories.validation_rows import inspect_rows
from stockguidance.database.update_schema import assert_source, assert_transactional_tables
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding


class MySQLValidationRepository:
	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory) -> None:
		self.source, self.connect = source, connect

	def inspect(self, query: SessionQuery) -> DatabaseInspection:
		if query.boundary.source != self.source.identity:
			raise ValueError("Database and validation query source identities differ")
		connection = self.connect(self.source.database_config)
		try:
			cursor = connection.cursor()
			try:
				cursor.execute("START TRANSACTION", ())
				try:
					assert_transactional_tables(cursor)
					assert_source(cursor, self.source)
				except Exception:
					return DatabaseInspection((), (ValidationFinding(FindingCategory.UNVERIFIED, ValidationCheck.SCHEMA, "Updater tables, InnoDB engines, or source binding are missing or incompatible; inspect schema before explicit initialization"),))
				try:
					return inspect_rows(cursor, query)
				except Exception:
					return DatabaseInspection((), (ValidationFinding(FindingCategory.FAILED, ValidationCheck.SCHEMA, "Required database columns or row layouts could not be inspected; review schema and database access"),))
			finally:
				try:
					connection.rollback()
				finally:
					cursor.close()
		finally:
			connection.close()
