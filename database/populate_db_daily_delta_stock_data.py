"""
Populate daily_delta_stock_data by calling the stored procedure for each transaction date.
The procedure is expected to take: current_date_id, prior_date_id, next_date_id.
"""

import argparse
import traceback
from typing import List, Optional

from database.db_connect import get_connection


def get_ordered_date_ids(cursor) -> List[int]:
	"""Get ordered list of transaction date IDs from old to new."""
	cursor.execute("SELECT id FROM transaction_dates ORDER BY transaction_date ASC")
	return [row[0] for row in cursor.fetchall()]


def populate_initial_deltas(cursor, curr_id: int, prior_id: int, ticker_id: Optional[int] = None):
	"""Populate initial delta values for a given date based on prior date."""
	where_clause = "WHERE curr.date_id = %s AND ddsd.ticker_id IS NULL"
	params = [prior_id, curr_id]
	if ticker_id is not None:
		where_clause += " AND curr.ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    INSERT INTO daily_delta_stock_data (
        ticker_id, date_id, open_ratio_to_prior, high_ratio_to_prior, low_ratio_to_prior,
        close_ratio_to_prior, volume_ratio_to_prior, vwap_ratio_to_prior,
        transactions_ratio_to_prior, change_in_price_over_day
    )
    SELECT
        curr.ticker_id, curr.date_id,
        curr.open / NULLIF(prior.open, 0), curr.high / NULLIF(prior.high, 0),
        curr.low / NULLIF(prior.low, 0), curr.close / NULLIF(prior.close, 0),
        curr.volume / NULLIF(prior.volume, 0), curr.vwap / NULLIF(prior.vwap, 0),
        curr.transactions / NULLIF(prior.transactions, 0),
        curr.close / NULLIF(curr.open, 0)
    FROM raw_stock_data curr
    JOIN raw_stock_data prior ON curr.ticker_id = prior.ticker_id AND prior.date_id = %s
    LEFT JOIN daily_delta_stock_data ddsd ON curr.ticker_id = ddsd.ticker_id AND curr.date_id = ddsd.date_id
    {where_clause};
"""
	cursor.execute(sql, tuple(params))


def update_next_day_change(cursor, curr_id: int, next_id: int, ticker_id: Optional[int] = None):
	"""Update next day change in price for a given date based on next date."""
	where_clause = "WHERE d.date_id = %s AND d.next_day_change_in_price IS NULL"
	params = [next_id, curr_id]
	if ticker_id is not None:
		where_clause += " AND d.ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    UPDATE daily_delta_stock_data d
    JOIN daily_delta_stock_data next_day ON d.ticker_id = next_day.ticker_id AND next_day.date_id = %s
    SET d.next_day_change_in_price = next_day.change_in_price_over_day
    {where_clause};
"""
	output = cursor.execute(sql, tuple(params))


def update_zero_centered_next_day_price_change(cursor, curr_id: int, ticker_id: Optional[int] = None):
	"""Update zero centered next day price change."""
	where_clause = "WHERE date_id = %s AND next_day_change_in_price IS NOT NULL AND zero_centered_next_day_price_change IS NULL"
	params = [curr_id]
	if ticker_id is not None:
		where_clause += " AND ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    UPDATE daily_delta_stock_data
    SET zero_centered_next_day_price_change = next_day_change_in_price - 1
    {where_clause};
"""
	cursor.execute(sql, tuple(params))


def update_next_day_price_change_to_extremes_0_1(cursor, curr_id: int, ticker_id: Optional[int] = None):
	"""Update next day price change to extremes 0-1."""
	where_clause = "WHERE date_id = %s AND next_day_change_in_price IS NOT NULL AND next_day_price_change_to_extremes_0_1 IS NULL"
	params = [curr_id]
	if ticker_id is not None:
		where_clause += " AND ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    UPDATE daily_delta_stock_data
    SET next_day_price_change_to_extremes_0_1 =
        CASE
            WHEN next_day_change_in_price >= 1 THEN 0.5 + next_day_change_in_price / (2 * (next_day_change_in_price + 1))
            ELSE 0.0 + next_day_change_in_price / (2 * (next_day_change_in_price + 1))
        END
    {where_clause};
"""
	cursor.execute(sql, tuple(params))


def update_raise_lower_metrics(cursor, curr_id: int, ticker_id: Optional[int] = None):
	"""Update raise/lower metrics (binary)."""
	where_clause = "WHERE date_id = %s AND next_day_change_in_price IS NOT NULL AND next_day_raise1_lower0 IS NULL"
	params = [curr_id]
	if ticker_id is not None:
		where_clause += " AND ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    UPDATE daily_delta_stock_data
    SET next_day_raise1_lower0 = CASE WHEN next_day_change_in_price >= 1 THEN 1 ELSE 0 END
    {where_clause};
