from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from database.db_connect import get_connection


@dataclass
class Ratios:
	open_ratio: Optional[float]
	high_ratio: Optional[float]
	low_ratio: Optional[float]
	close_ratio: Optional[float]
	volume_ratio: Optional[float]
	vwap_ratio: Optional[float]
	transaction_ratio: Optional[float]
	change_in_price_over_day: Optional[float]
	next_day_change_in_price: Optional[float]
	zero_centered_next_day_price_change: Optional[float]


# Round recomputed values to 4 decimal places before comparison/logging
def r4(x: Optional[float]) -> Optional[float]:
	if x is None:
		return None
	try:
		return round(float(x), 4)
	except Exception:
		return None


def parse_args(argv: Optional[list[str]] = None):
	p = argparse.ArgumentParser(
		description=(
			"Verify daily_delta_stock_data by recomputing per-day ratios from raw_stock_data. "
			"For a sampled set of (date_id,ticker_id), it fetches prior, current, and next day rows and compares."
		)
	)
	p.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")
	p.add_argument("--limit", type=int, default=200, help="How many (date_id,ticker_id) pairs to sample (default: 200)")
	p.add_argument("--seed", type=int, default=None, help="Random seed for sampling determinism")
	p.add_argument(
		"--error-log",
		default=str(Path("db_daily_delta_stock_delta_errors.txt")),
		help="Path to append detailed mismatch records (default: db_daily_delta_stock_delta_errors.txt)",
	)
	return p.parse_args(argv)


def _fetch_random_pairs(cur, limit: int) -> list[Tuple[int, int]]:
	cur.execute(
		"""
		SELECT d.date_id, d.ticker_id
		FROM daily_delta_stock_data d
		ORDER BY RAND()
		LIMIT %s
		""",
		(int(limit),),
	)
	rows = cur.fetchall() or []
	cols = cur.column_names
	pairs: list[Tuple[int, int]] = []
	for r in rows:
		rd = dict(zip(cols, r))
		pairs.append((int(rd["date_id"]), int(rd["ticker_id"])) )
	return pairs


def _fetch_raw_row(cur, date_id: int, ticker_id: int) -> Optional[Tuple[float, float, float, float, float, float, int]]:
	cur.execute(
		"""
		SELECT open, high, low, close, volume, vwap, transactions
		FROM raw_stock_data
		WHERE date_id = %s AND ticker_id = %s
		""",
		(int(date_id), int(ticker_id)),
	)
	row = cur.fetchone()
	if not row:
		return None
	# access by column name for robustness
	rowdict = dict(zip(cur.column_names, row))
	return (
		None if rowdict.get("open") is None else float(rowdict.get("open")),
		None if rowdict.get("high") is None else float(rowdict.get("high")),
		None if rowdict.get("low") is None else float(rowdict.get("low")),
		None if rowdict.get("close") is None else float(rowdict.get("close")),
		None if rowdict.get("volume") is None else float(rowdict.get("volume")),
		None if rowdict.get("vwap") is None else float(rowdict.get("vwap")),
		None if rowdict.get("transactions") is None else int(rowdict.get("transactions")),
	)


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
	if a is None or b is None:
		return None
	try:
		if b == 0:
			return None
		return float(a) / float(b)
	except Exception:
		return None


def _compute_ratios(prior, today, nxt) -> Ratios:
	# Each row is (open, high, low, close, volume, vwap, transactions)
	# today/prior for core ratios
	open_ratio = _safe_div(today[0], prior[0]) if today and prior else None
	high_ratio = _safe_div(today[1], prior[1]) if today and prior else None
	low_ratio = _safe_div(today[2], prior[2]) if today and prior else None
	close_ratio = _safe_div(today[3], prior[3]) if today and prior else None
	volume_ratio = _safe_div(today[4], prior[4]) if today and prior else None
	vwap_ratio = _safe_div(today[5], prior[5]) if today and prior else None
	transaction_ratio = _safe_div(float(today[6]) if today and today[6] is not None else None,
								  float(prior[6]) if prior and prior[6] is not None else None) if today and prior else None

	# change_in_price_over_day = close / open (same day)
	change_in_price_over_day = _safe_div(today[3], today[0]) if today else None

	# next_day_change_in_price = next.close / next.open
	next_day_change_in_price = _safe_div(nxt[3], nxt[0]) if nxt else None

	# zero_centered_next_day_price_change = next_day_change_in_price - 1
	zero_centered_next_day_price_change = (
		(next_day_change_in_price - 1.0) if next_day_change_in_price is not None else None
	)

	return Ratios(
		open_ratio,
		high_ratio,
		low_ratio,
		close_ratio,
		volume_ratio,
		vwap_ratio,
		transaction_ratio,
		change_in_price_over_day,
		next_day_change_in_price,
		zero_centered_next_day_price_change,
	)


