"""Compare Stage 12 SQL paths in a disposable MySQL schema."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4

from stockguidance.database.connections import MySQLConnectionFactory


TICKERS = 600
DAYS = 3
BATCH = 500
SERIES = ("price20", "price60", "delta20", "delta60")


def groups(values: list[int]) -> list[list[int]]:
	return [values[i:i + BATCH] for i in range(0, len(values), BATCH)]


def run(cursor: object, connection: object, *, batched: bool, defer_future: bool) -> dict[str, float | int]:
	for table in ("zscores", "daily_delta_stock_data", "raw_stock_data", "ticker_symbol"):
		cursor.execute(f"DROP TABLE IF EXISTS `{table}`")
	cursor.execute("CREATE TABLE ticker_symbol(id INT PRIMARY KEY,label VARCHAR(20) UNIQUE,exchange VARCHAR(8)) ENGINE=InnoDB")
	cursor.execute("CREATE TABLE raw_stock_data(date_id INT,ticker_id INT,close_price DOUBLE,PRIMARY KEY(date_id,ticker_id)) ENGINE=InnoDB")
	cursor.execute("CREATE TABLE daily_delta_stock_data(date_id INT,ticker_id INT,close_delta DOUBLE,PRIMARY KEY(date_id,ticker_id)) ENGINE=InnoDB")
	cursor.execute("CREATE TABLE zscores(date_id INT,series VARCHAR(20),ticker_id INT,zscore DOUBLE,PRIMARY KEY(date_id,series,ticker_id)) ENGINE=InnoDB")
	ids = list(range(1, TICKERS + 1))
	labels = [f"T{i:05d}" for i in ids]
	cursor.executemany("INSERT INTO ticker_symbol VALUES (%s,%s,%s)", [(i, label, "XNAS" if i % 2 else "XNYS") for i, label in zip(ids, labels)])
	cursor.executemany("INSERT INTO daily_delta_stock_data VALUES (%s,%s,%s)", [(day, i, 0.1) for day in range(1, DAYS + 1) for i in ids])
	cursor.executemany("INSERT INTO zscores VALUES (%s,%s,%s,%s)", [(day, series, i, 0.1) for day in range(1, DAYS + 1) for series in SERIES for i in ids])
	connection.commit()
	calls = 0
	start = perf_counter()
	if batched:
		resolved: dict[str, int] = {}
		for batch in groups(ids):
			cursor.execute(f"SELECT id,label FROM ticker_symbol WHERE label IN ({','.join('%s' for _ in batch)})", tuple(labels[i - 1] for i in batch))
			resolved.update((label, ticker_id) for ticker_id, label in cursor.fetchall())
			calls += 1
		for batch in groups(ids):
			cursor.executemany("INSERT INTO raw_stock_data VALUES (%s,%s,%s)", [(1, resolved[labels[i - 1]], float(i)) for i in batch])
			calls += 1
	else:
		for i, label in zip(ids, labels):
			cursor.execute("SELECT id FROM ticker_symbol WHERE label=%s", (label,))
			ticker_id = cursor.fetchall()[0][0]
			cursor.execute("INSERT INTO raw_stock_data VALUES (%s,%s,%s)", (1, ticker_id, float(i)))
			calls += 2
	raw_seconds = perf_counter() - start
	start = perf_counter()
	if batched:
		for batch in groups(ids):
			cursor.execute(f"DELETE FROM daily_delta_stock_data WHERE date_id=1 AND ticker_id IN ({','.join('%s' for _ in batch)})", tuple(batch))
			cursor.executemany("INSERT INTO daily_delta_stock_data VALUES (%s,%s,%s)", [(1, i, float(i) / 100) for i in batch])
			calls += 2
	else:
		for i in ids:
			cursor.execute("DELETE FROM daily_delta_stock_data WHERE date_id=1 AND ticker_id=%s", (i,))
			cursor.execute("INSERT INTO daily_delta_stock_data VALUES (%s,%s,%s)", (1, i, float(i) / 100))
			calls += 2
	delta_seconds = perf_counter() - start
	start = perf_counter()
	for day in range(1, 2 if defer_future else DAYS + 1):
		for series in SERIES:
			if batched:
				for batch in groups(ids):
					cursor.execute(f"DELETE FROM zscores WHERE date_id=%s AND series=%s AND ticker_id IN ({','.join('%s' for _ in batch)})", (day, series, *batch))
					cursor.executemany("INSERT INTO zscores VALUES (%s,%s,%s,%s)", [(day, series, i, float(i) / 100) for i in batch])
					calls += 2
			else:
				for i in ids:
					cursor.execute("DELETE FROM zscores WHERE date_id=%s AND series=%s AND ticker_id=%s", (day, series, i))
					cursor.execute("INSERT INTO zscores VALUES (%s,%s,%s,%s)", (day, series, i, float(i) / 100))
					calls += 2
	zscore_seconds = perf_counter() - start
	connection.commit()
	cursor.execute("SELECT COUNT(*),SUM(close_price) FROM raw_stock_data WHERE date_id=1")
	assert cursor.fetchall() == [(TICKERS, float(sum(ids)))]
	cursor.execute("SELECT COUNT(*) FROM zscores WHERE date_id=1")
	assert cursor.fetchall() == [(TICKERS * len(SERIES),)]
	return {"raw_seconds": raw_seconds, "delta_seconds": delta_seconds, "zscore_seconds": zscore_seconds, "total_seconds": raw_seconds + delta_seconds + zscore_seconds, "sql_api_calls": calls}


def main() -> None:
	connection = MySQLConnectionFactory()(Path("dbinfo.id"))
	cursor = connection.cursor()
	schema = "stockguidance_bench_" + uuid4().hex[:12]
	created = False
	try:
		cursor.execute(f"CREATE DATABASE `{schema}`")
		created = True
		cursor.execute(f"USE `{schema}`")
		results = {}
		for name, batched, defer_future in (("original", False, False), ("planner_only", False, True), ("batching_only", True, False), ("current", True, True)):
			runs = [run(cursor, connection, batched=batched, defer_future=defer_future) for _ in range(2)]
			results[name] = {key: median(float(item[key]) for item in runs) for key in runs[0]}
		print(json.dumps({"database": "disposable MySQL schema on the same server", "tickers": TICKERS, "dates": DAYS, "series_per_date": len(SERIES), "batch_size": BATCH, "repeats": 2, "results": results, "speedup_original_to_current": results["original"]["total_seconds"] / results["current"]["total_seconds"]}, indent=2))
	finally:
		if created:
			cursor.execute(f"DROP DATABASE `{schema}`")
		cursor.close()
		connection.close()


if __name__ == "__main__":
	main()
