"""
Populate MySQL tables `transaction_dates` and `raw_stock_data` from NDJSON market summary files
under `data/market_summary/YYYY/*.ndjson`.

Connection is created with `db_connect.get_connection`. Typical usage:

  python populate_db_raw_stock_info.py \
	  --user Redbeard --password "3YZs#z5GOJKdea" --host localhost

Notes on schema and mapping:
- Table: transaction_dates
	id INT AUTO_INCREMENT PRIMARY KEY
	transaction_date DATE NOT NULL UNIQUE

- Table: raw_stock_data
	ticker_id INT NOT NULL
	date_id INT NOT NULL
	open DECIMAL(19,4)
	high DECIMAL(19,4)
	low DECIMAL(19,4)
	close DECIMAL(19,4)
	volume BIGINT UNSIGNED
	vwap DECIMAL(19,4)
	transactions BIGINT
	PRIMARY KEY (ticker_id, date_id)
	FOREIGN KEYs to `ticker_symbol(id)` and `transaction_dates(id)` with CASCADE updates

- Ticker symbol mapping: The NDJSON field `T` (ticker) is matched to `ticker_symbol.ticker_symbol`.
  The repository already defines table `ticker_symbol` in `populate_db_ticker_info.py`.

- Date mapping: Each NDJSON file name is `YYYY-MM-DD.ndjson` and represents one trading day.
  That date is inserted into `transaction_dates` (if missing) and used as `date_id` for rows
  from that file. The NDJSON field `t` (epoch millis) is not used for keying but should correspond
  to the same calendar date.

- Idempotency: Insertion into `raw_stock_data` uses ON DUPLICATE KEY UPDATE, making the script safe
  to re-run. Unknown tickers (not present in `ticker_symbol`) are skipped with a warning the first
  time they are encountered during a run.

- Performance: Rows are buffered and inserted in batches per file.

Input file format (Polygon aggregate/summary NDJSON), per line example:
  {"T":"AAPL","v":62371161.0,"vw":181.474,"o":181.99,"c":181.18,
   "h":182.76,"l":180.17,"t":1704488400000,"n":682334}

Fields used:
  T -> ticker
  o -> open
  h -> high
  l -> low
  c -> close
  v -> volume (float or int in source; stored as BIGINT UNSIGNED rounded to int)
  vw -> vwap
  n -> transactions

"""
from __future__ import annotations

import argparse
import json
import os
import re
import traceback
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from database.db_connect import get_connection

DEFAULT_ROOT = Path("../data") / "market_summary"
BATCH_SIZE = 1000
MAX_STOCK_PRICE = 1000000


def d4(val: Optional[float]) -> Optional[Decimal]:
	if val is None:
		return None
	try:
		# Convert to Decimal with 4 places
		d = Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
		return d
	except Exception:
		return None


def to_int(val) -> Optional[int]:
	if val is None:
		return None
	try:
		# Some values in files may be floats; round to nearest int
		n = int(Decimal(str(val)).to_integral_value(rounding=ROUND_HALF_UP))
		if n < 0:
			n = 0
		return n
	except Exception:
		return None


def ensure_schema(cursor) -> None:
	# transaction_dates
	cursor.execute(
		"""
		CREATE TABLE IF NOT EXISTS transaction_dates (
			id INT NOT NULL AUTO_INCREMENT,
			transaction_date DATE NOT NULL,
			PRIMARY KEY (id),
			UNIQUE KEY ux_transaction_date (transaction_date)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
		"""
	)

	# raw_stock_data
	cursor.execute(
		"""
		CREATE TABLE IF NOT EXISTS raw_stock_data (
			ticker_id INT NOT NULL,
			date_id INT NOT NULL,
			open DECIMAL(19,4) NULL,
			high DECIMAL(19,4) NULL,
			low DECIMAL(19,4) NULL,
			close DECIMAL(19,4) NULL,
			volume BIGINT UNSIGNED NULL,
			vwap DECIMAL(19,4) NULL,
			transactions BIGINT NULL,
			PRIMARY KEY (ticker_id, date_id),
			KEY ix_rsp_date (date_id),
			CONSTRAINT fk_rsp_ticker FOREIGN KEY (ticker_id)
				REFERENCES ticker_symbol(id)
				ON DELETE RESTRICT
				ON UPDATE CASCADE,
			CONSTRAINT fk_rsp_date FOREIGN KEY (date_id)
				REFERENCES transaction_dates(id)
				ON DELETE RESTRICT
				ON UPDATE CASCADE
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
		"""
	)


