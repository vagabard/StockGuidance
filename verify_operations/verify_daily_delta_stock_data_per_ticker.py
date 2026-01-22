from __future__ import annotations

import argparse
import math
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from database.db_connect import get_connection
from verify_operations.verify_daily_delta_stock_data import verify_one_ticker_date



def parse_args(argv: Optional[List[str]] = None):
	parser = argparse.ArgumentParser(
		description=(
			"Verify daily_delta_stock_data for a single ticker by recomputing ratios "
			"from ordered raw_stock_data rows and comparing against stored values."
		)
	)
	parser.add_argument(
		"ticker_id",
		type=int,
		nargs="?",
		help="Ticker ID to verify (omit to verify all tickers)",
	)
	parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file")
	parser.add_argument(
		"--error-log",
		default=str(Path("daily_delta_stock_data_errors_per_ticker.txt")),
		help="Path to append detailed mismatch records (default: db_daily_delta_stock_delta_errors_per_ticker.txt)",
	)
	parser.add_argument(
		"--update",
		action="store_true",
		help="When set, mismatched daily_delta_stock_data fields are updated to the recomputed values",
	)
	parser.add_argument("--check-volume-normalized", action="store_true", help="Check volume_normalized field")
	parser.add_argument("--check-transaction-normalized", action="store_true", help="Check transactions_normalized field")
	return parser.parse_args(argv)


def _to_float(val: Optional[float]) -> Optional[float]:
	if val is None:
		return None
	try:
		return float(val)
	except Exception:
		return None


def _fetch_raw_for_ticker(cur, ticker_id: int) -> List[Tuple[int, Dict[str, Optional[float]]]]:
	cur.execute(
		"""
		SELECT date_id, open, high, low, close, volume, vwap, transactions
		FROM raw_stock_data
		WHERE ticker_id = %s
		ORDER BY date_id ASC
		""",
		(int(ticker_id),),
	)
	rows: List[Tuple[int, Dict[str, Optional[float]]]] = []
	for row in cur.fetchall():
		rowdict = dict(zip(cur.column_names, row))
		rows.append(
			(
				int(rowdict["date_id"]),
				{
					"open": _to_float(rowdict.get("open")),
					"high": _to_float(rowdict.get("high")),
					"low": _to_float(rowdict.get("low")),
					"close": _to_float(rowdict.get("close")),
					"volume": _to_float(rowdict.get("volume")),
					"vwap": _to_float(rowdict.get("vwap")),
					"transactions": _to_float(rowdict.get("transactions")),
				},
			),
		)
	return rows


def _fetch_ddsd_for_ticker(cur, ticker_id: int) -> Dict[int, Dict[str, Optional[float]]]:
	cur.execute(
		"""
		SELECT
			date_id,
			open_ratio_to_prior,
			high_ratio_to_prior,
			low_ratio_to_prior,
			close_ratio_to_prior,
			volume_ratio_to_prior,
			vwap_ratio_to_prior,
			transactions_ratio_to_prior,
			change_in_price_over_day,
			next_day_change_in_price,
			zero_centered_next_day_price_change,
			next_day_price_change_to_extremes_0_1,
			zero_center_extremes_neg1_pos1,
			next_day_raise1_lower0,
			zero_center_raise1_lower_neg1,
			next_day_top100_raise1_lower0,
			zero_center_top100_1_other_neg1
		FROM daily_delta_stock_data
		WHERE ticker_id = %s
		""",
		(int(ticker_id),),
	)

	result: Dict[int, Dict[str, Optional[float]]] = {}
	for row in cur.fetchall():
		rowdict = dict(zip(cur.column_names, row))
		date_id = int(rowdict["date_id"])
		result[date_id] = {k: _to_float(v) for k, v in rowdict.items() if k != "date_id"}
	return result


def _fetch_all_tickers(cur) -> List[int]:
	cur.execute("SELECT DISTINCT ticker_id FROM raw_stock_data ORDER BY ticker_id ASC")
	return [int(row[0]) for row in cur.fetchall()]


def _format_value(val: Optional[float]) -> str:
	try:
		if isinstance(val, float) and math.isnan(val):
			return "nan"
	except Exception:
		pass
	if val is None:
		return "None"
	return str(val)


