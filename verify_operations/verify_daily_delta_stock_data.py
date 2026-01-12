from __future__ import annotations

import argparse
import random
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple, List

from database.db_connect import get_connection


batch_size = 1000

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
		help="Path to append detailed mismatch records (default: db_daily_delta_stock_delta_errors.txt.1)",
	)
	return p.parse_args(argv)


def _fetch_random_pairs(cur, limit: int) -> list[Tuple[int, int]]:
	if limit == 0:
		# If limit is 0, fetch all date_ids and all ticker_ids
		cur.execute("SELECT date_id, ticker_id FROM daily_delta_stock_data")
		return [(int(row[0]), int(row[1])) for row in cur.fetchall()]

	# 1. Get a list of valid dates from transaction_dates
	cur.execute("SELECT id FROM transaction_dates")
	date_ids = [row[0] for row in cur.fetchall()]
	if not date_ids:
		return []

	# 2. Choose the dates with replacement up to the number of the limit
	sampled_date_ids = random.choices(date_ids, k=limit)

	pairs: list[Tuple[int, int]] = []
	# 3. For each date, pick a random ticker_id that exists for it
	for date_id in sampled_date_ids:
		cur.execute(
			"""
			SELECT ticker_id
			FROM daily_delta_stock_data
			WHERE date_id = %s
			ORDER BY RAND()
			LIMIT 1
			""",
			(int(date_id),),
		)
		row = cur.fetchone()
		if row:
			pairs.append((int(date_id), int(row[0])))

	return pairs


def _to_float(val: Optional[float]) -> Optional[float]:
	if val is None:
		return None
	try:
		return float(val)
	except Exception:
		return None


def _extract_raw_fields(rowdict: Dict[str, Optional[float]]):
	return {
		"open": _to_float(rowdict.get("open")),
		"high": _to_float(rowdict.get("high")),
		"low": _to_float(rowdict.get("low")),
		"close": _to_float(rowdict.get("close")),
		"volume": _to_float(rowdict.get("volume")),
		"vwap": _to_float(rowdict.get("vwap")),
		"transactions": _to_float(rowdict.get("transactions")),
	}


def _extract_ddsd_fields(rowdict: Dict[str, Optional[float]]):
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
		"next_day_price_change_to_extremes_0_1",
		"zero_center_extremes_neg1_pos1",
		"next_day_raise1_lower0",
		"zero_center_raise1_lower_neg1",
		"next_day_top100_raise1_lower0",
		"zero_center_top100_1_other_neg1",
	]
	out: Dict[str, Optional[float]] = {}
	for k in keys:
		v = rowdict.get(k)
		out[k] = None if v is None else float(v)
	return out


def _fetch_date_ticker_info(cur, pairs: List[Tuple[int, int]]) -> Dict[Tuple[int, int], Dict[str, dict]]:
	if not pairs:
		return {}

	result: Dict[Tuple[int, int], Dict[str, dict]] = {}

	for start in range(0, len(pairs), batch_size):
		batch = pairs[start:start + batch_size]
		placeholders = ", ".join(["(%s, %s)"] * len(batch))
		flat_params: list[int] = []
		for date_id, ticker_id in batch:
			flat_params.extend([int(date_id), int(ticker_id)])

		fetch_date_ticker_sql = f"""
		SELECT rsd.date_id,
		       rsd.ticker_id,
		       rsd.open,
		       rsd.high,
		       rsd.low,
		       rsd.close,
		       rsd.volume,
		       rsd.vwap,
		       rsd.transactions,
		       ddsd.open_ratio_to_prior,
		       ddsd.high_ratio_to_prior,
		       ddsd.low_ratio_to_prior,
		       ddsd.close_ratio_to_prior,
		       ddsd.volume_ratio_to_prior,
		       ddsd.vwap_ratio_to_prior,
		       ddsd.transactions_ratio_to_prior,
		       ddsd.change_in_price_over_day,
		       ddsd.next_day_change_in_price,
		       ddsd.zero_centered_next_day_price_change,
		       ddsd.next_day_price_change_to_extremes_0_1,
		       ddsd.zero_center_extremes_neg1_pos1,
		       ddsd.next_day_raise1_lower0,
		       ddsd.zero_center_raise1_lower_neg1,
		       ddsd.next_day_top100_raise1_lower0,
		       ddsd.zero_center_top100_1_other_neg1
		FROM raw_stock_data rsd
		LEFT JOIN daily_delta_stock_data ddsd
		  ON ddsd.date_id = rsd.date_id AND ddsd.ticker_id = rsd.ticker_id
		WHERE (rsd.date_id, rsd.ticker_id) IN ({placeholders})
		"""

		cur.execute(fetch_date_ticker_sql, tuple(flat_params))
		for row in cur.fetchall():
			rowdict = dict(zip(cur.column_names, row))
			key = (int(rowdict["date_id"]), int(rowdict["ticker_id"]))
			result[key] = {
				"raw": _extract_raw_fields(rowdict),
				"ddsd": _extract_ddsd_fields(rowdict),
			}

	return result


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
	# Each row is a mapping with keys: open, high, low, close, volume, vwap, transactions
	def gv(row, key):
		return None if row is None else row.get(key)

	open_ratio = _safe_div(gv(today, "open"), gv(prior, "open")) if today and prior else None
	high_ratio = _safe_div(gv(today, "high"), gv(prior, "high")) if today and prior else None
	low_ratio = _safe_div(gv(today, "low"), gv(prior, "low")) if today and prior else None
	close_ratio = _safe_div(gv(today, "close"), gv(prior, "close")) if today and prior else None
	volume_ratio = _safe_div(gv(today, "volume"), gv(prior, "volume")) if today and prior else None
	vwap_ratio = _safe_div(gv(today, "vwap"), gv(prior, "vwap")) if today and prior else None
	transaction_ratio = _safe_div(
		float(gv(today, "transactions")) if gv(today, "transactions") is not None else None,
		float(gv(prior, "transactions")) if gv(prior, "transactions") is not None else None,
	) if today and prior else None

	change_in_price_over_day = _safe_div(gv(today, "close"), gv(today, "open")) if today else None
	next_day_change_in_price = _safe_div(gv(nxt, "close"), gv(nxt, "open")) if nxt else None
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


