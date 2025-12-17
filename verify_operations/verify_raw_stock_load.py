from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from database.db_connect import get_connection

# Constants
MARKET_SUMMARY_ROOT = Path("data") / "market_summary"
ERROR_LOG_PATH = Path("db_raw_stock_load_errors.txt")


VOLUME_STORAGE_SUFFIX = "_VOLUME_ATTRIBUTE"


def _d4(val: Optional[float | int | str | Decimal]) -> Optional[Decimal]:
	if val is None:
		return None
	try:
		d = Decimal(str(val))
	except Exception:
		return None
	return d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _to_int(val) -> Optional[int]:
	if val is None:
		return None
	try:
		return round(val)
	except Exception:
		return None


def _discover_ndjson_files(root: Path) -> List[Tuple[str, Path]]:
	# Mirrors populate_db_raw_stock_info.discover_files but kept local to avoid import cycles
	items: List[Tuple[str, Path]] = []
	if not root.exists():
		return items
	all_year_directories = []
	for one_year_dir in root.iterdir():
		if one_year_dir.is_dir():
			all_year_directories.append(one_year_dir)
	format_string = "%Y-%m-%d"
	for year_dir in sorted(all_year_directories):
		for f in sorted(year_dir.glob("*.ndjson")):
			stem = Path(f.name).stem
			extension = Path(f.name).suffix
			try :
				datetime.strptime(stem, format_string).date()
				if extension == ".ndjson":
					# Cheap pattern check YYYY-MM-DD.ndjson
					date_str = stem
					items.append((date_str, f))
			except ValueError:
				continue # eat this, we just want to know if it's a valid date string
	return items


def _load_file_as_dict(
	path: Path,
	allowed_tickers: Optional[set[str]] = None,
) -> Dict[str, Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[int]] | int]:
	"""Return mapping ticker -> normalized tuple (o,h,l,c,v,vw,n) from file.

	If ``allowed_tickers`` is provided, only include rows whose ticker is in the
	provided set; other lines are ignored.
	"""
	result: Dict[str, Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[int]]|int] = {}
	with path.open("r", encoding="utf-8") as f:
		for line in f:
			line = line.strip()
			if not line:
				continue
			try:
				obj = json.loads(line)
			except json.JSONDecodeError:
				continue
			ticker = str(obj.get("T") or "").upper()
			if not ticker:
				continue
			if allowed_tickers is not None and ticker not in allowed_tickers:
				# Skip symbols not present in tickers.txt
				continue
			o = _d4(obj.get("o"))
			h = _d4(obj.get("h"))
			l = _d4(obj.get("l"))
			c = _d4(obj.get("c"))
			v = _to_int(obj.get("v"))
			vw = _d4(obj.get("vw"))
			n = _to_int(obj.get("n"))
			result[ticker] = (o, h, l, c, vw, n)
			file_storage_key = ticker+VOLUME_STORAGE_SUFFIX
			result[file_storage_key] = v
	return result


def _get_date_id(cursor, date_str: str) -> Optional[int]:
	cursor.execute(
		"SELECT id FROM transaction_dates WHERE transaction_date = %s",
		(date_str,),
	)
	row = cursor.fetchone()
	return int(row[0]) if row else None


def _fetch_db_for_date(cursor, date_str: str, allowed_tickers: Optional[set[str]] = None) -> Dict[str, Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[int]]|int]:
	sql = (
		"SELECT ts.label, rsd.open, rsd.high, rsd.low, rsd.close, rsd.volume, rsd.vwap, rsd.transactions "
		"FROM raw_stock_data rsd "
		"JOIN transaction_dates td ON td.id = rsd.date_id "
		"JOIN ticker_symbol ts ON ts.id = rsd.ticker_id "
		"WHERE td.transaction_date = %s"
	)
	cursor.execute(sql, (date_str,))
	db_map: Dict[str, Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[int]]|int] = {}
	for row in cursor.fetchall() or []:
		sym = str(row[0]).upper() if row[0] is not None else ""
		if not sym or (allowed_tickers and sym not in allowed_tickers):
			continue
		o = _d4(row[1])
		h = _d4(row[2])
		l = _d4(row[3])
		c = _d4(row[4])
		v = _to_int(row[5])
		vw = _d4(row[6])
		n = _to_int(row[7])
		db_map[sym] = (o, h, l, c, vw, n)
		storage_key = sym+VOLUME_STORAGE_SUFFIX
		db_map[storage_key] = v
	return db_map


def _log_mismatch(date_str: str, ticker: str, file_vals, db_vals, reason: str) -> None:
	ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
	with ERROR_LOG_PATH.open("a", encoding="utf-8") as f:
		f.write(
			f"[{ts}] date={date_str} ticker={ticker} reason={reason}\n"
			f"  file={file_vals}\n  db  ={db_vals}\n"
		)


