from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from stockguidance.config.source import SourceConfig
from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.repositories.sessions import SessionQuery, StoredSession
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

	def __init__(self, source: SourceConfig, connect: ConnectionFactory) -> None:
		self.source = source
		self.connect = connect

	def read(self, query: SessionQuery) -> tuple[StoredSession, ...]:
		if query.boundary.source != self.source.identity:
			raise ValueError("Query and database source identities differ")
		dates = tuple(session.session_date for session in query.sessions)
		placeholders = ",".join("%s" for session in query.sessions)
		sql = (
			"SELECT t.id, t.transaction_date, COUNT(r.ticker_id) "
			"FROM transaction_dates t LEFT JOIN raw_stock_data r ON r.date_id = t.id "
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