def _compute_next_day_price_change_to_extremes(ndcip: Optional[float]) -> Optional[float]:
	if ndcip is None:
		return None
	try:
		denom = 2 * (ndcip + 1)
		if denom == 0:
			return None
		if ndcip >= 1:
			return 0.5 + ndcip / denom
		return ndcip / denom
	except Exception:
		return None


def verify_one_ticker_date(
		date_id: int,
		ticker_id: int,
		today_row: Optional[dict],
		prior_row: Optional[dict],
		next_row: Optional[dict],
		ddsd_vals: Dict[str, Optional[float]],
		log_path: Path,
		unchecked_fields: Optional[Dict[str, int]] = None,
	) -> List[Tuple[str, Optional[float], Optional[float]]]:
	mismatches: List[Tuple[str, Optional[float], Optional[float]]] = []

	if today_row is None or prior_row is None:
		mismatches.append(("missing_raw_prior_or_today", None, None))
	else:
		if not ddsd_vals or all(v is None for v in ddsd_vals.values()):
			mismatches.append(("missing_ddsd_values", None, None))
		else:
			recomputed = _compute_ratios(prior_row, today_row, next_row)
			ndcip = recomputed.next_day_change_in_price
			extremes_0_1 = _compute_next_day_price_change_to_extremes(ndcip)
			raise_lower = None if ndcip is None else (1.0 if ndcip >= 1 else 0.0)
			zero_center_extremes = None if extremes_0_1 is None else extremes_0_1 * 2 - 1
			zero_center_raise_lower = None if raise_lower is None else raise_lower * 2 - 1

			comparisons = [
				(r4(recomputed.open_ratio), r4(ddsd_vals.get("open_ratio_to_prior")), "open_ratio_to_prior"),
				(r4(recomputed.high_ratio), r4(ddsd_vals.get("high_ratio_to_prior")), "high_ratio_to_prior"),
				(r4(recomputed.low_ratio), r4(ddsd_vals.get("low_ratio_to_prior")), "low_ratio_to_prior"),
				(r4(recomputed.close_ratio), r4(ddsd_vals.get("close_ratio_to_prior")), "close_ratio_to_prior"),
				(r4(recomputed.volume_ratio), r4(ddsd_vals.get("volume_ratio_to_prior")), "volume_ratio_to_prior"),
				(r4(recomputed.vwap_ratio), r4(ddsd_vals.get("vwap_ratio_to_prior")), "vwap_ratio_to_prior"),
				(r4(recomputed.transaction_ratio), r4(ddsd_vals.get("transactions_ratio_to_prior")), "transactions_ratio_to_prior"),
				(r4(recomputed.change_in_price_over_day), r4(ddsd_vals.get("change_in_price_over_day")), "change_in_price_over_day"),
				(r4(ndcip), r4(ddsd_vals.get("next_day_change_in_price")), "next_day_change_in_price"),
				(r4(recomputed.zero_centered_next_day_price_change), r4(ddsd_vals.get("zero_centered_next_day_price_change")),
				 "zero_centered_next_day_price_change"),
				(r4(extremes_0_1), r4(ddsd_vals.get("next_day_price_change_to_extremes_0_1")), "next_day_price_change_to_extremes_0_1"),
				(r4(zero_center_extremes), r4(ddsd_vals.get("zero_center_extremes_neg1_pos1")), "zero_center_extremes_neg1_pos1"),
				(r4(raise_lower), r4(ddsd_vals.get("next_day_raise1_lower0")), "next_day_raise1_lower0"),
				(r4(zero_center_raise_lower), r4(ddsd_vals.get("zero_center_raise1_lower_neg1")), "zero_center_raise1_lower_neg1"),
			]

			for recomputed_val, db_val, name in comparisons:
				if not _compare(recomputed_val, db_val):
					mismatches.append((name, recomputed_val, db_val))

			if unchecked_fields is not None:
				for k in list(unchecked_fields.keys()):
					if ddsd_vals.get(k) is not None:
						unchecked_fields[k] += 1

	if mismatches:
		field_names = [name for name, _, _ in mismatches]
		recomp_vals = [float("nan") if recomputed_val is None else recomputed_val for _, recomputed_val, _ in mismatches]
		db_vals = [float("nan") if db_val is None else db_val for _, _, db_val in mismatches]
		_append_mismatch_log(log_path, date_id, ticker_id, field_names, recomp_vals, db_vals)

	return mismatches


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


