from datetime import date

from stockguidance.database.repositories.mysql_market_read import _dates, _receipts
from stockguidance.database.repositories.quality import invalidate, read_quality, save_quality
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.database.dates import normalize_date_id
from stockguidance.market_data.quality import FIELDS, ObservedBar, interpolate


def reconcile_quality(cursor: Cursor, query: SessionQuery, names: TableNames, *, scan: bool = True, ticker_filter: frozenset[int] | None = None) -> tuple[date, ...]:
	ids = _dates(cursor, query, names)
	if not ids:
		return ()
	sessions = {session.session_date: session for session in query.sessions}
	date_ids = tuple(ids)
	placeholders = ",".join("%s" for _ in date_ids)
	known = read_quality(cursor, date_ids)
	if ticker_filter is not None:
		known = {key: record for key, record in known.items() if key[1] in ticker_filter}
	tickers = {ticker_id for _, ticker_id in known}
	if scan:
		condition = " OR ".join(f"{field} IS NULL OR {field} <= 0" for field in FIELDS[:4])
		condition += " OR volume IS NULL OR volume < 0 OR vwap <= 0 OR transactions < 0 OR low > open OR low > close OR high < open OR high < close OR low > high"
		cursor.execute(f"SELECT DISTINCT ticker_id FROM {quote_identifier(names.prices)} WHERE date_id IN ({placeholders}) AND ({condition})", date_ids)
		tickers.update(normalize_date_id(row[0]) for row in cursor.fetchall())
	if not tickers:
		return ()
	cursor.execute(f"SELECT id, label FROM {quote_identifier(names.tickers)}", ())
	labels = {normalize_date_id(row[0]): str(row[1]) for row in cursor.fetchall()}
	receipts = _receipts(cursor, tuple(ids.values()), names)
	changed: set[date] = set()
	ticker_ids = tuple(sorted(tickers))
	for offset in range(0, len(ticker_ids), 100):
		observed = []
		keys = {}
		batch = ticker_ids[offset:offset + 100]
		ticker_placeholders = ",".join("%s" for _ in batch)
		cursor.execute(f"SELECT date_id, ticker_id, {', '.join(FIELDS)} FROM {quote_identifier(names.prices)} WHERE date_id IN ({placeholders}) AND ticker_id IN ({ticker_placeholders}) ORDER BY ticker_id, date_id FOR UPDATE", date_ids + batch)
		stored_rows = cursor.fetchall()
		current = read_quality(cursor, date_ids, for_update=True, ticker_ids=batch)
		known = {key: record for key, record in known.items() if key[1] not in batch}
		known.update(current)
		for row in stored_rows:
			date_id, ticker_id = normalize_date_id(row[0]), normalize_date_id(row[1])
			session = sessions[ids[date_id]]
			receipt = receipts.get(session.session_date)
			previous = known.get((date_id, ticker_id))
			values = tuple(row[2:])
			available = query.as_of if receipt is None else receipt.available_at
			if previous is not None:
				values = previous.observation.values
				available = previous.observation.available_at
			item = ObservedBar(labels[ticker_id], session.session_date, session.sequence, values, available)
			observed.append(item)
			keys[(item.ticker, item.session_date)] = (date_id, ticker_id)
		for record in interpolate(tuple(observed), query.as_of):
			date_id, ticker_id = keys[(record.observation.ticker, record.observation.session_date)]
			previous = known.get((date_id, ticker_id))
			if previous is not None and previous.effective is not None and (previous.prior not in sessions or previous.following not in sessions):
				continue
			if save_quality(cursor, date_id, ticker_id, record, query.as_of):
				changed.add(ids[date_id])
	sequences = {sessions[value].sequence for value in changed}
	dependent_ids = tuple(date_id for date_id, session_date in ids.items() if any(abs(sessions[session_date].sequence - sequence) <= 1 for sequence in sequences))
	invalidate(cursor, names, dependent_ids)
	return tuple(sorted(changed))
