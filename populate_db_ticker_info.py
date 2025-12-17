"""
Populate MySQL tables from data/tickers_full.txt using db_connect.get_connection.

Tables (created if they don't exist):
- ticker_symbol
  - id INT AUTO_INCREMENT PRIMARY KEY
  - ticker_symbol VARCHAR(16) UNIQUE NOT NULL
  - exchange VARCHAR(10)
  - type VARCHAR(10)
  - market VARCHAR(20)

- ticker_extended_info
  - ticker_id INT PRIMARY KEY REFERENCES ticker_symbol(id)
  - name VARCHAR(255)
  - locale VARCHAR(10)
  - currency_name VARCHAR(20)
  - active TINYINT(1)
  - cik VARCHAR(10)
  - composite_figi VARCHAR(12)
  - share_class_figi VARCHAR(12)
  - last_updated DATETIME

Usage examples:
  python populate_db_ticker_info.py \
	  --user Redbeard --password "3YZs#z5GOJKdea" --host localhost

  python populate_db_ticker_info.py --file data/tickers_full.txt --limit 1000

Notes:
- The input file is expected to be JSON-Lines (one JSON object per line) with
  keys like those produced by Polygon: ticker, name, market, locale,
  primary_exchange, type, active, currency_name, cik, composite_figi,
  share_class_figi, last_updated_utc.
- The connection uses autocommit; operations are performed row-by-row.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database.db_connect import get_connection

DEFAULT_INPUT_FILE = os.path.join("data", "tickers_full.txt")


def _ensure_schema(cursor) -> None:
	cursor.execute(
		"""
		CREATE TABLE IF NOT EXISTS ticker_symbol (
			id INT NOT NULL AUTO_INCREMENT,
			ticker_symbol VARCHAR(16) NOT NULL,
			exchange VARCHAR(10) NULL,
			type VARCHAR(10) NULL,
			market VARCHAR(20) NULL,
			PRIMARY KEY (id),
			UNIQUE KEY ux_ticker_symbol (ticker_symbol)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
		"""
	)

	cursor.execute(
		"""
		CREATE TABLE IF NOT EXISTS ticker_extended_info (
			ticker_id INT NOT NULL,
			name VARCHAR(255) NULL,
			locale VARCHAR(10) NULL,
			currency_name VARCHAR(20) NULL,
			active TINYINT(1) NULL,
			cik VARCHAR(10) NULL,
			composite_figi VARCHAR(12) NULL,
			share_class_figi VARCHAR(12) NULL,
			last_updated DATETIME NULL,
			PRIMARY KEY (ticker_id),
			CONSTRAINT fk_ticker_ext_symbol FOREIGN KEY (ticker_id)
				REFERENCES ticker_symbol(id)
				ON DELETE CASCADE
				ON UPDATE CASCADE
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
		"""
	)


def _parse_dt(val: Optional[str]) -> Optional[str]:
	"""Convert ISO-8601 UTC string to MySQL DATETIME string (YYYY-MM-DD HH:MM:SS).

	Examples of input:
	  2025-11-10T07:05:23.290883129Z
	  2025-11-10T07:05:23Z
	Returns None if input is falsy or malformed.
	"""
	if not val:
		return None
	try:
		s = val.strip()
		if s.endswith("Z"):
			s = s[:-1]
			tz = timezone.utc
		else:
			tz = None
		# Trim fractional seconds to microseconds for fromisoformat
		if "." in s:
			left, right = s.split(".", 1)
			# keep up to 6 digits for microseconds
			frac = ''.join(ch for ch in right if ch.isdigit())[:6]
			s = left + ("." + frac if frac else "")
		dt = datetime.fromisoformat(s)
		if tz is not None:
			dt = dt.replace(tzinfo=timezone.utc)
			# store as naive UTC in MySQL DATETIME
			dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
		return dt.strftime("%Y-%m-%d %H:%M:%S")
	except Exception:
		return None


def _bool_to_tinyint(v: Any) -> int:
	return 1 if bool(v) else 0


def process_file(
	conn,
	input_file: str,
	limit: Optional[int] = None,
	skip_bad_lines: bool = True,
) -> dict:
	cursor = conn.cursor()
	_ensure_schema(cursor)

	# Prepared statements
	insert_symbol_sql = (
		"""
		INSERT INTO ticker_symbol (label, exchange, type, market)
		VALUES (%s, %s, %s, %s)
		ON DUPLICATE KEY UPDATE
			id = LAST_INSERT_ID(id),
			exchange = VALUES(exchange),
			type = VALUES(type),
			market = VALUES(market)
		"""
	)

	insert_ext_sql = (
		"""
		INSERT INTO ticker_extended_info (
			ticker_id, name, locale, currency_name, active, cik,
			composite_figi, share_class_figi, last_updated
		) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
		ON DUPLICATE KEY UPDATE
			name = VALUES(name),
			locale = VALUES(locale),
			currency_name = VALUES(currency_name),
			active = VALUES(active),
			cik = VALUES(cik),
			composite_figi = VALUES(composite_figi),
			share_class_figi = VALUES(share_class_figi),
			last_updated = VALUES(last_updated)
		"""
	)

	processed = 0
	inserted_symbols = 0
	updated_symbols = 0
	upserted_extended = 0
	errors = 0

	def norm_len(s: Optional[str], max_len: int) -> Optional[str]:
		if s is None:
			return None
		s2 = str(s)
		if len(s2) > max_len:
			return s2[:max_len]
		return s2

	with open(input_file, "r", encoding="utf-8") as f:
		for line in f:
			if limit is not None and processed >= limit:
				break
			line = line.strip()
			if not line:
				continue
			try:
				obj: Dict[str, Any] = json.loads(line)
			except json.JSONDecodeError:
				errors += 1
				print(f"Error decoding JSON: {line} , {json.JSONDecodeError}")
				if skip_bad_lines:
					continue
				else:
					raise

			ticker = obj.get("ticker")
			if not ticker:
				errors += 1
				if skip_bad_lines:
					continue
				else:
					raise ValueError("Missing 'ticker' in line JSON")

			exchange = obj.get("primary_exchange")
			ttype = obj.get("type")
			market = obj.get("market")

			# Normalize lengths
			ticker_v = norm_len(ticker, 16)
			exchange_v = norm_len(exchange, 10)
			type_v = norm_len(ttype, 10)
			market_v = norm_len(market, 20)

			try:
				cursor.execute(
					insert_symbol_sql, (ticker_v, exchange_v, type_v, market_v)
				)
				last_id = getattr(cursor, "lastrowid", None)
				if not last_id:
					# Fallback: read id
					cursor.execute(
						"SELECT id FROM ticker_symbol WHERE ticker_symbol=%s", (ticker_v,)
					)
					row = cursor.fetchone()
					if not row:
						raise RuntimeError("Unable to obtain ticker id for " + str(ticker_v))
					ticker_id = row[0]
					updated_symbols += 1
				else:
					ticker_id = int(last_id)
					# We can't differentiate insert vs update perfectly across drivers here.
					# If an existing row caused duplicate key, lastrowid becomes the matched id.
					updated_symbols += 0  # no-op; we'll count upserts via extended table
					inserted_symbols += 0  # conservative
			except Exception:
				print(f"Error inserting ticker symbol: {ticker_v} error is {str(Exception)}")
				errors += 1
				if skip_bad_lines:
					continue
				else:
					raise

			name = norm_len(obj.get("name"), 255)
			locale = norm_len(obj.get("locale"), 10)
			currency_name = norm_len(obj.get("currency_name"), 20)
			active = _bool_to_tinyint(obj.get("active", False))
			cik = norm_len(obj.get("cik"), 10)
			composite_figi = norm_len(obj.get("composite_figi"), 12)
			share_class_figi = norm_len(obj.get("share_class_figi"), 12)
			last_updated = _parse_dt(obj.get("last_updated_utc"))

			try:
				cursor.execute(
					insert_ext_sql,
					(
						ticker_id,
						name,
						locale,
						currency_name,
						active,
						cik,
						composite_figi,
						share_class_figi,
						last_updated,
					),
				)
				upserted_extended += 1
			except Exception:
				print(f"Error inserting ticker extended info: {ticker_v} error is {str(Exception)}")
				errors += 1
				if skip_bad_lines:
					continue
				else:
					raise

			processed += 1

	return {
		"processed_lines": processed,
		"inserted_symbols": inserted_symbols,
		"updated_symbols": updated_symbols,
		"upserted_extended": upserted_extended,
		"errors": errors,
	}


def main(argv: Optional[list[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Populate ticker tables from JSONL file")
	parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")
	parser.add_argument("--file", default=DEFAULT_INPUT_FILE, help="Input JSONL file path")
	parser.add_argument("--limit", type=int, default=None, help="Process only first N records")
	parser.add_argument("--no-skip-bad", action="store_true", help="Fail on bad lines instead of skipping")

	args = parser.parse_args(argv)

	conn = get_connection(dbinfo_file=args.dbinfo_file)

	try:
		stats = process_file(
			conn,
			input_file=args.file,
			limit=args.limit,
			skip_bad_lines=not args.no_skip_bad,
		)
	finally:
		try:
			conn.close()
		except Exception:
			pass

	print("Populate summary:")
	for k, v in stats.items():
		print(f"  {k}: {v}")

	return 0


if __name__ == "__main__":
	raise SystemExit(main())
