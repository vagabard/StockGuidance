"""Identify tickers that have missing trading dates between their first and last appearances.

This script connects to the MySQL database using the standard ``dbinfo.id`` file and scans
``raw_stock_data`` to determine, for each ticker, the first and last ``date_id`` where it appears.
For any ticker whose coverage between those two endpoints is not continuous, the script reports
the missing ``date_id`` values.

Example usage::

	python identify_tickers_with_missing_dates.py --dbinfo-file dbinfo.id --limit-missing 20
"""

from __future__ import annotations

import argparse
from typing import Dict, List, Optional, Tuple

from database.db_connect import get_connection


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:

	p = argparse.ArgumentParser(
		description=(
			"Find tickers whose raw_stock_data entries have gaps between the first and last date_id."
		)
	)

	p.add_argument(
		"--dbinfo-file",
		default="dbinfo.id",
		help="Path to DB info file (default: dbinfo.id)",
	)

	p.add_argument(
		"--limit-missing",
		type=int,
		default=25,
		help=(
			"Maximum number of missing date_ids to display per ticker (0 for unlimited)."
		),
	)

	return p.parse_args(argv)


def _load_transaction_dates(cur) -> List[int]:

	cur.execute("SELECT id FROM transaction_dates ORDER BY id ASC")
	rows = cur.fetchall()
	return [int(row[0]) for row in rows]


def _load_ticker_ranges(cur) -> List[Tuple[int, int, int, int]]:

	cur.execute(
		"""
		SELECT
			rsd.ticker_id,
			MIN(rsd.date_id) AS first_date_id,
			MAX(rsd.date_id) AS last_date_id,
			COUNT(DISTINCT rsd.date_id) AS date_count
		FROM raw_stock_data rsd
		JOIN ticker_symbol ts ON ts.id = rsd.ticker_id
		GROUP BY rsd.ticker_id
		ORDER BY rsd.ticker_id
		"""
	)
	rows = cur.fetchall()
	return [
		(
			int(row[0]),
			int(row[1]),
			int(row[2]),
			int(row[3]),
		)
		for row in rows
	]


def _load_ticker_date_ids(cur, ticker_id: int) -> List[int]:

	cur.execute(
		"SELECT DISTINCT date_id FROM raw_stock_data WHERE ticker_id = %s ORDER BY date_id ASC",
		(int(ticker_id),),
	)
	rows = cur.fetchall()
	return [int(row[0]) for row in rows]


def _mark_missing_dates_flag(cur, ticker_id: int) -> None:

	cur.execute(
		"UPDATE ticker_symbol SET missing_dates = TRUE WHERE id = %s",
		(int(ticker_id),),
	)


def _mark_no_missing_dates_flag(cur, ticker_id: int) -> None:

	cur.execute(
		"UPDATE ticker_symbol SET missing_dates = FALSE WHERE id = %s",
		(int(ticker_id),),
	)


def count_missing_dates_for_ticker(
	cur,
	ticker_id: int,
	start_date_id: int,
	end_date_id: int,
	*,
	transaction_dates: Optional[List[int]] = None,
	date_index: Optional[Dict[int, int]] = None,
) -> Tuple[int,int,int]:
	"""
	Count how many transaction_dates between start_date_id and end_date_id (inclusive)
	are absent from raw_stock_data for the given ticker_id.

	This helper accepts preloaded transaction_dates and their index map to avoid
	repeated queries when called in a loop; if not provided, they are loaded on demand.
	"""

	if start_date_id is None or end_date_id is None:
		return 0,0,0

	# Use a single SQL query to count how many distinct dates between the start and end
	# bounds are absent for this ticker. This mirrors the requested pattern of comparing
	# all dates in the range against the ticker's own dates via a derived table.
	cur.execute(
		"""
		SELECT COUNT(*) AS missing_count,min(rsd_dates.date_id) AS first_missing_date_id,max(rsd_dates.date_id) AS last_missing_date_id
		FROM (
			SELECT DISTINCT date_id
			FROM raw_stock_data
			WHERE date_id BETWEEN %s AND %s
		) AS rsd_dates
		LEFT JOIN (
			SELECT DISTINCT date_id FROM raw_stock_data WHERE ticker_id = %s
		) AS ticker_dates ON rsd_dates.date_id = ticker_dates.date_id
		WHERE ticker_dates.date_id IS NULL
		""",
		(int(start_date_id), int(end_date_id), int(ticker_id)),
	)

	row = cur.fetchone()
	rowcount = int(row[0]) if row and row[0] is not None else 0
	min_date_id = int(row[1]) if row and row[1] is not None else 0
	max_date_id = int(row[2]) if row and row[2] is not None else 0
	return rowcount, min_date_id, max_date_id


