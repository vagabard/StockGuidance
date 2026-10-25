from contextlib import contextmanager
from collections.abc import Callable
from collections.abc import Iterator
from datetime import date
from decimal import Decimal, InvalidOperation

from stockguidance.config.source import SourceConfig
from stockguidance.database.dates import normalize_date_id
from stockguidance.database.repositories.market_data import DerivedUpdate, SessionMetadata, SessionSnapshot
from stockguidance.database.repositories.mysql_market_read import _dates, has_orphans, read_metadata, read_snapshots
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.validation_rows import inspect_rows
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.update_schema import assert_source, assert_transactional_tables
from stockguidance.database.write_connections import WriteConnectionFactory, WriteCursor
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, derived_id
from stockguidance.market_data.providers.contracts import DownloadedSession, ResponseState
from stockguidance.market_data.records import records_id
from stockguidance.market_data.quality import ObservedBar, QualityFlag, QualityRecord, invalid_fields
from stockguidance.database.repositories.quality import CURRENT, SCANS, save_quality, derived_quality, read_quality
from stockguidance.market_data.identity import content_id, data_configuration_key
from stockguidance.database.repositories.mysql_quality import reconcile_quality
from stockguidance.workflows.progress import ProgressReporter
from stockguidance.database.repositories.timed_cursor import TimedCursor
from stockguidance.market_data.update_planning import SnapshotIndex
from stockguidance.validation.findings import ValidationFinding