"""
	cursor.execute(sql, tuple(params))


def update_top100_metrics(cursor, curr_id: int, ticker_id: Optional[int] = None):
	"""Update top 100 raise metrics."""
	where_clause = "WHERE d.date_id = %s AND d.next_day_change_in_price IS NOT NULL AND d.next_day_top100_raise1_lower0 IS NULL"
	params = [curr_id, curr_id]
	if ticker_id is not None:
		where_clause += " AND d.ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    UPDATE daily_delta_stock_data d
    JOIN (
        SELECT ticker_id, ROW_NUMBER() OVER (ORDER BY next_day_change_in_price DESC) AS rn
        FROM daily_delta_stock_data
        WHERE date_id = %s AND next_day_change_in_price IS NOT NULL
    ) r ON d.ticker_id = r.ticker_id
    SET d.next_day_top100_raise1_lower0 = IF(r.rn <= 100, 1, 0)
    {where_clause};
"""
	cursor.execute(sql, tuple(params))


def update_zero_center_extremes_neg1_pos1(cursor, curr_id: int, ticker_id: Optional[int] = None):
	"""Update zero center extremes -1 to +1."""
	where_clause = "WHERE date_id = %s AND next_day_price_change_to_extremes_0_1 IS NOT NULL AND zero_center_extremes_neg1_pos1 IS NULL"
	params = [curr_id]
	if ticker_id is not None:
		where_clause += " AND ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    UPDATE daily_delta_stock_data
    SET zero_center_extremes_neg1_pos1 = next_day_price_change_to_extremes_0_1 * 2 - 1
    {where_clause};
"""
	cursor.execute(sql, tuple(params))


def update_zero_center_top100_1_other_neg1(cursor, curr_id: int, ticker_id: Optional[int] = None):
	"""Update zero center top 100 (1) or others (-1)."""
	where_clause = "WHERE date_id = %s AND next_day_top100_raise1_lower0 IS NOT NULL AND zero_center_top100_1_other_neg1 IS NULL"
	params = [curr_id]
	if ticker_id is not None:
		where_clause += " AND ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    UPDATE daily_delta_stock_data
    SET zero_center_top100_1_other_neg1 = next_day_top100_raise1_lower0 * 2 - 1
    {where_clause};
"""
	cursor.execute(sql, tuple(params))


def update_zero_center_raise1_lower_neg1(cursor, curr_id: int, ticker_id: Optional[int] = None):
	"""Update zero center raise (1) or lower (-1)."""
	where_clause = "WHERE date_id = %s AND next_day_raise1_lower0 IS NOT NULL AND zero_center_raise1_lower_neg1 IS NULL"
	params = [curr_id]
	if ticker_id is not None:
		where_clause += " AND ticker_id = %s"
		params.append(ticker_id)

	sql = f"""
    UPDATE daily_delta_stock_data
    SET zero_center_raise1_lower_neg1 = next_day_raise1_lower0 * 2 - 1
    {where_clause};
"""
	cursor.execute(sql, tuple(params))


def _populate_initial_deltas_for_date(cursor, curr_id: int, prior_id: int, ticker_id: Optional[int] = None):
	if prior_id is None:
		return
	print(f"Populating initial deltas for date {curr_id}...")
	populate_initial_deltas(cursor, curr_id, prior_id, ticker_id=ticker_id)

def _update_next_day_change_for_date(cursor, curr_id: int, next_id: int, ticker_id: Optional[int] = None):
	if next_id is None:
		return
	print(f"Updating next day change for date {curr_id}...")
	update_next_day_change(cursor, curr_id, next_id, ticker_id=ticker_id)

def _update_remaining_metrics_for_date(cursor, curr_id: int, ticker_id: Optional[int] = None):
	print(f"Updating remaining metrics for date {curr_id}...")
	update_zero_centered_next_day_price_change(cursor, curr_id, ticker_id=ticker_id)
	update_next_day_price_change_to_extremes_0_1(cursor, curr_id, ticker_id=ticker_id)
	update_raise_lower_metrics(cursor, curr_id, ticker_id=ticker_id)
	update_top100_metrics(cursor, curr_id, ticker_id=ticker_id)
	update_zero_center_extremes_neg1_pos1(cursor, curr_id, ticker_id=ticker_id)
	update_zero_center_top100_1_other_neg1(cursor, curr_id, ticker_id=ticker_id)
	update_zero_center_raise1_lower_neg1(cursor, curr_id, ticker_id=ticker_id)


def populate_deltas(conn, date_ids: List[int], start_date_id: Optional[int] = None, end_date_id: Optional[int] = None, ticker_id: Optional[int] = None):
	"""Iterate through date IDs and execute update queries."""
	cursor = conn.cursor()
	total = len(date_ids)

	try:
		# Pass 1: Initial Deltas
		for i in range(total):
			curr_id = date_ids[i]
			if (start_date_id is not None and curr_id < start_date_id) or (end_date_id is not None and curr_id > end_date_id):
				continue
			prior_id = date_ids[i - 1] if i > 0 else None
			_populate_initial_deltas_for_date(cursor, curr_id, prior_id, ticker_id=ticker_id)
		conn.commit()

		# Pass 2: Next Day Change
		for i in range(total):
			curr_id = date_ids[i]
			if (start_date_id is not None and curr_id < start_date_id - 1) or (end_date_id is not None and curr_id > end_date_id):
				continue
			next_id = date_ids[i + 1] if i < total - 1 else None
			_update_next_day_change_for_date(cursor, curr_id, next_id, ticker_id=ticker_id)
		conn.commit()

		# Pass 3: Remaining Metrics
		for i in range(total):
			curr_id = date_ids[i]
			if (start_date_id is not None and curr_id < start_date_id - 1) or (end_date_id is not None and curr_id > end_date_id):
				continue
			_update_remaining_metrics_for_date(cursor, curr_id, ticker_id=ticker_id)
		conn.commit()

	except Exception as e:
		print(f"Error during population: {e}")
		traceback.print_exc()
	finally:
		cursor.close()