def _slice_dates_by_bounds(all_dates: List[int], start_id: int, end_id: int, index_map: Dict[int, int]) -> List[int]:

	if start_id not in index_map or end_id not in index_map:
		return []
	start_idx = index_map[start_id]
	end_idx = index_map[end_id]
	if end_idx < start_idx:
		return []
	return all_dates[start_idx : end_idx + 1]


def main(argv: Optional[list[str]] = None) -> None:

	args = parse_args(argv)

	conn = get_connection(dbinfo_file=args.dbinfo_file)
	try:
		cur = conn.cursor()

		transaction_dates = _load_transaction_dates(cur)
		if not transaction_dates:
			print("No transaction_dates found; nothing to check.")
			return

		date_index: Dict[int, int] = {date_id: idx for idx, date_id in enumerate(transaction_dates)}

		ticker_ranges = _load_ticker_ranges(cur)

		tickers_with_gaps: List[Tuple[int, int, int, List[int]]] = []

		for ticker_id, first_date_id, last_date_id, date_count in ticker_ranges:
			if first_date_id is None or last_date_id is None:
				continue

			expected_span = _slice_dates_by_bounds(transaction_dates, first_date_id, last_date_id, date_index)
			if not expected_span:
				print(
					f"Skipping ticker ({ticker_id}) because first/last date_id values are not present in transaction_dates."
				)
				continue

			missing_count, first_missing_date_id, last_missing_date_id = count_missing_dates_for_ticker(
				cur,
				ticker_id,
				first_date_id,
				last_date_id,
				transaction_dates=transaction_dates,
				date_index=date_index,
			)
			if missing_count < 5:
				_mark_no_missing_dates_flag(cur, ticker_id)
				continue

			print(f"For ticker ({ticker_id}) Missing count: {missing_count} first_missing_date_id: {first_missing_date_id} last_missing_date_id: {last_missing_date_id}")
			ticker_date_ids = _load_ticker_date_ids(cur, ticker_id)
			ticker_date_set = set(ticker_date_ids)

			missing_ids = [date_id for date_id in expected_span if date_id not in ticker_date_set]

			if missing_ids:
				tickers_with_gaps.append(
					(ticker_id,  first_date_id, last_date_id, missing_ids)
				)
				_mark_missing_dates_flag(cur, ticker_id)

		if not tickers_with_gaps:
			print("No tickers with missing dates found between their first and last date_id.")
			return

		print(
			f"Found {len(tickers_with_gaps)} ticker(s) with missing dates between their first and last entries."
		)

		for ticker_id, first_date_id, last_date_id, missing_ids in tickers_with_gaps:
			if 0 < args.limit_missing < len(missing_ids):
				display_ids = missing_ids[: args.limit_missing]
				remaining = len(missing_ids) - args.limit_missing
				missing_repr = f"{display_ids} ... (+{remaining} more)"
			else:
				missing_repr = str(missing_ids)

			print(
				f" (id {ticker_id}): first_date_id={first_date_id}, last_date_id={last_date_id}, "
				f"missing_count={len(missing_ids)}, missing_date_ids={missing_repr}"
			)

	finally:
		try:
			conn.close()
		except Exception:
			pass


if __name__ == "__main__":

	main()