def _load_allowed_tickers(tickers_file: Path) -> set[str]:
	allowed: set[str] = set()
	if not tickers_file.exists():
		return allowed
	with tickers_file.open("r", encoding="utf-8") as f:
		for line in f:
			s = line.strip()
			if not s or s.startswith("#") or s.startswith("//") or s.startswith(";"):
				continue
			# take the first token per line (allow comments after ticker)
			token = s.split()[0]
			if token:
				allowed.add(token.upper())
	return allowed


def verify_once(cursor, date_str: str, path: Path, allowed_tickers: Optional[set[str]] = None) -> bool:
	"""Verify a single date file against DB. Returns True if all records match.

	Only compares tickers present in ``allowed_tickers`` when provided.
	"""
	file_map = _load_file_as_dict(path, allowed_tickers)

	date_id = _get_date_id(cursor, date_str)
	if date_id is None:
		_log_mismatch(date_str, "*", None, None, "No date_id found in DB; entire day missing")
		return False

	db_map = _fetch_db_for_date(cursor, date_str, allowed_tickers)
	# if allowed_tickers is not None and allowed_tickers:
	# 	# Restrict DB map to the same allowed tickers to avoid false positives
	# 	db_map = {k: v for k, v in db_map.items() if k in allowed_tickers}
	#
	all_ok = True
	keys = set(db_map.keys())|set(file_map.keys())
	for sym in sorted(keys):
		if sym.endswith(VOLUME_STORAGE_SUFFIX):
			f_vol = file_map.get(sym)
			d_vol = db_map.get(sym)

			if f_vol == d_vol:
				continue
			elif not (f_vol and d_vol):
				all_ok = False
				_log_mismatch(date_str, sym, f_vol, d_vol, "Volume mismatch (missing value)")
			elif abs(f_vol - d_vol) > 2 :  # there's a weird rounding error in the file
				all_ok = False
				_log_mismatch(date_str, sym, f_vol, d_vol, "Volume mismatch")
		else :
			f_vals = file_map.get(sym)
			d_vals = db_map.get(sym)
			if f_vals is None and d_vals is not None:
				all_ok = False
				_log_mismatch(date_str, sym, f_vals, d_vals, "Ticker present in DB but missing from file")
				continue
			if f_vals is not None and d_vals is None:
				all_ok = False
				_log_mismatch(date_str, sym, f_vals, d_vals, "Ticker present in file but missing in DB")
				continue
			if f_vals != d_vals:
				all_ok = False
				_log_mismatch(date_str, sym, f_vals, d_vals, "Field mismatch")
	return all_ok


def parse_args(argv: Optional[List[str]] = None):
	p = argparse.ArgumentParser(description="Verify raw_stock_data rows match NDJSON market summary files.")
	p.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")
	# All DB parameters are provided via the dbinfo file only
	p.add_argument("--root", default=str(MARKET_SUMMARY_ROOT), help="Root folder for market summary files (default: data/market_summary)")
	p.add_argument("--iterations", type=int, default=0, help="How many random date checks to perform (0 = run until interrupted)")
	return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
	args = parse_args(argv)

	current_directory = Path(os.getcwd())
	root = Path(args.root)
	while not os.path.exists(os.path.join(current_directory, root)):
		current_directory = current_directory.parent
	os.chdir(current_directory)
	root = Path(os.path.join(current_directory, root))
	files = _discover_ndjson_files(root)
	if not files:
		print(f"No NDJSON files found under {root}")
		return 2

	# Load allowed tickers from data/tickers.txt relative to project root
	tickers_path = Path("data") / "tickers.txt"
	allowed_tickers = _load_allowed_tickers(tickers_path)
	if not allowed_tickers:
		print(f"Warning: No tickers loaded from {tickers_path}. All file rows will be considered.")

	conn = get_connection(dbinfo_file=args.dbinfo_file)
	cursor = conn.cursor()

	checks = 0
	correct = 0

	target = args.iterations if args.iterations and args.iterations > 0 else None

	try:
		while True:
			date_str, path = random.choice(files)
			ok = verify_once(cursor, date_str, path, allowed_tickers=allowed_tickers if allowed_tickers else None)
			checks += 1
			if ok:
				correct += 1

			if checks % 10 == 0:
				print(f"Verification progress: checked={checks}, correct_matches={correct}")

			if target is not None and checks >= target:
				break
	except KeyboardInterrupt:
		print("Interrupted by user.")
	finally:
		try:
			cursor.close()
		except Exception:
			pass
		try:
			conn.close()
		except Exception:
			pass

	# Final report
	print(f"Verification complete. Total checked={checks}, correct_matches={correct}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