def _compare(a: Optional[float], b: Optional[float], tol: float = 1e-3) -> bool:
	if a is None and b is None:
		return True
	if a is None or b is None:
		return False
	return abs(a - b) <= tol


def _append_mismatch_log(log_path: Path, date_id: int, ticker_id: int, name: List[str], recomputed: List[float], db_value: List[float]) -> None:
	try:
		log_path.parent.mkdir(parents=True, exist_ok=True)
		with log_path.open("a", encoding="utf-8") as f:
			f.write(
				f"date_id={date_id}, ticker_id={ticker_id}, ")
			for namer, recomp, dbval in zip(name, recomputed, db_value):
				f.write(f"{namer}={recomp:.3f}, {dbval:.3f};")

	except Exception as e :
		traceback.print_exception(e)
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

def check_given_list(cur, date_ticker_pairs:List[Tuple[int,int]], log_path:Path,unchecked_fields:dict, name_of_list: str) -> Tuple[int,int] :
	added_all_for_date = False
	total_to_check = 1
	mismatches = 0
	progress_counter = 0
	while len(date_ticker_pairs) >0 :
		batch = date_ticker_pairs[:batch_size]
		date_ticker_pairs = date_ticker_pairs[batch_size:]

		batch_requirements: list[Tuple[int, int, Optional[int], Optional[int]]] = []
		pairs_to_fetch: set[Tuple[int, int]] = set()
		for did,tid in batch:
			progress_counter += 1
			if progress_counter % 1000 == 0:
				print(f"Processed inner {progress_counter} of {progress_counter + len(date_ticker_pairs)} tickers for date {did}...")

			prior_id, next_id = _fetch_prior_and_next_date(cur, did, tid)
			batch_requirements.append((did,tid, prior_id, next_id))
			pairs_to_fetch.add((did, tid))
			if prior_id is not None:
				prior_candidate = (prior_id, tid)
				if prior_candidate not in pairs_to_fetch:
					pairs_to_fetch.add(prior_candidate)
			if next_id is not None:
				next_candidate = (next_id, tid)
				if next_candidate not in pairs_to_fetch:
					pairs_to_fetch.add(next_candidate)

		pair_info = _fetch_date_ticker_info(cur, list(pairs_to_fetch))

		for date_id,tid, prior_id, next_id in batch_requirements:
			today_info = pair_info.get((date_id, tid))
			today_row = today_info.get("raw") if today_info else None
			prior_row = pair_info.get((prior_id, tid), {}).get("raw") if prior_id is not None else None
			next_row = pair_info.get((next_id, tid), {}).get("raw") if next_id is not None else None
			ddsd_vals = today_info.get("ddsd") if today_info else {}

			pair_mismatches = verify_one_ticker_date(
				date_id,
				tid,
				today_row,
				prior_row,
				next_row,
				ddsd_vals,
				log_path,
				unchecked_fields,
			)
			if pair_mismatches:
				mismatches += 1

		# if mismatches > 0 and not added_all_for_date:
		# 	print(f"Mismatches for date {date_id} and ticker {tid}")
		# 	tickers_to_check.extend(get_all_tickersForDate(cur, date_id))
		# 	total_to_check += len(tickers_to_check)
		# 	added_all_for_date = True

	# if mismatches > 0:
	# 	print(f"Mismatches for date {date_id}: {mismatches}/{total_to_check}")
	return mismatches, total_to_check


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
	# progress_counter = 0
	#
	# for (date_id, ticker_id) in pairs:
	# 	progress_counter += 1
	# 	if progress_counter % 100 == 0:
	# 		print(f"Processed {progress_counter} of {len(pairs)} pairs...")
	# 	batch_mismatches,batch_total_to_check = check_given_list(cur,date_id,ticker_id, log_path,unchecked_fields, "daily_delta_stock_data")
	# 	mismatches += batch_mismatches
	# 	total += batch_total_to_check
	#
	mismatches,total = check_given_list(cur,pairs, log_path,unchecked_fields, "daily_delta_stock_data")

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