def bring_up_to_date(conn):
	"""Bring daily delta stock data up to date by processing missing dates."""
	cursor = conn.cursor()
	try:
		# Get ordered list of date_ids
		date_ids = get_ordered_date_ids(cursor)
		if not date_ids:
			print("No transaction dates found.")
			return

		# 1. Look at raw_stock data to get the list of ticker_ids and date_ids that are present
		print("Fetching (ticker_id, date_id) from raw_stock_data...")
		cursor.execute("SELECT ticker_id, date_id FROM raw_stock_data")
		raw_stock_pairs = set(cursor.fetchall())

		# 2. Get a list of date_id and ticker_ids in daily delta
		print("Fetching (ticker_id, date_id) from daily_delta_stock_data...")
		cursor.execute("SELECT ticker_id, date_id FROM daily_delta_stock_data")
		daily_delta_pairs = set(cursor.fetchall())

		# 3. do a populate_initial_deltas for anything in raw_stock_data that isn't in daily delta stock data
		missing_pairs = raw_stock_pairs - daily_delta_pairs
		if not missing_pairs:
			print("Daily delta stock data is already up to date.")
			return

		missing_date_ids = {date_id for ticker_id, date_id in missing_pairs}
		print(f"Found {len(missing_pairs)} missing records across {len(missing_date_ids)} dates.")

		# 4. do this by iterating through ordered_date_ids that comes from get_ordered_date_ids,
		# then only processing date_ids that are absent from daily delta stock data
		total = len(date_ids)
		# Pass 1: Initial Deltas
		for i in range(total):
			curr_id = date_ids[i]
			if curr_id in missing_date_ids:
				prior_id = date_ids[i - 1] if i > 0 else None
				_populate_initial_deltas_for_date(cursor, curr_id, prior_id)
		conn.commit()

		# Pass 2: Next Day Change
		for i in range(total):
			curr_id = date_ids[i]
			if curr_id in missing_date_ids or (i + 1 < total and date_ids[i + 1] in missing_date_ids):
				next_id = date_ids[i + 1] if i < total - 1 else None
				_update_next_day_change_for_date(cursor, curr_id, next_id)
		conn.commit()

		# Pass 3: Remaining Metrics
		for i in range(total):
			curr_id = date_ids[i]
			if curr_id in missing_date_ids or (i + 1 < total and date_ids[i + 1] in missing_date_ids):
				_update_remaining_metrics_for_date(cursor, curr_id)
		conn.commit()
	except Exception as e:
		print(f"Error during bring_up_to_date: {e}")
		traceback.print_exc()
	finally:
		cursor.close()


def main(argv: Optional[List[str]] = None):
	parser = argparse.ArgumentParser(description="Populate daily delta stock data.")
	parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file")
	parser.add_argument("--up-to-date", action="store_true", help="Bring daily delta stock data up to date")
	parser.add_argument("--ticker-id", type=int, help="Optional ticker_id to update")
	parser.add_argument("--date-id", type=int, help="Optional date_id to update")
	args = parser.parse_args(argv)

	conn = get_connection(dbinfo_file=args.dbinfo_file)
	try:
		if args.up_to_date:
			bring_up_to_date(conn)
		elif args.date_id is not None:
			date_ids = get_ordered_date_ids(conn.cursor())
			if args.date_id not in date_ids:
				print(f"Date ID {args.date_id} not found.")
				return
			idx = date_ids.index(args.date_id)
			prior_id = date_ids[idx - 1] if idx > 0 else None
			next_id = date_ids[idx + 1] if idx < len(date_ids) - 1 else None

			cursor = conn.cursor()
			_populate_initial_deltas_for_date(cursor, args.date_id, prior_id, ticker_id=args.ticker_id)
			_update_next_day_change_for_date(cursor, args.date_id, next_id, ticker_id=args.ticker_id)
			_update_remaining_metrics_for_date(cursor, args.date_id, ticker_id=args.ticker_id)

			print("Done populating daily delta stock data for specified date.")
			conn.commit()
		else:
			date_ids = get_ordered_date_ids(conn.cursor())
			if not date_ids:
				print("No transaction dates found.")
				return

			print(f"Found {len(date_ids)} dates. Starting population...")
			populate_deltas(conn, date_ids, ticker_id=args.ticker_id)
		print("Done.")
	finally:
		conn.close()


if __name__ == "__main__":
	main()
