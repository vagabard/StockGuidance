from collections.abc import Sequence
from datetime import date, datetime
import math

from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.repositories.market_data import ImportReceipt, SessionSnapshot
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.table_names import TableNames, TableRole, quote_identifier
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow
from stockguidance.market_data.records import DailyBar, count_value, decimal_price, decimal_quantity


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
	return normalize_date_id(row[0]), DailyBar(row[1], decimal_price(row[2]), decimal_price(row[3]), decimal_price(row[4]), decimal_price(row[5]), decimal_quantity(row[6]), None if row[7] is None else decimal_price(row[7]), None if row[8] is None else count_value(row[8]))


def decode_derived_row(row: Sequence[object]) -> tuple[int, LegacyDerivedRow]:
	if len(row) != len(DERIVED_COLUMNS) + 2 or not isinstance(row[1], str):
		raise ValueError("Invalid derived record layout")
	return normalize_date_id(row[0]), LegacyDerivedRow(row[1], tuple(_derived_value(value) for value in row[2:]))


def decode_receipt(row: Sequence[object]) -> tuple[date, ImportReceipt]:
	if len(row) != 5 or not isinstance(row[1], str) or not isinstance(row[2], str):
		raise ValueError("Invalid import receipt layout")
	return normalize_session_date(row[0]), ImportReceipt(row[1], datetime.fromisoformat(row[2]), _optional_text(row[3]), _optional_text(row[4]))


def read_snapshots(cursor: Cursor, query: SessionQuery, table_names: TableNames) -> tuple[SessionSnapshot, ...]:
	dates = tuple(session.session_date for session in query.sessions)
	placeholders = ",".join("%s" for session in query.sessions)
	cursor.execute(f"SELECT id, transaction_date FROM {quote_identifier(table_names.table(TableRole.SESSIONS))} WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date", dates)
	date_rows = cursor.fetchall()
	ids: dict[int, date] = {}
	for row in date_rows:
		if len(row) != 2:
			raise ValueError("Invalid date layout")
		date_id, session_date = normalize_date_id(row[0]), normalize_session_date(row[1])
		if date_id in ids or session_date in ids.values() or session_date not in dates:
			raise ValueError("Duplicate or out-of-scope date mapping")
		ids[date_id] = session_date
	query_tail = f" FROM {{table}} r JOIN {quote_identifier(table_names.table(TableRole.TICKERS))} s ON s.id = r.ticker_id JOIN {quote_identifier(table_names.table(TableRole.SESSIONS))} t ON t.id = r.date_id WHERE t.transaction_date IN ({placeholders}) ORDER BY t.transaction_date, s.label"
	cursor.execute("SELECT r.date_id, s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions" + query_tail.format(table=quote_identifier(table_names.table(TableRole.PRICES))), dates)
	raw: dict[int, list[DailyBar]] = {}
	for row in cursor.fetchall():
		date_id, bar = decode_raw_row(row)
		raw.setdefault(date_id, []).append(bar)
	cursor.execute("SELECT r.date_id, s.label, " + ", ".join(f"r.{column}" for column in DERIVED_COLUMNS) + query_tail.format(table=quote_identifier(table_names.table(TableRole.DELTAS))), dates)
	derived: dict[int, list[LegacyDerivedRow]] = {}
	for row in cursor.fetchall():
		date_id, derived_row = decode_derived_row(row)
		derived.setdefault(date_id, []).append(derived_row)
	cursor.execute(f"SELECT session_date, raw_id, available_at, derived_dependency_id, derived_content_id FROM {quote_identifier(table_names.table(TableRole.IMPORTS))} WHERE session_date IN ({placeholders})", dates)
	receipts: dict[date, ImportReceipt] = {}
	for row in cursor.fetchall():
		session_date, receipt = decode_receipt(row)
		if session_date in receipts or session_date not in dates:
			raise ValueError("Invalid receipt scope or timestamp")
		receipts[session_date] = receipt
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