class MySQLMarketDataRepository:
	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None, *, reporter: ProgressReporter | None = None) -> None:
		self.source, self.connect = source, connect
		self.table_names = table_names or load_table_names()
		self.reporter = reporter or ProgressReporter()
		self.force_verify = False
		self.metadata_only = False
		self.legacy_read_only = False

	@contextmanager
	def _transaction(self, query: SessionQuery, *, write: bool = False, commit_when: Callable[[], bool] | None = None) -> Iterator[WriteCursor]:
		if query.boundary.source != self.source.identity:
			raise ValueError("Database and query scopes differ")
		connection = self.connect(self.source.database_config)
		try:
			cursor = TimedCursor(connection.cursor(), self.reporter) if self.reporter.enabled else connection.cursor()
			try:
				cursor.execute("START TRANSACTION", ())
				assert_transactional_tables(cursor, self.table_names)
				assert_source(cursor, self.source, self.table_names, write=write)
				yield cursor
				if write and (commit_when is None or commit_when()):
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

	def reconcile_quality(self, query: SessionQuery) -> tuple[date, ...]:
		changed: tuple[date, ...] = ()
		with self._transaction(query, write=True, commit_when=lambda: bool(changed)) as cursor:
			changed = self.reporter.measure(f"classify and interpolate {len(query.sessions)} session(s)", lambda: reconcile_quality(cursor, query, self.table_names))
			if changed and self.reporter.enabled:
				ids = _dates(cursor, query, self.table_names)
				records = tuple(read_quality(cursor, tuple(date_id for date_id, session_date in ids.items() if session_date in changed)).values())
				self.reporter.message(f"quality changed: {len(changed)} sessions; {len(records)} bad rows; {sum(record.effective is not None for record in records)} interpolated")
				for record in records[:10]:
					self.reporter.message(f"quality {record.observation.ticker}/{record.observation.session_date}: flags={record.flags.name}; gap={record.gap_length}; fields={record.fields}; original={record.observation.values}")
		return changed

	def has_orphans(self, query: SessionQuery) -> bool:
		with self._transaction(query) as cursor:
			return has_orphans(cursor, self.table_names)

	def reconcile_quality_dependencies(self, query: SessionQuery, affected: frozenset[date]) -> tuple[date, ...]:
		changed: tuple[date, ...] = ()
		with self._transaction(query, write=True, commit_when=lambda: bool(changed)) as cursor:
			known = read_quality(cursor, tuple(_dates(cursor, query, self.table_names)))
			tickers = frozenset(ticker_id for (_, ticker_id), record in known.items() if record.observation.session_date in affected or record.prior in affected or record.following in affected or record.effective is None)
			if tickers:
				changed = reconcile_quality(cursor, query, self.table_names, scan=False, ticker_filter=tickers)
		return changed

	def inspect_metadata(self, query: SessionQuery, *, force_verify: bool = False) -> tuple[SessionMetadata, ...]:
		with self._transaction(query) as cursor:
			return self.reporter.measure("database session metadata", lambda: read_metadata(cursor, query, self.table_names, force_verify=force_verify or self.force_verify, metadata_only=self.metadata_only))

	def scan_quality(self, query: SessionQuery) -> tuple[date, ...]:
		key = content_id([self.source.data_configuration.semantic_payload(), query.boundary.calendar_id, [session.session_date.isoformat() for session in query.sessions]])
		with self._transaction(query) as cursor:
			cursor.execute(f"SELECT last_session FROM {SCANS} WHERE scope_key = %s", (key,))
			rows = cursor.fetchall()
		last = None if not rows else date.fromisoformat(str(rows[0][0]))
		remaining = tuple(session for session in query.sessions if last is None or session.session_date > last)
		changed: set[date] = set()
		for offset in range(0, len(remaining), 128):
			batch = remaining[offset:offset + 128]
			local = SessionQuery(query.boundary, batch, query.as_of)
			with self._transaction(local, write=True) as cursor:
				changed.update(reconcile_quality(cursor, local, self.table_names))
				cursor.execute(f"DELETE FROM {SCANS} WHERE scope_key = %s", (key,))
				cursor.execute(f"INSERT INTO {SCANS} (scope_key, last_session, completed_at) VALUES (%s,%s,%s)", (key, batch[-1].session_date.isoformat(), query.as_of.isoformat()))
			self.reporter.message(f"quality scan {min(offset + 128, len(remaining))}/{len(remaining)} remaining sessions")
		with self._transaction(query, write=True) as cursor:
			changed.update(reconcile_quality(cursor, query, self.table_names, scan=False))
			cursor.execute(f"DELETE FROM {SCANS} WHERE scope_key = %s", (key,))
		return tuple(sorted(changed))

	def cleanup_placeholders(self, query: SessionQuery) -> tuple[date, ...]:
		return self.reconcile_quality(query)

	def inspect_integrity(self, query: SessionQuery) -> tuple[ValidationFinding, ...]:
		with self._transaction(query) as cursor:
			findings = inspect_rows(cursor, query, self.table_names).findings
			quality = read_quality(cursor, tuple(_dates(cursor, query, self.table_names)))
			return tuple(finding for finding in findings if not (finding.check.value == "raw_values" and len(finding.record_ids) == 2 and tuple(finding.record_ids) in quality))

	def import_session(self, query: SessionQuery, download: DownloadedSession) -> None:
		if download.session not in query.sessions or download.available_at > query.as_of or download.response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING}:
			raise ValueError("Import is outside verified available scope")
		with self._transaction(query, write=True) as cursor:
			date_id = self._date_id(cursor, download)
			cursor.execute(f"DELETE FROM {quote_identifier(self.table_names.table(TableRole.PRICES))} WHERE date_id = %s", (date_id,))
			ticker_ids = self._ticker_ids(cursor)
			rows: list[tuple[object, ...]] = []
			cursor.execute(f"DELETE FROM {CURRENT} WHERE date_id = %s", (date_id,))
			for bar in download.response.records:
				ticker_id = ticker_ids.get(bar.ticker)
				if ticker_id is None:
					ticker_id = self._ticker_id(cursor, bar.ticker, download.session.calendar_id)
				rows.append((ticker_id, date_id, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions))
			for ticker, values in download.response.bad_records:
				ticker_id = ticker_ids.get(ticker)
				if ticker_id is None:
					ticker_id = self._ticker_id(cursor, ticker, download.session.calendar_id)
				observation = ObservedBar(ticker, download.session.session_date, download.session.sequence, values, download.available_at)
				save_quality(cursor, date_id, ticker_id, QualityRecord(observation, QualityFlag.BAD, invalid_fields(values)), query.as_of)
				stored_values = []
				for value in values:
					try:
						number = Decimal(str(value))
						stored_values.append(number if number.is_finite() and abs(number) < Decimal("1e18") else None)
					except (ValueError, InvalidOperation):
						stored_values.append(None)
				rows.append((ticker_id, date_id, *stored_values))
			for offset in range(0, len(rows), 1000):
				cursor.executemany(f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.PRICES))} (ticker_id, date_id, open, high, low, close, volume, vwap, transactions) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows[offset:offset + 1000])
			cursor.execute(f"DELETE FROM {quote_identifier(self.table_names.table(TableRole.IMPORTS))} WHERE session_date = %s", (download.session.session_date,))
			cursor.execute(f"INSERT INTO {quote_identifier(self.table_names.table(TableRole.IMPORTS))} (session_date, raw_id, available_at) VALUES (%s,%s,%s)", (download.session.session_date, records_id(download.response.records), download.available_at.isoformat()))
			cursor.execute("DELETE FROM stockguidance_daily_import_in_progress WHERE session_date = %s", (download.session.session_date,))
			cursor.execute("UPDATE stockguidance_daily_source_files SET download_pending = 0 WHERE source_key = %s AND session_date = %s", (data_configuration_key(self.source.data_configuration), download.session.session_date))

	def acknowledge_archive(self, query: SessionQuery, download: DownloadedSession) -> None:
		with self._transaction(query, write=True) as cursor:
			cursor.execute(f"SELECT raw_id FROM {quote_identifier(self.table_names.imports)} WHERE session_date = %s FOR UPDATE", (download.session.session_date,))
			rows = cursor.fetchall()
			if len(rows) != 1 or rows[0][0] != records_id(download.response.records):
				raise ValueError("Cannot acknowledge an archive without its matching committed raw receipt")
			cursor.execute("DELETE FROM stockguidance_daily_import_in_progress WHERE session_date = %s", (download.session.session_date,))
			cursor.execute("UPDATE stockguidance_daily_source_files SET download_pending = 0 WHERE source_key = %s AND session_date = %s", (data_configuration_key(self.source.data_configuration), download.session.session_date))

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
			derived_quality(cursor, current.date_id, ticker_ids, snapshots, current)
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
