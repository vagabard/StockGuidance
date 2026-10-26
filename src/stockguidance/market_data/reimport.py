from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, cast

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import SourceConfig, SourceIdentity
from stockguidance.database.repositories.archive_catalog import ARCHIVE_DDL
from stockguidance.database.table_names import TableNames, TableRole, quote_identifier
from stockguidance.market_data.archive import archive_path, decode_archive
from stockguidance.market_data.identity import data_configuration_key, encode_json
from stockguidance.market_data.records import records_id


@dataclass(frozen=True)
class ReimportFile:
	day: date
	path: Path
	content_id: str
	row_count: int


@dataclass(frozen=True)
class ReimportPlan:
	start: date
	end: date
	files: tuple[ReimportFile, ...]
	missing_dates: tuple[date, ...]
	canonical_dates: tuple[date, ...]

	@property
	def complete(self) -> bool:
		return not self.missing_dates


def _sha256(path: Path) -> str:
	from hashlib import sha256

	digest = sha256()
	with path.open("rb") as stream:
		while chunk := stream.read(1024 * 1024):
			digest.update(chunk)
	return digest.hexdigest()


def plan(data_root: Path, start: date, end: date, calendar: ExchangeCalendar) -> ReimportPlan:
	if start > end:
		raise ValueError("Reimport start must not follow its end")
	paths: list[ReimportFile] = []
	for path in sorted((data_root / "market_summary").glob("[0-9][0-9][0-9][0-9]/*.ndjson")):
		if path.parent.name != path.stem[:4]:
			continue
		try:
			session_date = date.fromisoformat(path.stem)
		except ValueError:
			continue
		if not start <= session_date <= end:
			continue
		session = calendar.session(session_date)
		response = decode_archive(path.read_bytes(), session)
		paths.append(ReimportFile(session_date, path, _sha256(path), len(response.records) + len(response.bad_records)))
	requested = tuple(session.session_date for session in calendar.sessions(start, end))
	available = tuple(item.day for item in paths)
	missing = tuple(session_date for session_date in requested if session_date not in set(available))
	return ReimportPlan(start, end, tuple(paths), missing, requested)


def _date_ids(cursor: Any, names: TableNames, first: date, last: date) -> tuple[int, ...]:
	cursor.execute(f"SELECT id FROM {quote_identifier(names.table(TableRole.SESSIONS))} WHERE transaction_date BETWEEN %s AND %s", (first, last))
	return tuple(int(row[0]) for row in cursor.fetchall())


def _delete_period(cursor: Any, names: TableNames, first: date, last: date) -> dict[str, int]:
	raw_ids = _date_ids(cursor, names, first, last)
	counts: dict[str, int] = {}
	def delete_ids(table: str, column: str, ids: tuple[int, ...]) -> None:
		if not ids:
			counts[table] = 0
			return
		placeholders = ",".join("%s" for _ in ids)
		cursor.execute(f"DELETE FROM {quote_identifier(table)} WHERE {quote_identifier(column)} IN ({placeholders})", ids)
		counts[table] = cursor.rowcount
	for role in (TableRole.DELTAS, TableRole.PRICES):
		delete_ids(names.table(role), "date_id", raw_ids)
	for table in ("stockguidance_output_quality", "stockguidance_quality_revisions", "stockguidance_row_quality", names.table(TableRole.ZSCORE_SESSIONS), *[table for window in names.resolve_zscore_tables((20, 60)).values() for table in (window.price, window.price_delta)]):
		delete_ids(table, "date_id", raw_ids)
	for table, column in ((names.table(TableRole.IMPORTS), "session_date"), ("stockguidance_daily_import_in_progress", "session_date"), ("stockguidance_migration_completion", "session_date"), ("stockguidance_migration_receipts", "session_date")):
		cursor.execute(f"DELETE FROM {quote_identifier(table)} WHERE {quote_identifier(column)} BETWEEN %s AND %s", (first, last))
		counts[table] = cursor.rowcount
	cursor.execute("DELETE FROM stockguidance_daily_source_files WHERE session_date BETWEEN %s AND %s", (first, last))
	counts["stockguidance_daily_source_files"] = cursor.rowcount
	cursor.execute("DELETE FROM stockguidance_quality_scans", ())
	counts["stockguidance_quality_scans"] = cursor.rowcount
	cursor.execute("DELETE FROM stockguidance_zscore_series", ())
	counts["stockguidance_zscore_series"] = cursor.rowcount
	cursor.execute("DELETE FROM stockguidance_data_verification_samples", ())
	counts["stockguidance_data_verification_samples"] = cursor.rowcount
	cursor.execute("DELETE FROM stockguidance_data_verifications", ())
	counts["stockguidance_data_verifications"] = cursor.rowcount
	return counts