def _fetch_prior_and_next_date(cur, date_id: int, ticker_id: int) -> Tuple[Optional[int], Optional[int]]:
	"""
	Find the nearest prior and next date_ids that have raw_stock_data for the given ticker_id.
	This skips calendar dates where the ticker has no data.
	"""
	# Prior date_id for this ticker
	cur.execute(
		"""
		SELECT date_id
		FROM raw_stock_data
		WHERE ticker_id = %s AND date_id < %s
		ORDER BY date_id DESC
		LIMIT 1
		""",
		(int(ticker_id), int(date_id)),
	)
	row = cur.fetchone()
	prior_id = int(dict(zip(cur.column_names, row))["date_id"]) if row else None

	# Next date_id for this ticker
	cur.execute(
		"""
		SELECT date_id
		FROM raw_stock_data
		WHERE ticker_id = %s AND date_id > %s
		ORDER BY date_id ASC
		LIMIT 1
		""",
		(int(ticker_id), int(date_id)),
	)
	row = cur.fetchone()
	next_id = int(dict(zip(cur.column_names, row))["date_id"]) if row else None
	return prior_id, next_id


def _fetch_ddsd_row(cur, date_id: int, ticker_id: int) -> Dict[str, Optional[float]]:
	cur.execute(
		"""
		SELECT open_ratio_to_prior, high_ratio_to_prior, low_ratio_to_prior,
			   close_ratio_to_prior, volume_ratio_to_prior, vwap_ratio_to_prior,
			   transactions_ratio_to_prior,
			   change_in_price_over_day, next_day_change_in_price,
			   zero_centered_next_day_price_change,
			   -- Fields below exist in table but are not validated yet in this script
			   `zero_center_extremes_neg1_pos1`,
			   `zero_center_top100_1_other_neg1`,
			   `zero_center_raise1_lower_neg1`,
				`next_day_price_change_to_extremes_0_1`,
				`next_day_raise1_lower0`,
				`next_day_top100_raise1_lower0`

		FROM daily_delta_stock_data
		WHERE date_id = %s AND ticker_id = %s
		""",
		(int(date_id), int(ticker_id)),
	)
	row = cur.fetchone()
	if not row:
		return {}
	# Build dictionary by column name, then select values by name to avoid index-based access
	rowdict = dict(zip(cur.column_names, row))
	keys = [
		"open_ratio_to_prior",
		"high_ratio_to_prior",
		"low_ratio_to_prior",
		"close_ratio_to_prior",
		"volume_ratio_to_prior",
		"vwap_ratio_to_prior",
		"transactions_ratio_to_prior",
		"change_in_price_over_day",
		"next_day_change_in_price",
		"zero_centered_next_day_price_change",
		# not checked yet (placeholders)
		"zero_center_extremes_neg1_pos1",
		"zero_center_top100_1_other_neg1",
		"zero_center_raise1_lower_neg1",
		"next_day_price_change_to_extremes_0_1",
		"next_day_raise1_lower0",
		"next_day_top100_raise1_lower0",
	]
	out: Dict[str, Optional[float]] = {}
	for k in keys:
		v = rowdict.get(k)
		out[k] = None if v is None else float(v)
	return out


def _compare(a: Optional[float], b: Optional[float], tol: float = 1e-3) -> bool:
	if a is None and b is None:
		return True
	if a is None or b is None:
		return False
	return abs(a - b) <= tol


def _append_mismatch_log(log_path: Path, date_id: int, ticker_id: int, name: str, recomputed: Optional[float], db_value: Optional[float]) -> None:
	try:
		log_path.parent.mkdir(parents=True, exist_ok=True)
		with log_path.open("a", encoding="utf-8") as f:
			f.write(
				f"date_id={date_id}, ticker_id={ticker_id}, field={name}, recomputed={recomputed}, db_value={db_value}\n"
			)
	except Exception:
		# Swallow logging errors to avoid interrupting verification
		pass

def get_all_tickersForDate(cur, date_id: int) -> list[int]:
	"""Fetch all tickers for a given date from the database."""
	tickers = []
#	date_id = 16781
	try:
		SQL_STATEMENT = f"SELECT DISTINCT ticker_id FROM daily_delta_stock_data WHERE date_id = {date_id}"
		cur.execute(SQL_STATEMENT)
		tickers = [row[0] for row in cur.fetchall()]
	except Exception as e:
		print(f"Error fetching tickers for date {date_id}: {e}")
	return tickers

