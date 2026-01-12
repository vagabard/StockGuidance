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


def populate_initial_deltas(cursor, curr_id: int, prior_id: int):
	"""Populate initial delta values for a given date based on prior date."""
	sql = """
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
    WHERE curr.date_id = %s AND ddsd.ticker_id IS NULL;
"""
	cursor.execute(sql, (prior_id, curr_id))


def update_next_day_change(cursor, curr_id: int, next_id: int):
	"""Update next day change in price for a given date based on next date."""
	sql = """
    UPDATE daily_delta_stock_data d
    JOIN daily_delta_stock_data next_day ON d.ticker_id = next_day.ticker_id AND next_day.date_id = %s
    SET d.next_day_change_in_price = next_day.change_in_price_over_day
    WHERE d.date_id = %s AND d.next_day_change_in_price IS NULL;
"""
	cursor.execute(sql, (next_id, curr_id))


def update_zero_centered_next_day_price_change(cursor, curr_id: int):
	"""Update zero centered next day price change."""
	sql = """
    UPDATE daily_delta_stock_data
    SET zero_centered_next_day_price_change = next_day_change_in_price - 1
    WHERE date_id = %s AND next_day_change_in_price IS NOT NULL AND zero_centered_next_day_price_change IS NULL;
"""
	cursor.execute(sql, (curr_id,))


def update_next_day_price_change_to_extremes_0_1(cursor, curr_id: int):
	"""Update next day price change to extremes 0-1."""
	sql = """
    UPDATE daily_delta_stock_data
    SET next_day_price_change_to_extremes_0_1 =
        CASE
            WHEN next_day_change_in_price >= 1 THEN 0.5 + next_day_change_in_price / (2 * (next_day_change_in_price + 1))
            ELSE 0.0 + next_day_change_in_price / (2 * (next_day_change_in_price + 1))
        END
    WHERE date_id = %s AND next_day_change_in_price IS NOT NULL AND next_day_price_change_to_extremes_0_1 IS NULL;
"""
	cursor.execute(sql, (curr_id,))


def update_raise_lower_metrics(cursor, curr_id: int):
	"""Update raise/lower metrics (binary)."""
	sql = """
    UPDATE daily_delta_stock_data
    SET next_day_raise1_lower0 = CASE WHEN next_day_change_in_price >= 1 THEN 1 ELSE 0 END
    WHERE date_id = %s AND next_day_change_in_price IS NOT NULL AND next_day_raise1_lower0 IS NULL;
"""
	cursor.execute(sql, (curr_id,))


def update_top100_metrics(cursor, curr_id: int):
	"""Update top 100 raise metrics."""
	sql = """
    UPDATE daily_delta_stock_data d
    JOIN (
        SELECT ticker_id, ROW_NUMBER() OVER (ORDER BY next_day_change_in_price DESC) AS rn
        FROM daily_delta_stock_data
        WHERE date_id = %s AND next_day_change_in_price IS NOT NULL
    ) r ON d.ticker_id = r.ticker_id
    SET d.next_day_top100_raise1_lower0 = IF(r.rn <= 100, 1, 0)
    WHERE d.date_id = %s AND d.next_day_change_in_price IS NOT NULL AND d.next_day_top100_raise1_lower0 IS NULL;
"""
	cursor.execute(sql, (curr_id, curr_id))


def update_zero_center_extremes_neg1_pos1(cursor, curr_id: int):
	"""Update zero center extremes -1 to +1."""
	sql = """
    UPDATE daily_delta_stock_data
    SET zero_center_extremes_neg1_pos1 = next_day_price_change_to_extremes_0_1 * 2 - 1
    WHERE date_id = %s AND next_day_price_change_to_extremes_0_1 IS NOT NULL AND zero_center_extremes_neg1_pos1 IS NULL;
"""
	cursor.execute(sql, (curr_id,))


def update_zero_center_top100_1_other_neg1(cursor, curr_id: int):
	"""Update zero center top 100 (1) or others (-1)."""
	sql = """
    UPDATE daily_delta_stock_data
    SET zero_center_top100_1_other_neg1 = next_day_top100_raise1_lower0 * 2 - 1
    WHERE date_id = %s AND next_day_top100_raise1_lower0 IS NOT NULL AND zero_center_top100_1_other_neg1 IS NULL;
"""
	cursor.execute(sql, (curr_id,))


def update_zero_center_raise1_lower_neg1(cursor, curr_id: int):
	"""Update zero center raise (1) or lower (-1)."""
	sql = """
    UPDATE daily_delta_stock_data
    SET zero_center_raise1_lower_neg1 = next_day_raise1_lower0 * 2 - 1
    WHERE date_id = %s AND next_day_raise1_lower0 IS NOT NULL AND zero_center_raise1_lower_neg1 IS NULL;
"""
	cursor.execute(sql, (curr_id,))


def populate_deltas(conn, date_ids: List[int], start_date_id: Optional[int] = None, end_date_id: Optional[int] = None):
	"""Iterate through date IDs and execute update queries."""
	cursor = conn.cursor()
	total = len(date_ids)

	try:
		for i in range(total):
			curr_id = date_ids[i]

			if start_date_id is not None and curr_id < start_date_id:
				continue
			if end_date_id is not None and curr_id > end_date_id:
				continue
			prior_id = date_ids[i - 1] if i > 0 else None
			next_id = date_ids[i + 1] if i < total - 1 else None
			_populate_deltas_for_date(cursor, curr_id, prior_id, next_id)
			conn.commit()

	except Exception as e:
		print(f"Error during population: {e}")
		traceback.print_exc()
	finally:
		cursor.close()

def _populate_deltas_for_date(cursor, curr_id: int, prior_id: int, next_id: int):

	if prior_id is None:
		# Skip the first date as we need prior day data for deltas
		return

	print(f"Executing update queries for date {curr_id}...")

	# Execute each update via individual functions
	populate_initial_deltas(cursor, curr_id, prior_id)
	if next_id is not None:
		update_next_day_change(cursor, curr_id, next_id)

	update_zero_centered_next_day_price_change(cursor, curr_id)
	update_next_day_price_change_to_extremes_0_1(cursor, curr_id)
	update_raise_lower_metrics(cursor, curr_id)
	update_top100_metrics(cursor, curr_id)
	update_zero_center_extremes_neg1_pos1(cursor, curr_id)
	update_zero_center_top100_1_other_neg1(cursor, curr_id)
	update_zero_center_raise1_lower_neg1(cursor, curr_id)


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
		for i in range(total):
			curr_id = date_ids[i]
			if curr_id in missing_date_ids:
				prior_id = date_ids[i - 1] if i > 0 else None
				next_id = date_ids[i + 1] if i < total - 1 else None

				_populate_deltas_for_date(cursor, curr_id, prior_id, next_id)


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
	args = parser.parse_args(argv)

	conn = get_connection(dbinfo_file=args.dbinfo_file)
	try:
		if args.up_to_date:
			bring_up_to_date(conn)
		else:
			date_ids = get_ordered_date_ids(conn.cursor())
			if not date_ids:
				print("No transaction dates found.")
				return

			print(f"Found {len(date_ids)} dates. Starting population...")
			populate_deltas(conn, date_ids)
		print("Done.")
	finally:
		conn.close()


if __name__ == "__main__":
	main()
