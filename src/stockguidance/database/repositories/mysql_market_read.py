from collections.abc import Sequence
from datetime import date, datetime
import math

from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.repositories.market_data import ImportReceipt, SessionMetadata, SessionSnapshot
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.table_names import TableNames, TableRole, quote_identifier
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow
from stockguidance.market_data.records import DailyBar, count_value, decimal_price, decimal_quantity, decimal_vwap, is_non_trading_placeholder


def _optional_text(value: object) -> str | None:
	if value is not None and not isinstance(value, str):
		raise ValueError("Invalid import receipt")
	return value


def _derived_value(value: object) -> float | None:
	if value is None:
		return None
	result = float(str(value))
	if not math.isfinite(result):
		raise ValueError("Invalid derived value")
	return result


def decode_raw_row(row: Sequence[object]) -> tuple[int, DailyBar]:
	if len(row) != 9 or not isinstance(row[1], str):
		raise ValueError("Invalid raw record layout")
	return normalize_date_id(row[0]), DailyBar(row[1], decimal_price(row[2]), decimal_price(row[3]), decimal_price(row[4]), decimal_price(row[5]), decimal_quantity(row[6]), None if row[7] is None else decimal_vwap(row[7]), None if row[8] is None else count_value(row[8]))


def decode_derived_row(row: Sequence[object]) -> tuple[int, LegacyDerivedRow]:
	if len(row) != len(DERIVED_COLUMNS) + 2 or not isinstance(row[1], str):
		raise ValueError("Invalid derived record layout")
	return normalize_date_id(row[0]), LegacyDerivedRow(row[1], tuple(_derived_value(value) for value in row[2:]))


def decode_receipt(row: Sequence[object]) -> tuple[date, ImportReceipt]:
	if len(row) != 5 or not isinstance(row[1], str) or not isinstance(row[2], str):
		raise ValueError("Invalid import receipt layout")
	return normalize_session_date(row[0]), ImportReceipt(row[1], datetime.fromisoformat(row[2]), _optional_text(row[3]), _optional_text(row[4]))


def _dates(cursor: Cursor, query: SessionQuery, table_names: TableNames) -> dict[int, date]:
	dates = tuple(session.session_date for session in query.sessions)
	placeholders = ",".join("%s" for _ in dates)
	cursor.execute(f"SELECT id, transaction_date FROM {quote_identifier(table_names.table(TableRole.SESSIONS))} WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date", dates)
	ids: dict[int, date] = {}
	for row in cursor.fetchall():
		if len(row) != 2:
			raise ValueError("Invalid date layout")
		date_id, session_date = normalize_date_id(row[0]), normalize_session_date(row[1])
		if date_id in ids or session_date in ids.values() or session_date not in dates:
			raise ValueError("Duplicate or out-of-scope date mapping")
		ids[date_id] = session_date
	return ids


def _receipts(cursor: Cursor, dates: tuple[date, ...], table_names: TableNames) -> dict[date, ImportReceipt]:
	if not dates:
		return {}
	placeholders = ",".join("%s" for _ in dates)
	cursor.execute(f"SELECT session_date, raw_id, available_at, derived_dependency_id, derived_content_id FROM {quote_identifier(table_names.table(TableRole.IMPORTS))} WHERE session_date IN ({placeholders})", dates)
	receipts: dict[date, ImportReceipt] = {}
	for row in cursor.fetchall():
		session_date, receipt = decode_receipt(row)
		if session_date in receipts or session_date not in dates:
			raise ValueError("Invalid receipt scope or timestamp")
		receipts[session_date] = receipt
	return receipts