def check_given_list(cur,date_id:int,ticker_id: int, log_path:Path,unchecked_fields:dict, name_of_list: str) -> Tuple[int,int] :
	added_all_for_date = False
	total_to_check = 1
	mismatches = 0
	tickers_to_check = [ticker_id]
	progress_counter = 0
	while(tickers_to_check):
		progress_counter += 1
		if progress_counter % 100 == 0:
			print(f"Processed inner {progress_counter} of {len(tickers_to_check)} tickers for date {date_id}...")
		ticker_id = tickers_to_check.pop(0)
		prior_id, next_id = _fetch_prior_and_next_date(cur, date_id, ticker_id)
		today_row = _fetch_raw_row(cur, date_id, ticker_id)
		prior_row = _fetch_raw_row(cur, prior_id, ticker_id) if prior_id is not None else None
		next_row = _fetch_raw_row(cur, next_id, ticker_id) if next_id is not None else None

		if today_row is None or prior_row is None:
			# Cannot validate this sample
			mismatches += 1
		else:
			recomputed = _compute_ratios(prior_row, today_row, next_row)
			ddsd_vals = _fetch_ddsd_row(cur, date_id, ticker_id)
			if not ddsd_vals:
				mismatches += 1
				return mismatches


			comparisons = [
				(r4(recomputed.open_ratio), r4(ddsd_vals.get("open_ratio_to_prior")), "open_ratio_to_prior"),
				(r4(recomputed.high_ratio), r4(ddsd_vals.get("high_ratio_to_prior")), "high_ratio_to_prior"),
				(r4(recomputed.low_ratio), r4(ddsd_vals.get("low_ratio_to_prior")), "low_ratio_to_prior"),
				(r4(recomputed.close_ratio), r4(ddsd_vals.get("close_ratio_to_prior")), "close_ratio_to_prior"),
				(r4(recomputed.volume_ratio), r4(ddsd_vals.get("volume_ratio_to_prior")), "volume_ratio_to_prior"),
				(r4(recomputed.vwap_ratio), r4(ddsd_vals.get("vwap_ratio_to_prior")), "vwap_ratio_to_prior"),
				(r4(recomputed.transaction_ratio), r4(ddsd_vals.get("transactions_ratio_to_prior")),
				 "transactions_ratio_to_prior"),
				(r4(recomputed.change_in_price_over_day), r4(ddsd_vals.get("change_in_price_over_day")),
				 "change_in_price_over_day"),
				(r4(recomputed.next_day_change_in_price), r4(ddsd_vals.get("next_day_change_in_price")),
				 "next_day_change_in_price"),
				(r4(recomputed.zero_centered_next_day_price_change), r4(ddsd_vals.get("zero_centered_next_day_price_change")),
				 "zero_centered_next_day_price_change"),
			]

			ok = True
			for comp, dbv, name in comparisons:
				if not _compare(comp, dbv):
					ok = False
					_append_mismatch_log(log_path, date_id, ticker_id, name, comp, dbv)

			if not ok:
				mismatches += 1

			# Count placeholders to remind they need checks later
			for k in list(unchecked_fields.keys()):
				if ddsd_vals.get(k) is not None:
					unchecked_fields[k] += 1
		if mismatches >0 and not added_all_for_date:
			print(f"Mismatches for date {date_id} and ticker {ticker_id}")
			tickers_to_check.extend(get_all_tickersForDate(cur, date_id))
			total_to_check += len(tickers_to_check)  #we might technically double count the first one, but that doesn't really matter.
			added_all_for_date = True
	if mismatches >0:
		print(f"Mismatches for date {date_id}: {mismatches}/{total_to_check}")
	return mismatches,total_to_check


def main(argv: Optional[list[str]] = None) -> int:
	args = parse_args(argv)
	total = 0

	if args.seed is not None:
		random.seed(args.seed)

	conn = get_connection(dbinfo_file=args.dbinfo_file)
	cur = conn.cursor()

	pairs = _fetch_random_pairs(cur, args.limit)
	if not pairs:
		print("No pairs found to validate.")
		return 1

	mismatches = 0
	unchecked_fields: Dict[str, int] = {
		"zero_center_extremes_neg1_pos1": 0,
		"zero_center_top100_1_other_neg1": 0,
		"zero_center_raise1_lower_neg1": 0,
		"next_day_price_change_to_extremes_0_1": 0,
		"next_day_raise1_lower0" : 0,
		"next_day_top100_raise1_lower0" : 0
	}

	log_path = Path(args.error_log)
	progress_counter = 0
	for (date_id, ticker_id) in pairs:
		progress_counter += 1
		if progress_counter % 100 == 0:
			print(f"Processed {progress_counter} of {len(pairs)} pairs...")
		batch_mismatches,batch_total_to_check = check_given_list(cur,date_id,ticker_id, log_path,unchecked_fields, "daily_delta_stock_data")
		mismatches += batch_mismatches
		total += batch_total_to_check

	total = len(pairs)
	print({
		"sampled_pairs": total,
		"mismatches": mismatches,
		"error_log": str(log_path),
		"unchecked_fields": unchecked_fields,
	})

	try:
		conn.close()
	except Exception:
		pass

	return 0 if mismatches == 0 else 2


if __name__ == "__main__":
	raise SystemExit(main())
