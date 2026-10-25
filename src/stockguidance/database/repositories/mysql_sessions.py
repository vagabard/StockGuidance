from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Protocol

from stockguidance.config.source import PriceAdjustment, SourceConfig
from stockguidance.market_data.history import EvidenceQuality, InventoryIssue, LocalCoverageInventory, LocalObservation
from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.repositories.sessions import SessionQuery, StoredSession
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.validation.coverage import CoverageState


class Cursor(Protocol):
	def execute(self, operation: str, parameters: tuple[object, ...]) -> object: ...
	def fetchall(self) -> Sequence[Sequence[object]]: ...
	def close(self) -> None: ...


class Connection(Protocol):
	def cursor(self) -> Cursor: ...
	def close(self) -> None: ...


class ConnectionFactory(Protocol):
	def __call__(self, database_config: Path) -> Connection: ...


class MySQLSessionRepository:
	"""Read legacy date mappings without claiming source/provider completeness."""

	def __init__(self, source: SourceConfig, connect: ConnectionFactory, table_names: TableNames | None = None) -> None:
		self.source, self.connect = source, connect
		self.table_names = table_names or load_table_names()

	def inventory(self, start: date, end: date, calendar_id: str) -> LocalCoverageInventory:
		"""Inventory legacy mappings with one bounded read-only query.

		These rows have no source-bound response or receipt identity, so they remain
		visible as unverified observations.
		"""
		if not isinstance(start, date) or not isinstance(end, date) or start > end or not isinstance(calendar_id, str) or not calendar_id:
			raise ValueError("Invalid legacy inventory bounds")
		sql = (
			f"SELECT t.id, t.transaction_date, COUNT(r.ticker_id) FROM {quote_identifier(self.table_names.table(TableRole.SESSIONS))} t "
			f"LEFT JOIN {quote_identifier(self.table_names.table(TableRole.PRICES))} r ON r.date_id = t.id "
			"WHERE t.transaction_date BETWEEN %s AND %s "
			"GROUP BY t.id, t.transaction_date ORDER BY t.transaction_date ASC, t.id ASC"
		)
		if self.source.identity.adjustment is PriceAdjustment.UNADJUSTED:
			sql = (
				f"SELECT t.id, t.transaction_date, CASE WHEN i.raw_id IS NULL THEN 0 ELSE 1 END FROM {quote_identifier(self.table_names.sessions)} t "
				f"LEFT JOIN {quote_identifier(self.table_names.imports)} i ON i.session_date = t.transaction_date "
				"WHERE t.transaction_date BETWEEN %s AND %s ORDER BY t.transaction_date ASC, t.id ASC"
			)
		connection = self.connect(self.source.database_config)
		try:
			cursor = connection.cursor()
			try:
				cursor.execute(sql, (start, end))
				rows = cursor.fetchall()
			finally:
				cursor.close()
		finally:
			connection.close()
		observations: list[LocalObservation] = []
		issues: list[InventoryIssue] = []
		seen_dates: set[date] = set()
		for row in rows:
			if len(row) != 3:
				raise ValueError("Legacy inventory returned an invalid row layout")
			date_id, session_date = normalize_date_id(row[0]), normalize_session_date(row[1])
			count = row[2]
			if type(count) is not int or count < 0 or session_date in seen_dates:
				raise ValueError("Invalid or duplicate legacy inventory row")
			seen_dates.add(session_date)
			if count == 0:
				issues.append(InventoryIssue(calendar_id, "Legacy date mapping exists without raw observations", session_date))
				continue
			observations.append(LocalObservation(self.source.identity, calendar_id, session_date, EvidenceQuality.UNVERIFIED, "Legacy raw rows have no source-bound manifest or receipt evidence", False))
		return LocalCoverageInventory(self.source.identity, calendar_id, tuple(observations), tuple(issues))

	def read(self, query: SessionQuery) -> tuple[StoredSession, ...]:
		if query.boundary.source != self.source.identity:
			raise ValueError("Query and database source identities differ")
		dates = tuple(session.session_date for session in query.sessions)
		placeholders = ",".join("%s" for session in query.sessions)
		sql = (
			f"SELECT t.id, t.transaction_date, COUNT(r.ticker_id) FROM {quote_identifier(self.table_names.table(TableRole.SESSIONS))} t "
			f"LEFT JOIN {quote_identifier(self.table_names.table(TableRole.PRICES))} r ON r.date_id = t.id "
			f"WHERE t.transaction_date IN ({placeholders}) "
			"GROUP BY t.id, t.transaction_date ORDER BY t.transaction_date ASC, t.id ASC"
		)
		connection = self.connect(self.source.database_config)
		try:
			cursor = connection.cursor()
			try:
				cursor.execute(sql, dates)
				rows = cursor.fetchall()
			finally:
				cursor.close()
		finally:
			connection.close()
		stored: list[StoredSession] = []
		for row in rows:
			if len(row) != 3:
				raise ValueError("Session query returned an invalid row layout")
			date_id, session_date = normalize_date_id(row[0]), normalize_session_date(row[1])
			count = row[2]
			if type(count) is not int or count < 0 or session_date not in dates:
				raise ValueError("Invalid or out-of-scope session coverage row")
			state = CoverageState.MISSING if count == 0 else CoverageState.UNVERIFIED
			stored.append(StoredSession(date_id, session_date, state, "Legacy raw rows absent" if count == 0 else "Legacy rows present; source scope and publication unverified"))
		if len({item.date_id for item in stored}) != len(stored) or len({item.session_date for item in stored}) != len(stored):
			raise ValueError("Duplicate database session IDs or dates")
		return tuple(sorted(stored, key=lambda item: item.session_date))