def read_metadata(cursor: Cursor, query: SessionQuery, table_names: TableNames) -> tuple[SessionMetadata, ...]:
	ids = _dates(cursor, query, table_names)
	by_date = {session.session_date: session for session in query.sessions}
	receipts = _receipts(cursor, tuple(by_date), table_names)
	date_ids = tuple(ids)
	if not date_ids:
		return ()
	placeholders = ",".join("%s" for _ in date_ids)
	counts: dict[int, list[int]] = {date_id: [0, 0] for date_id in date_ids}
	for index, role in enumerate((TableRole.PRICES, TableRole.DELTAS)):
		cursor.execute(f"SELECT date_id, COUNT(*) FROM {quote_identifier(table_names.table(role))} WHERE date_id IN ({placeholders}) GROUP BY date_id", date_ids)
		for row in cursor.fetchall():
			if len(row) != 2:
				raise ValueError("Invalid session metadata count")
			date_id = normalize_date_id(row[0])
			count = row[1]
			if date_id not in counts or type(count) is not int or count < 0:
				raise ValueError("Invalid session metadata count")
			counts[date_id][index] = count
	cursor.execute(f"SELECT date_id, ticker_id FROM {quote_identifier(table_names.table(TableRole.PRICES))} WHERE date_id IN ({placeholders}) ORDER BY date_id, ticker_id", date_ids)
	ticker_ids: dict[int, list[int]] = {date_id: [] for date_id in date_ids}
	for row in cursor.fetchall():
		if len(row) != 2:
			raise ValueError("Invalid session metadata key")
		date_id, ticker_id = normalize_date_id(row[0]), normalize_date_id(row[1])
		if date_id not in ticker_ids:
			raise ValueError("Invalid session metadata key scope")
		ticker_ids[date_id].append(ticker_id)
	cursor.execute(f"SELECT id, label FROM {quote_identifier(table_names.table(TableRole.TICKERS))}", ())
	labels: dict[int, str] = {}
	for row in cursor.fetchall():
		if len(row) != 2 or not isinstance(row[1], str):
			raise ValueError("Invalid ticker mapping")
		ticker_id = normalize_date_id(row[0])
		if ticker_id in labels:
			raise ValueError("Duplicate ticker mapping")
		labels[ticker_id] = row[1]
	if any(ticker_id not in labels for values in ticker_ids.values() for ticker_id in values):
		raise ValueError("Missing ticker mapping")
	return tuple(SessionMetadata(by_date[session_date], date_id, receipts.get(session_date), counts[date_id][0], counts[date_id][1], tuple(labels[ticker_id] for ticker_id in ticker_ids[date_id])) for date_id, session_date in ids.items())


def has_orphans(cursor: Cursor, table_names: TableNames) -> bool:
	for role in (TableRole.PRICES, TableRole.DELTAS):
		cursor.execute(f"SELECT date_id FROM {quote_identifier(table_names.table(role))} WHERE date_id NOT IN (SELECT id FROM {quote_identifier(table_names.table(TableRole.SESSIONS))}) LIMIT 1", ())
		if cursor.fetchall():
			return True
	return False


def read_snapshots(cursor: Cursor, query: SessionQuery, table_names: TableNames) -> tuple[SessionSnapshot, ...]:
	dates = tuple(session.session_date for session in query.sessions)
	ids = _dates(cursor, query, table_names)
	date_ids = tuple(ids)
	if not date_ids:
		return ()
	id_placeholders = ",".join("%s" for _ in date_ids)
	cursor.execute(f"SELECT id, label FROM {quote_identifier(table_names.table(TableRole.TICKERS))}", ())
	labels: dict[int, str] = {}
	for row in cursor.fetchall():
		if len(row) != 2 or not isinstance(row[1], str):
			raise ValueError("Invalid ticker mapping")
		ticker_id = normalize_date_id(row[0])
		if ticker_id in labels:
			raise ValueError("Duplicate ticker mapping")
		labels[ticker_id] = row[1]
	cursor.execute(f"SELECT date_id, ticker_id, open, high, low, close, volume, vwap, transactions FROM {quote_identifier(table_names.table(TableRole.PRICES))} WHERE date_id IN ({id_placeholders}) ORDER BY date_id, ticker_id", date_ids)
	raw: dict[int, list[DailyBar]] = {}
	for row in cursor.fetchall():
		if len(row) != 9 or normalize_date_id(row[1]) not in labels:
			raise ValueError("Invalid raw ticker mapping")
		if is_non_trading_placeholder(tuple(row[2:])):
			continue
		date_id, bar = decode_raw_row((row[0], labels[normalize_date_id(row[1])], *row[2:]))
		raw.setdefault(date_id, []).append(bar)
	cursor.execute(f"SELECT date_id, ticker_id, " + ", ".join(DERIVED_COLUMNS) + f" FROM {quote_identifier(table_names.table(TableRole.DELTAS))} WHERE date_id IN ({id_placeholders}) ORDER BY date_id, ticker_id", date_ids)
	derived: dict[int, list[LegacyDerivedRow]] = {}
	for row in cursor.fetchall():
		if len(row) != len(DERIVED_COLUMNS) + 2 or normalize_date_id(row[1]) not in labels:
			raise ValueError("Invalid derived ticker mapping")
		date_id, derived_row = decode_derived_row((row[0], labels[normalize_date_id(row[1])], *row[2:]))
		derived.setdefault(date_id, []).append(derived_row)
	receipts = _receipts(cursor, dates, table_names)
	by_date = {session.session_date: session for session in query.sessions}
	snapshots: list[SessionSnapshot] = []
	for date_id, session_date in ids.items():
		bars, rows = tuple(raw.get(date_id, [])), tuple(derived.get(date_id, []))
		if _duplicates(bars, rows):
			raise ValueError("Duplicate database record keys")
		snapshots.append(SessionSnapshot(by_date[session_date], date_id, bars, rows, receipts.get(session_date)))
	return tuple(snapshots)


def _duplicates(bars: Sequence[DailyBar], rows: Sequence[LegacyDerivedRow]) -> bool:
	return len({bar.ticker for bar in bars}) != len(bars) or len({row.ticker for row in rows}) != len(rows)
