from contextlib import contextmanager
from collections.abc import Iterator

from stockguidance.config.source import SourceConfig
from stockguidance.database.dates import normalize_date_id
from stockguidance.database.repositories.market_data import DerivedUpdate, SessionSnapshot
from stockguidance.database.repositories.mysql_market_read import read_snapshots
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.validation_rows import inspect_rows
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.update_schema import assert_source, assert_transactional_tables
from stockguidance.database.write_connections import WriteConnectionFactory, WriteCursor
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, derived_id
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState
from stockguidance.market_data.records import records_id
from stockguidance.market_data.update_planning import SnapshotIndex
from stockguidance.validation.findings import ValidationFinding


class MySQLMarketDataRepository:
	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None) -> None:
		self.source, self.connect = source, connect
		self.table_names = table_names or load_table_names()

	@contextmanager
	def _transaction(self, query: SessionQuery, *, write: bool = False) -> Iterator[WriteCursor]:
		if query.boundary.source != self.source.identity:
			raise ValueError("Database and query scopes differ")
		connection = self.connect(self.source.database_config)
		try:
			cursor = connection.cursor()
			try:
				cursor.execute("START TRANSACTION", ())
				assert_transactional_tables(cursor, self.table_names)
				assert_source(cursor, self.source, self.table_names, write=write)
				yield cursor
				if write:
					connection.commit()
				else:
					connection.rollback()
			except Exception:
				connection.rollback()
				raise
			finally:
				cursor.close()
		finally:
			connection.close()

	def inspect(self, query: SessionQuery) -> tuple[SessionSnapshot, ...]:
		with self._transaction(query) as cursor:
			return read_snapshots(cursor, query, self.table_names)

	def inspect_integrity(self, query: SessionQuery) -> tuple[ValidationFinding, ...]:
		with self._transaction(query) as cursor:
			return inspect_rows(cursor, query, self.table_names).findings

	def import_session(self, query: SessionQuery, download: DownloadedSession) -> None:
		if download.session not in query.sessions or download.available_at > query.as_of or download.response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING}:
			raise ValueError("Import is outside verified available scope")
		with self._transaction(query, write=True) as cursor:
			date_id = self._date_id(cursor, download)
			cursor.execute(f"DELETE FROM {quote_identifier(self.table_names.table(TableRole.PRICES))} WHERE date_id = %s", (date_id,))
			ticker_ids = self._ticker_ids(cursor)
			rows: list[tuple[object, ...]] = []
			for bar in download.response.records:
				ticker_id = ticker_ids.get(bar.ticker)
				if ticker_id is None:
					ticker_id = self._ticker_id(cursor, bar.ticker, download.session.calendar_id)
				rows.append((ticker_id, date_id, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions))
			for offset in range(0, len(rows), 1000):
				cursor.executemany(f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.PRICES))} (ticker_id, date_id, open, high, low, close, volume, vwap, transactions) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows[offset:offset + 1000])
			cursor.execute(f"DELETE FROM {quote_identifier(self.table_names.table(TableRole.IMPORTS))} WHERE session_date = %s", (download.session.session_date,))
			cursor.execute(f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.IMPORTS))} (session_date, raw_id, available_at) VALUES (%s,%s,%s)", (download.session.session_date, records_id(download.response.records), download.available_at.isoformat()))

	def replace_derived(self, query: SessionQuery, update: DerivedUpdate) -> None:
		local_query = SessionQuery(query.boundary, tuple(session for session in query.sessions if abs(session.sequence - update.snapshot.session.sequence) <= 1), query.as_of)
		with self._transaction(local_query, write=True) as cursor:
			snapshots = read_snapshots(cursor, local_query, self.table_names)
			current = next((snapshot for snapshot in snapshots if snapshot.session == update.snapshot.session), None)
			if current is None or not current.raw_verified or current.receipt != update.snapshot.receipt or current.records != update.snapshot.records or SnapshotIndex(snapshots).dependency(current) != update.dependency_id:
				raise ValueError("Raw data changed during derived repair")
			if len({row.ticker for row in update.rows}) != len(update.rows) or {row.ticker for row in update.rows} != {bar.ticker for bar in current.records}:
				raise ValueError("Derived repair must preserve exact source record keys")
			cursor.execute(f"DELETE FROM {quote_identifier(self.table_names.table(TableRole.DELTAS))} WHERE date_id = %s", (current.date_id,))
			ticker_ids = self._ticker_ids(cursor)
			rows = [(ticker_ids[row.ticker], current.date_id, *row.values) for row in update.rows]
			columns = ", ".join(DERIVED_COLUMNS)
			placeholders = ",".join("%s" for column in range(len(DERIVED_COLUMNS) + 2))
			for offset in range(0, len(rows), 1000):
				cursor.executemany(f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.DELTAS))} (ticker_id, date_id, {columns}) VALUES ({placeholders})", rows[offset:offset + 1000])
			stored = next(snapshot for snapshot in read_snapshots(cursor, local_query, self.table_names) if snapshot.session == current.session)
			cursor.execute(f"UPDATE {quote_identifier(self.table_names.table(TableRole.IMPORTS))} SET derived_dependency_id = %s, derived_content_id = %s WHERE session_date = %s", (update.dependency_id, derived_id(stored.derived), current.session.session_date))

	def _ticker_ids(self, cursor: Cursor) -> dict[str, int]:
		cursor.execute(f"SELECT id, label FROM {quote_identifier(self.table_names.table(TableRole.TICKERS))}", ())
		result: dict[str, int] = {}
		for row in cursor.fetchall():
			if len(row) != 2 or not isinstance(row[1], str) or row[1] in result:
				raise ValueError("Invalid ticker mapping")
			result[row[1]] = normalize_date_id(row[0])
		return result

	def _date_id(self, cursor: Cursor, download: DownloadedSession) -> int:
		parameters = (download.session.session_date,)
		cursor.execute(f"INSERT IGNORE INTO {quote_identifier(self.table_names.table(TableRole.SESSIONS))} (transaction_date) VALUES (%s)", parameters)
		cursor.execute(f"SELECT id FROM {quote_identifier(self.table_names.table(TableRole.SESSIONS))} WHERE transaction_date = %s", parameters)
		rows = cursor.fetchall()
		if len(rows) != 1 or len(rows[0]) != 1:
			raise ValueError("Date lookup must resolve exactly one ingested session")
		return normalize_date_id(rows[0][0])

	def _ticker_id(self, cursor: Cursor, ticker: str, exchange: str) -> int:
		cursor.execute(f"SELECT id, label, exchange FROM {quote_identifier(self.table_names.table(TableRole.TICKERS))}", ())
		rows = [row for row in cursor.fetchall() if len(row) == 3 and row[1] == ticker]
		if rows:
			if len(rows) != 1 or len(rows[0]) != 3 or (rows[0][2] is not None and rows[0][2] != exchange):
				raise ValueError("Ticker lookup must resolve exactly one key")
			if rows[0][2] is None:
				cursor.execute(f"UPDATE {quote_identifier(self.table_names.table(TableRole.TICKERS))} SET exchange = %s WHERE id = %s", (exchange, rows[0][0]))
			return normalize_date_id(rows[0][0])
		cursor.execute(f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.TICKERS))} (label, exchange) VALUES (%s, %s)", (ticker, exchange))
		cursor.execute(f"SELECT id FROM {quote_identifier(self.table_names.table(TableRole.TICKERS))} WHERE label = %s", (ticker,))
		rows = cursor.fetchall()
		if len(rows) != 1 or len(rows[0]) != 1:
			raise ValueError("Ticker lookup must resolve exactly one key")
		return normalize_date_id(rows[0][0])
