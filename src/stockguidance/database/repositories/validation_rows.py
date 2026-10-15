from collections.abc import Sequence
from datetime import date

from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.repositories.market_data import ImportReceipt, SessionSnapshot
from stockguidance.database.repositories.mysql_market_read import decode_derived_row, decode_raw_row, decode_receipt
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.validation import DatabaseInspection, SessionRecordCounts
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow
from stockguidance.market_data.records import DailyBar
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding


def inspect_rows(cursor: Cursor, query: SessionQuery) -> DatabaseInspection:
	dates = tuple(session.session_date for session in query.sessions)
	placeholders = ",".join("%s" for session in query.sessions)
	findings: list[ValidationFinding] = []
	ids: dict[int, date] = {}
	seen_dates: set[date] = set()
	cursor.execute(f"SELECT id, transaction_date FROM transaction_dates WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date", dates)
	for row in cursor.fetchall():
		date_id, session_date = normalize_date_id(row[0]), normalize_session_date(row[1])
		if session_date not in dates:
			raise ValueError("Date mapping outside the bounded query")
		if date_id in ids or session_date in seen_dates:
			findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.DUPLICATE, "Duplicate date mapping", session_date, record_ids=(date_id,)))
		else:
			ids[date_id] = session_date
			seen_dates.add(session_date)
	raw: dict[int, dict[str, DailyBar]] = {}
	derived: dict[int, dict[str, LegacyDerivedRow]] = {}
	counts: dict[date, list[int]] = {session_date: [0, 0] for session_date in dates}
	ticker_symbols: dict[int, str] = {}
	symbol_ids: dict[str, int] = {}
	for table, columns in (("raw_stock_data", "r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions"), ("daily_delta_stock_data", ", ".join(f"r.{column}" for column in DERIVED_COLUMNS))):
		cursor.execute(f"SELECT r.date_id, r.ticker_id, t.transaction_date, s.label, {columns} FROM {table} r LEFT JOIN ticker_symbol s ON s.id = r.ticker_id LEFT JOIN transaction_dates t ON t.id = r.date_id WHERE t.transaction_date IN ({placeholders}) OR t.id IS NULL ORDER BY r.date_id, r.ticker_id", dates)
		for row in cursor.fetchall():
			try:
				ticker_id = normalize_date_id(row[1])
			except ValueError:
				ticker_id = None
			if ticker_id is not None and isinstance(row[3], str):
				if (ticker_id in ticker_symbols and ticker_symbols[ticker_id] != row[3]) or (row[3] in symbol_ids and symbol_ids[row[3]] != ticker_id):
					findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.DUPLICATE, "Ambiguous ticker identity", record_ids=(ticker_id,)))
				ticker_symbols[ticker_id] = row[3]
				symbol_ids[row[3]] = ticker_id
			if row[2] is not None:
				row_date = normalize_session_date(row[2])
				if row_date not in counts:
					raise ValueError("Record outside the bounded query")
				counts[row_date][0 if table == "raw_stock_data" else 1] += 1
			_read_record(row, table, ids, raw, derived, findings)
	receipts: dict[date, ImportReceipt] = {}
	cursor.execute(f"SELECT session_date, raw_id, available_at, derived_dependency_id, derived_content_id FROM stockguidance_imports WHERE session_date IN ({placeholders})", dates)
	for row in cursor.fetchall():
		session_date = normalize_session_date(row[0])
		if session_date not in dates:
			raise ValueError("Receipt outside the bounded query")
		if session_date not in seen_dates:
			findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.ORPHAN, "Import receipt has no date mapping", session_date))
		try:
			decoded_date, receipt = decode_receipt(row)
			if decoded_date in receipts:
				findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.DUPLICATE, "Duplicate import receipt", session_date))
			else:
				receipts[decoded_date] = receipt
		except (ValueError, TypeError, OverflowError):
			findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.IMPORT, "Invalid import receipt identity or availability", session_date))
	sessions = {session.session_date: session for session in query.sessions}
	snapshots: list[SessionSnapshot] = []
	for date_id, session_date in ids.items():
		snapshot_receipt = receipts.get(session_date)
		if snapshot_receipt is not None and snapshot_receipt.available_at < sessions[session_date].closes_at:
			findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.IMPORT, "Daily receipt predates the session close", session_date))
			snapshot_receipt = None
		snapshots.append(SessionSnapshot(sessions[session_date], date_id, tuple(raw.get(date_id, {}).values()), tuple(derived.get(date_id, {}).values()), snapshot_receipt))
	return DatabaseInspection(tuple(snapshots), tuple(findings), tuple(SessionRecordCounts(session_date, count[0], count[1]) for session_date, count in counts.items()), tuple(sorted(ticker_symbols.items())))


def _read_record(row: Sequence[object], table: str, ids: dict[int, date], raw: dict[int, dict[str, DailyBar]], derived: dict[int, dict[str, LegacyDerivedRow]], findings: list[ValidationFinding]) -> None:
	session_date = None if row[2] is None else normalize_session_date(row[2])
	tickers = (row[3],) if isinstance(row[3], str) else ()
	try:
		date_id = normalize_date_id(row[0])
		ticker_id = normalize_date_id(row[1])
	except ValueError:
		findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.ORPHAN, f"{table} record has invalid mapping identifiers", session_date, tickers, tuple(value for value in row[:2] if type(value) is int)))
		return
	if date_id not in ids or not tickers:
		findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.ORPHAN, f"{table} record lacks a valid date or ticker mapping; unassigned dates are checked across storage", session_date, tickers, (date_id, ticker_id)))
		return
	try:
		if table == "raw_stock_data":
			decoded_id, bar = decode_raw_row((row[0], row[3], *row[4:]))
			bars = raw.setdefault(decoded_id, {})
			if bar.ticker in bars:
				findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.DUPLICATE, "Duplicate raw source key", session_date, tickers, (date_id, ticker_id)))
			else:
				bars[bar.ticker] = bar
		else:
			decoded_id, derived_row = decode_derived_row((row[0], row[3], *row[4:]))
			rows = derived.setdefault(decoded_id, {})
			if derived_row.ticker in rows:
				findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.DUPLICATE, "Duplicate derived source key", session_date, tickers, (date_id, ticker_id)))
			else:
				rows[derived_row.ticker] = derived_row
	except (ValueError, TypeError, OverflowError):
		check = ValidationCheck.RAW_VALUES if table == "raw_stock_data" else ValidationCheck.DERIVED
		findings.append(ValidationFinding(FindingCategory.INVALID, check, f"Invalid {table} prices, counts, or numerical values", session_date, tickers, (date_id, ticker_id)))