def _append_summary_log(summary_log_path: Path, date_id: int, mismatches: List[Tuple[str, Optional[float], Optional[float]]]) -> None:
	try:
		summary_log_path.parent.mkdir(parents=True, exist_ok=True)
		reasons: List[str] = []
		for field, recomputed_val, db_val in mismatches:
			reasons.append(
				f"{field}: computed={_format_value(recomputed_val)}, db={_format_value(db_val)}"
			)
		line = ", ".join([str(date_id)] + reasons)
		with summary_log_path.open("a", encoding="utf-8") as f:
			f.write(line + "\n")
	except Exception as e:
		traceback.print_exception(e)


def _apply_mismatch_updates(cur, ticker_id: int, date_id: int, mismatches: List[Tuple[str, Optional[float], Optional[float]]]) -> None:
	update_fields: List[str] = []
	params: List[Optional[float]] = []
	for field, recomputed_val, _ in mismatches:
		# Skip non-field mismatch markers
		if field.startswith("missing_"):
			continue
		update_fields.append(f"{field} = %s")
		params.append(recomputed_val)

	if not update_fields:
		return

	params.extend([ticker_id, date_id])
	try:
		cur.execute(
			f"""
			UPDATE daily_delta_stock_data
			SET {', '.join(update_fields)}
			WHERE ticker_id = %s AND date_id = %s
			""",
			tuple(params),
		)
	except Exception as e:
		print(f"Error applying updates for ticker {ticker_id}, date {date_id}: {e}")
		traceback.print_exception(e)


def _verify_ticker(cur, ticker_id: int, log_path: Path, summary_log_path: Path, update: bool, check_volume_normalized: bool = False, check_transaction_normalized: bool = False) -> Tuple[int, int]:
	raw_rows = _fetch_raw_for_ticker(cur, ticker_id)
	ddsd_map = _fetch_ddsd_for_ticker(cur, ticker_id)

	if not raw_rows:
		print(f"No raw_stock_data found for ticker {ticker_id}.")
		return 0, 0

	mismatches = 0
	checked = 0

	for idx, (date_id, today_row) in enumerate(raw_rows):
		prior_row = raw_rows[idx - 1][1] if idx > 0 else None
		next_row = raw_rows[idx + 1][1] if idx + 1 < len(raw_rows) else None
		ddsd_vals = ddsd_map.get(date_id, {})

		mismatch_list = verify_one_ticker_date(
			date_id,
			ticker_id,
			today_row,
			prior_row,
			next_row,
			ddsd_vals,
			log_path,
			check_volume_normalized=check_volume_normalized,
			check_transaction_normalized=check_transaction_normalized,
		)

		if mismatch_list:
			mismatches += 1
			_append_summary_log(summary_log_path, date_id, mismatch_list)
			if update:
				_apply_mismatch_updates(cur, ticker_id, date_id, mismatch_list)

		checked += 1

	return mismatches, checked


def main(argv: Optional[List[str]] = None) -> int:
	args = parse_args(argv)
	log_path = Path("daily_delta_stock_data_errors/"+args.error_log)

	conn = get_connection(dbinfo_file=args.dbinfo_file)
	try:
		cur = conn.cursor()
		tickers: List[int]
		if args.ticker_id is None:
			tickers = _fetch_all_tickers(cur)
			if not tickers:
				print("No tickers found to verify.")
				return 1
		else:
			tickers = [args.ticker_id]

		total_mismatches = 0
		total_checked = 0
		per_ticker_results: List[Dict[str, object]] = []

		for tid in tickers:
			summary_log_path = log_path.with_name(f"daily_delta_stock_data_ticker_verify_{tid}.txt")
			print(f"Verifying ticker {tid}... with log path {summary_log_path.absolute()}")
			ticker_mismatches, ticker_checked = _verify_ticker(
				cur,
				tid,
				log_path,
				summary_log_path,
				args.update,
				check_volume_normalized=args.check_volume_normalized,
				check_transaction_normalized=args.check_transaction_normalized
			)
			total_mismatches += ticker_mismatches
			total_checked += ticker_checked
			per_ticker_results.append({"ticker_id": tid, "checked_rows": ticker_checked, "mismatches": ticker_mismatches, "summary_log": str(summary_log_path)})
			if args.update:
				conn.commit()

		print({
			"tickers_processed": len(tickers),
			"total_checked_rows": total_checked,
			"total_mismatches": total_mismatches,
			"per_ticker": per_ticker_results,
			"error_log": str(log_path),
		})
		return 0 if total_mismatches == 0 else 2
	finally:
		try:
			conn.close()
		except Exception:
			pass


if __name__ == "__main__":
	raise SystemExit(main())
