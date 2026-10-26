from collections.abc import Iterable
from datetime import date
import warnings
from typing import Any

from stockguidance.config.source import SourceConfig
from stockguidance.database.validity import DataValidityNotice, DataValidityWarning, enforce_minimum_valid_session
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.validation import DatabaseInspection
from stockguidance.database.repositories.validation_rows import inspect_rows
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.update_schema import assert_source, assert_transactional_tables
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding


class MySQLValidationRepository:
	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None, guardian_windows: tuple[int, ...] | None = None) -> None:
		self.source, self.connect, self.guardian_windows = source, connect, guardian_windows
		self.table_names = table_names or load_table_names()

	def inspect(self, query: SessionQuery) -> DatabaseInspection:
		if query.boundary.source != self.source.identity:
			raise ValueError("Database and validation query source identities differ")
		enforce_minimum_valid_session(self.source, query.sessions[0].session_date)
		connection = self.connect(self.source.database_config)
		try:
			cursor = connection.cursor()
			try:
				cursor.execute("START TRANSACTION", ())
				try:
					assert_transactional_tables(cursor, self.table_names)
					assert_source(cursor, self.source, self.table_names)
				except Exception:
					return DatabaseInspection((), (ValidationFinding(FindingCategory.UNVERIFIED, ValidationCheck.SCHEMA, "Updater tables, InnoDB engines, or source binding are missing or incompatible; inspect schema before explicit initialization"),))
				try:
					inspection = inspect_rows(cursor, query, self.table_names)
					if self.guardian_windows is not None:
						for warning in self.guard_statuses(cursor, query, self.guardian_windows):
							warnings.warn(warning.detail, DataValidityWarning, stacklevel=2)
					return inspection
				except Exception:
					return DatabaseInspection((), (ValidationFinding(FindingCategory.FAILED, ValidationCheck.SCHEMA, "Required database columns or row layouts could not be inspected; review schema and database access"),))
			finally:
				try:
					connection.rollback()
				finally:
					cursor.close()
		finally:
			connection.close()

	def guard_statuses(self, cursor: Any, query: SessionQuery, windows: Iterable[int] = (20, 60)) -> tuple[DataValidityNotice, ...]:
		"""Read non-ready z-score statuses for a bounded range without changing data."""
		dates = tuple(session.session_date for session in query.sessions)
		placeholders = ",".join("%s" for _ in dates)
		warnings_found: list[DataValidityNotice] = []
		selected_windows = tuple(windows)
		if len(set(selected_windows)) != len(selected_windows):
			raise ValueError("Z-score windows must be distinct integers of at least 2")
		for window in selected_windows:
			if type(window) is not int or window < 2:
				raise ValueError("Z-score windows must be distinct integers of at least 2")
			resolved = self.table_names.resolve_zscore_tables((window,))[window]
			for family, table in (("price", resolved.price), ("price_delta", resolved.price_delta)):
				try:
					cursor.execute(
						f"SELECT s.transaction_date, z.status, COUNT(*) FROM {quote_identifier(table)} z JOIN {quote_identifier(self.table_names.table(TableRole.SESSIONS))} s ON s.id = z.date_id WHERE s.transaction_date IN ({placeholders}) AND z.status <> %s GROUP BY s.transaction_date, z.status ORDER BY s.transaction_date, z.status",
						dates + (0,),
					)
					rows = cursor.fetchall()
				except Exception as error:
					message = str(error).casefold()
					if "no such table" in message or "doesn't exist" in message or "does not exist" in message:
						continue
					raise
				for row in rows:
					if len(row) != 3:
						raise ValueError("Invalid z-score guardian result")
					row_date = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])
					warnings_found.append(DataValidityNotice(window, family, row_date, int(row[1]), int(row[2])))
		return tuple(warnings_found)