def load_ticker_map(cursor) -> Dict[str, int]:
	cursor.execute("SELECT id, label FROM ticker_symbol")
	result: Dict[str, int] = {}
	for row in cursor.fetchall():
		tid, sym = row[0], row[1]
		if sym:
			result[str(sym).upper()] = int(tid)
	print(f"Loaded {len(result)} tickers from ticker_symbol table.")
	if result:
		print(f"largest {max(result.values())}")
	return result


def add_new_ticker(cursor, ticker_label: str,  ticker_map: Dict[str, int]) -> int:
	cursor.execute(
		"INSERT INTO ticker_symbol (label) VALUES (%s)",
		(ticker_label,),
	)
	# Last insert id via SELECT to be safe across drivers
	cursor.execute(
		"SELECT id FROM ticker_symbol WHERE label = %s",
		(ticker_label,),
	)
	row = cursor.fetchone()
	ticker_id = row[0]
	ticker_map[ticker_label] = ticker_id
	if row:
		return int(ticker_id)
	raise RuntimeError(f"Failed to retrieve ID for new ticker: {ticker_label}")


DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.ndjson$", re.IGNORECASE)


def discover_files(root: Path) -> List[Tuple[str, Path]]:
	"""Return list of (date_str, path) for all *.ndjson under root subfolders.

	date_str is YYYY-MM-DD from the filename.
	"""
	items: List[Tuple[str, Path]] = []
	if not root.exists():
		return items
	for year_dir in sorted([p for p in root.iterdir() if p.is_dir()]):
		for f in sorted(year_dir.glob("*.ndjson")):
			m = DATE_FILE_RE.match(f.name)
			if not m:
				continue
			items.append((m.group(1), f))
	return items


def get_or_create_date_id(cursor, date_str: str) -> int:
	cursor.execute(
		"SELECT id FROM transaction_dates WHERE transaction_date = %s",
		(date_str,),
	)
	row = cursor.fetchone()
	if row:
		return int(row[0])
	cursor.execute(
		"INSERT INTO transaction_dates (transaction_date) VALUES (%s)",
		(date_str,),
	)
	# Last insert id via SELECT to be safe across drivers
	cursor.execute(
		"SELECT id FROM transaction_dates WHERE transaction_date = %s",
		(date_str,),
	)
	row = cursor.fetchone()
	return int(row[0])


def batch_insert_raw_stock_data(cursor, rows: List[Tuple[int, int, Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[int], Optional[Decimal], Optional[int]]]):
	if not rows:
		return
	sql = (
		f"INSERT INTO raw_stock_data (ticker_id, date_id, open, high, low, close, volume, vwap, transactions) "
		f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
		f"ON DUPLICATE KEY UPDATE "
		f"open=VALUES(open), "
		f"high=VALUES(high), "
		f"low=VALUES(low), "
		f"close=VALUES(close), "
		f"volume=VALUES(volume), "
		f"vwap=VALUES(vwap), "
		f"transactions=VALUES(transactions);"
	)
	print(f"batch_insert_raw_stock_data: {len(rows)} rows")
	# print(f"sql is {sql}")
	# print(f"rows are {rows}")
	sql_return = cursor.executemany(sql, rows)

	# Different drivers report affected rows differently.
	# - PyMySQL: executemany() returns an int total affected rows.
	# - mysql-connector: executemany() returns None; cursor.rowcount holds the
	#   affected-row count for the *last* statement in the batch.
	affected = sql_return
	if affected is None:
		affected = getattr(cursor, "rowcount", None)
	if affected is None:
		print(f"Warning: Could not determine affected-row count for {len(rows)} rows")
	else:
		print(f"Rows affected by batch insert: {affected}")


