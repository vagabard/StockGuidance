"""Disposable SQLite benchmark of Stage 12 row-at-a-time and batched SQL paths."""

from __future__ import annotations

import json
from statistics import median
from time import perf_counter
import sqlite3


TICKERS = 1500
DAYS = 5
BATCH = 500
SERIES = ("price20", "price60", "delta20", "delta60")


def chunks(values: list[int]) -> list[list[int]]:
	return [values[i:i + BATCH] for i in range(0, len(values), BATCH)]


def run(*, batched: bool, defer_future: bool) -> dict[str, float | int]:
	# An in-memory SQLite database is created afresh for every timed run.
	db = sqlite3.connect(":memory:")
	db.executescript("""
	CREATE TABLE ticker_symbol(id INTEGER PRIMARY KEY, label TEXT UNIQUE, exchange TEXT);
	CREATE TABLE raw_stock_data(date_id INTEGER, ticker_id INTEGER, close REAL, PRIMARY KEY(date_id,ticker_id));
	CREATE TABLE daily_delta_stock_data(date_id INTEGER, ticker_id INTEGER, value REAL, PRIMARY KEY(date_id,ticker_id));
	CREATE TABLE zscores(date_id INTEGER, series TEXT, ticker_id INTEGER, value REAL, PRIMARY KEY(date_id,series,ticker_id));
	""")
	labels = [f"T{i:05d}" for i in range(TICKERS)]
	ids = list(range(1, TICKERS + 1))
	db.executemany("INSERT INTO ticker_symbol VALUES (?,?,?)", [(i, label, "XNAS" if i % 2 else "XNYS") for i, label in enumerate(labels, 1)])
	db.executemany("INSERT INTO daily_delta_stock_data VALUES (?,?,?)", [(day, i, 0.1) for day in range(1, DAYS + 1) for i in ids])
	db.executemany("INSERT INTO zscores VALUES (?,?,?,?)", [(day, series, i, 0.1) for day in range(1, DAYS + 1) for series in SERIES for i in ids])
	db.commit()
	cursor = db.cursor()
	calls = 0
	start = perf_counter()
	if batched:
		resolved: dict[str, int] = {}
		for group in chunks(ids):
			for ticker_id, label in cursor.execute(f"SELECT id,label FROM ticker_symbol WHERE label IN ({','.join('?' for _ in group)})", [labels[i - 1] for i in group]):
				resolved[label] = ticker_id
			calls += 1
		for group in chunks(ids):
			cursor.executemany("INSERT INTO raw_stock_data VALUES (?,?,?)", [(1, resolved[labels[i - 1]], float(i)) for i in group])
			calls += 1
	else:
		for i, label in enumerate(labels, 1):
			ticker_id = cursor.execute("SELECT id FROM ticker_symbol WHERE label=?", (label,)).fetchone()[0]
			cursor.execute("INSERT INTO raw_stock_data VALUES (?,?,?)", (1, ticker_id, float(i)))
			calls += 2
	raw_seconds = perf_counter() - start
	start = perf_counter()
	if batched:
		for group in chunks(ids):
			cursor.execute(f"DELETE FROM daily_delta_stock_data WHERE date_id=1 AND ticker_id IN ({','.join('?' for _ in group)})", group)
			cursor.executemany("INSERT INTO daily_delta_stock_data VALUES (?,?,?)", [(1, i, float(i) / 100) for i in group])
			calls += 2
	else:
		for i in ids:
			cursor.execute("DELETE FROM daily_delta_stock_data WHERE date_id=1 AND ticker_id=?", (i,))
			cursor.execute("INSERT INTO daily_delta_stock_data VALUES (?,?,?)", (1, i, float(i) / 100))
			calls += 2
	delta_seconds = perf_counter() - start
	start = perf_counter()
	for day in range(1, 2 if defer_future else DAYS + 1):
		for series in SERIES:
			if batched:
				for group in chunks(ids):
					cursor.execute(f"DELETE FROM zscores WHERE date_id=? AND series=? AND ticker_id IN ({','.join('?' for _ in group)})", [day, series, *group])
					cursor.executemany("INSERT INTO zscores VALUES (?,?,?,?)", [(day, series, i, float(i) / 100) for i in group])
					calls += 2
			else:
				for i in ids:
					cursor.execute("DELETE FROM zscores WHERE date_id=? AND series=? AND ticker_id=?", (day, series, i))
					cursor.execute("INSERT INTO zscores VALUES (?,?,?,?)", (day, series, i, float(i) / 100))
					calls += 2
	zscore_seconds = perf_counter() - start
	db.commit()
	assert cursor.execute("SELECT COUNT(*),SUM(close) FROM raw_stock_data WHERE date_id=1").fetchone() == (TICKERS, float(sum(ids)))
	assert cursor.execute("SELECT COUNT(*) FROM zscores WHERE date_id=1").fetchone()[0] == TICKERS * len(SERIES)
	db.close()
	return {"raw_seconds": raw_seconds, "delta_seconds": delta_seconds, "zscore_seconds": zscore_seconds, "total_seconds": raw_seconds + delta_seconds + zscore_seconds, "sql_api_calls": calls}


def main() -> None:
	results = {}
	for name, batched, defer_future in (("original", False, False), ("planner_only", False, True), ("batching_only", True, False), ("current", True, True)):
		runs = [run(batched=batched, defer_future=defer_future) for _ in range(3)]
		results[name] = {key: median(float(item[key]) for item in runs) for key in runs[0]}
	print(json.dumps({"database": "disposable in-memory SQLite", "tickers": TICKERS, "dates": DAYS, "series_per_date": len(SERIES), "batch_size": BATCH, "repeats": 3, "results": results, "speedup_original_to_current": results["original"]["total_seconds"] / results["current"]["total_seconds"]}, indent=2))


if __name__ == "__main__":
	main()