def _upsert_archive(cursor: Any, source: SourceConfig, item: ReimportFile, response: Any, available_at: str) -> None:
	identity = cast(SourceIdentity, source.identity)
	metadata = {"source": asdict(identity), "calendar_id": source.calendar_id, "session_date": item.day.isoformat(), "available_at": available_at, "content_id": item.content_id, "records_id": records_id(response.records), "pending": False, "evidence": "Canonical market_summary archive adopted during controlled reimport"}
	key = data_configuration_key(source.data_configuration)
	cursor.execute(f"DELETE FROM stockguidance_archive_files WHERE source_key = %s AND session_date = %s", (key, item.day))
	cursor.execute("INSERT INTO stockguidance_archive_files (source_key, session_date, metadata) VALUES (%s,%s,%s)", (key, item.day, encode_json(metadata).decode("utf-8")))
	cursor.execute("INSERT INTO stockguidance_daily_source_files (source_key, session_date, file_size, modified_ns, download_pending) VALUES (%s,%s,%s,%s,0) ON DUPLICATE KEY UPDATE file_size=VALUES(file_size), modified_ns=VALUES(modified_ns), download_pending=0", (key, item.day, item.path.stat().st_size, item.path.stat().st_mtime_ns))


def apply_reimport(source: SourceConfig, connect: Any, names: TableNames, operation: str, migration_backup: str, plan_value: ReimportPlan) -> dict[str, Any]:
	if not plan_value.complete:
		raise ValueError(f"Cannot apply reimport; {len(plan_value.missing_dates)} canonical dates are missing")
	if not migration_backup.strip():
		raise ValueError("A database backup confirmation is required")
	connection = connect(source.database_config)
	try:
		cursor = connection.cursor()
		try:
			cursor.execute("START TRANSACTION", ())
			cursor.execute(ARCHIVE_DDL, ())
			cursor.execute(f"SELECT raw_price_split_adjusted FROM {quote_identifier(names.table(TableRole.SOURCE))} WHERE id=1", ())
			row = cursor.fetchone()
			if row is None or not bool(row[0]):
				raise ValueError("Reimport expects the current database source to be the adjusted configuration")
			counts = _delete_period(cursor, names, plan_value.start, plan_value.end)
			connection.commit()
		finally:
			cursor.close()

		imported = 0
		adopted_at = datetime.now(timezone.utc).isoformat()
		for item in plan_value.files:
			cursor = connection.cursor()
			try:
				cursor.execute("START TRANSACTION", ())
				if _sha256(item.path) != item.content_id:
					raise ValueError(f"Canonical archive changed before import: {item.day}")
				session = ExchangeCalendar(source.calendar_id, item.day, item.day).session(item.day)
				response = decode_archive(item.path.read_bytes(), session)
				cursor.execute(f"SELECT id,label,exchange FROM {quote_identifier(names.table(TableRole.TICKERS))}", ())
				tickers = {str(row[1]): (int(row[0]), row[2]) for row in cursor.fetchall()}
				cursor.execute(f"SELECT id FROM {quote_identifier(names.table(TableRole.SESSIONS))} WHERE transaction_date=%s", (item.day,))
				date_row = cursor.fetchone()
				if date_row is None:
					cursor.execute(f"INSERT INTO {quote_identifier(names.table(TableRole.SESSIONS))} (transaction_date) VALUES (%s)", (item.day,))
					cursor.execute(f"SELECT id FROM {quote_identifier(names.table(TableRole.SESSIONS))} WHERE transaction_date=%s", (item.day,))
					date_row = cursor.fetchone()
				date_id = int(date_row[0])
				cursor.execute(f"DELETE FROM {quote_identifier(names.table(TableRole.PRICES))} WHERE date_id=%s", (date_id,))
				rows: list[tuple[Any, ...]] = []
				for bar in response.records:
					if bar.ticker not in tickers:
						cursor.execute(f"INSERT INTO {quote_identifier(names.table(TableRole.TICKERS))} (label,exchange,type,market) VALUES (%s,%s,%s,%s)", (bar.ticker, source.calendar_id, "CS", source.identity.market))
						cursor.execute(f"SELECT id,exchange FROM {quote_identifier(names.table(TableRole.TICKERS))} WHERE label=%s", (bar.ticker,))
						new_row = cursor.fetchone()
						tickers[bar.ticker] = (int(new_row[0]), new_row[1])
					rows.append((tickers[bar.ticker][0], date_id, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions))
				for ticker, values in response.bad_records:
					if ticker not in tickers:
						cursor.execute(f"INSERT INTO {quote_identifier(names.table(TableRole.TICKERS))} (label,exchange,type,market) VALUES (%s,%s,%s,%s)", (ticker, source.calendar_id, "CS", source.identity.market))
						cursor.execute(f"SELECT id,exchange FROM {quote_identifier(names.table(TableRole.TICKERS))} WHERE label=%s", (ticker,))
						new_row = cursor.fetchone()
						tickers[ticker] = (int(new_row[0]), new_row[1])
					stored_values = []
					for value in values:
						try:
							number = Decimal(str(value))
							stored_values.append(number if number.is_finite() and abs(number) < Decimal("1e18") else None)
						except (ValueError, ArithmeticError):
							stored_values.append(None)
					rows.append((tickers[ticker][0], date_id, *stored_values))
				for offset in range(0, len(rows), 1000):
					cursor.executemany(f"INSERT INTO {quote_identifier(names.table(TableRole.PRICES))} (ticker_id,date_id,open,high,low,close,volume,vwap,transactions) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows[offset:offset + 1000])
				cursor.execute(f"INSERT INTO {quote_identifier(names.table(TableRole.IMPORTS))} (session_date,raw_id,available_at) VALUES (%s,%s,%s)", (item.day, records_id(response.records), adopted_at))
				_upsert_archive(cursor, source, item, response, adopted_at)
				connection.commit()
				imported += 1
			except BaseException:
				connection.rollback()
				raise
			finally:
				cursor.close()

		cursor = connection.cursor()
		try:
			configuration = source.data_configuration
			cursor.execute("START TRANSACTION", ())
			cursor.execute(f"UPDATE {quote_identifier(names.table(TableRole.SOURCE))} SET configuration_key=%s, source_key=%s, source_family=%s, endpoint=%s, market=%s, universe=%s, interval_code=%s, raw_price_split_adjusted=0, raw_price_dividend_adjusted=0, raw_price_vendor_defined_adjustment=NULL WHERE id=1", (data_configuration_key(configuration), data_configuration_key(configuration), configuration.source_family, configuration.endpoint, configuration.market, configuration.universe, configuration.interval.value))
			connection.commit()
		finally:
			cursor.close()
		return {"operation": operation, "applied": True, "imported_dates": imported, "deleted_rows": counts, "start": plan_value.start.isoformat(), "end": plan_value.end.isoformat(), "backup_confirmation": migration_backup}
	finally:
		connection.close()