def process_file(conn, cursor, path: Path, date_str: str, ticker_map: Dict[str, int]) -> Tuple[int, int, int]:
	"""Process a single NDJSON file.

	Returns (inserted_or_updated_rows, skipped_unknown_ticker, total_lines)
	"""
	date_id = get_or_create_date_id(cursor, date_str)

	rows_buffer: List[Tuple[int, int, Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[int], Optional[Decimal], Optional[int]]] = []
	inserted = 0
	skipped = 0

	warned: set[str] = set()

	with path.open("r", encoding="utf-8") as f:
		for line_no, line in enumerate(f, start=1):
			line = line.strip()
			if not line:
				continue
			try:
				obj = json.loads(line)
			except json.JSONDecodeError:
				# Skip bad line
				skipped += 1
				continue

			ticker = str(obj.get("T") or "").upper()
			if not ticker:
				skipped += 1
				continue

			tid = ticker_map.get(ticker)
			if tid is None:
				tid = add_new_ticker(cursor, ticker, ticker_map)
				# if ticker not in warned:
				# 	#print(f"[WARN] Unknown ticker in DB (skipping): {ticker}")
				# 	warned.add(ticker)
				# skipped += 1
				continue

			o = d4(obj.get("o"))
			h = d4(obj.get("h"))
			l = d4(obj.get("l"))
			c = d4(obj.get("c"))
			v = to_int(obj.get("v"))
			vw = d4(obj.get("vw"))
			n = to_int(obj.get("n"))
			#sanity check
			if o < MAX_STOCK_PRICE and h < MAX_STOCK_PRICE and l < MAX_STOCK_PRICE and c < MAX_STOCK_PRICE:
				rows_buffer.append((tid, date_id, o, h, l, c, v, vw, n))
			else :
				print(f"[WARN] Skipping stock price out of range: {ticker} {o} {h} {l} {c} {v} {vw} {n}")
			if len(rows_buffer) >= BATCH_SIZE:
				batch_insert_raw_stock_data(cursor, rows_buffer)
				inserted += len(rows_buffer)
				rows_buffer.clear()
	print("warned tickers: ", warned)
	if rows_buffer:
		batch_insert_raw_stock_data(cursor, rows_buffer)
		inserted += len(rows_buffer)

	return inserted, skipped, line_no if 'line_no' in locals() else 0


def parse_args(argv: Optional[List[str]] = None):
	p = argparse.ArgumentParser(description="Populate raw_stock_data from market summary NDJSON files.")
	p.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")
	p.add_argument("--root", default=str(DEFAULT_ROOT), help="Root folder for market summary files (default: data/market_summary)")
	p.add_argument(
		"--start-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD). If provided, only NDJSON files with "
			"a date strictly more recent than this value will be processed."
		),
	)
	p.add_argument(
		"--end-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD). If provided, only NDJSON files with "
			"a date strictly less recent than this value will be processed."
		),
	)
	p.add_argument("--limit", type=int, default=0, help="Limit number of files to process (0 = all)")
	return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
	args = parse_args(argv)

	root = Path(args.root)
	files = discover_files(root)

	# If a starting date is provided, restrict processing to files strictly
	# after that calendar date.  This allows incremental backfills driven by
	# the latest date already present in raw_stock_data.
	if args.start_date:
		try:
			start_d = datetime.fromisoformat(args.start_date).date()
		except ValueError:
			raise SystemExit(f"Invalid --start-date value (expected YYYY-MM-DD): {args.start_date}")

		filtered: List[Tuple[str, Path]] = []
		for date_str, path in files:
			try:
				file_d = datetime.fromisoformat(date_str).date()
			except ValueError:
				# Filenames are discovered via DATE_FILE_RE, but be defensive.
				continue
			if file_d > start_d:
				filtered.append((date_str, path))
		files = filtered
	if args.end_date:
		try:
			end_d = datetime.fromisoformat(args.end_date).date()
		except ValueError:
			raise SystemExit(f"Invalid --start-date value (expected YYYY-MM-DD): {args.end_date}")

		filtered: List[Tuple[str, Path]] = []
		for date_str, path in files:
			try:
				file_d = datetime.fromisoformat(date_str).date()
			except ValueError:
				# Filenames are discovered via DATE_FILE_RE, but be defensive.
				continue
			if file_d < end_d:
				filtered.append((date_str, path))
		files = filtered
	if args.limit and args.limit > 0:
		files = files[: args.limit]

	if not files:
		print(f"No NDJSON files found under: {root}")
		return

	conn = get_connection(dbinfo_file=args.dbinfo_file)

	cursor = conn.cursor()
	try:
		ensure_schema(cursor)
		ticker_map = load_ticker_map(cursor)
		print(f"keys in ticker_map: {ticker_map.keys()}")
		# if not ticker_map:
		# 	print("[ERROR] No tickers loaded in ticker_symbol table. Run populate_db_ticker_info.py first.")
		# 	return

		total_files = len(files)
		grand_total = 0
		grand_skipped = 0
		for idx, (date_str, path) in enumerate(files, start=1):
			print(f"[{idx}/{total_files}] Processing {path} (date {date_str}) ...")
			inserted, skipped, total_lines = process_file(conn, cursor, path, date_str, ticker_map)
			grand_total += inserted
			grand_skipped += skipped
			print(f"    Lines: {total_lines}, inserted/updated: {inserted}, skipped: {skipped}")

		print(f"Done. Files: {total_files}, rows inserted/updated: {grand_total}, skipped: {grand_skipped}")
	except Exception as e:
		traceback.print_exception(e)
		print(f"[ERROR] {e}")
	finally:
		try:
			cursor.close()
		except Exception:
			pass
		try:
			conn.close()
		except Exception:
			pass


if __name__ == "__main__":
	main()
