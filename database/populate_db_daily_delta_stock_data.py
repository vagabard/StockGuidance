"""
Populate daily_delta_stock_data by calling the stored procedure for each transaction date.
The procedure is expected to take: current_date_id, prior_date_id, next_date_id.
"""

import argparse
import traceback
from typing import List, Optional

from database.db_connect import get_connection


def ensure_stored_procedure(conn):
	"""Ensure the populate_daily_delta_stock_data stored procedure exists."""
	cursor = conn.cursor()
	try:
		# Check if procedure exists
		cursor.execute("SHOW PROCEDURE STATUS WHERE Name = 'populate_one_day_daily_delta_stock_data'")
		if not cursor.fetchone():

			print("Creating stored procedure populate_one_day_daily_delta_stock_data...")
			# The procedure definition
			populate_one_day_daily_delta_stock_data_sql = """
	CREATE PROCEDURE `populate_one_day_daily_delta_stock_data`(
	    IN p_date_id INT,
	    IN p_prior_date_id INT,
	    IN p_next_date_id INT
	)
	BEGIN
	    -- 1. Insert any missing rows, including change_in_price_over_day
	    -- We use p_date_id as the current day and p_prior_date_id for the prior day's data.
	    INSERT INTO daily_delta_stock_data (
	        ticker_id,
	        date_id,
	        open_ratio_to_prior,
	        high_ratio_to_prior,
	        low_ratio_to_prior,
	        close_ratio_to_prior,
	        volume_ratio_to_prior,
	        vwap_ratio_to_prior,
	        transactions_ratio_to_prior,
	        change_in_price_over_day
	    )
	    SELECT
	        curr.ticker_id,
	        curr.date_id,
	        curr.open   / NULLIF(prior.open,   0),
	        curr.high   / NULLIF(prior.high,   0),
	        curr.low    / NULLIF(prior.low,    0),
	        curr.close  / NULLIF(prior.close,  0),
	        curr.volume / NULLIF(prior.volume, 0),
	        curr.vwap   / NULLIF(prior.vwap,   0),
	        curr.transactions / NULLIF(prior.transactions, 0),
	        curr.close / NULLIF(curr.open, 0) AS change_in_price_over_day
	    FROM raw_stock_data curr
	    JOIN raw_stock_data prior ON curr.ticker_id = prior.ticker_id AND prior.date_id = p_prior_date_id
	    LEFT JOIN daily_delta_stock_data ddsd ON curr.ticker_id = ddsd.ticker_id AND curr.date_id = ddsd.date_id
	    WHERE curr.date_id = p_date_id
	      AND ddsd.ticker_id IS NULL;
"""
		cursor.execute(populate_one_day_daily_delta_stock_data_sql)
		conn.commit()
	finally:
		cursor.close()

	next_day_change_price_sql =	"""
    -- 2. Fill next_day_change_in_price using the successor row per ticker
    -- We use p_next_date_id to get the change_in_price_over_day from the next day.
    IF p_next_date_id IS NOT NULL THEN
        UPDATE daily_delta_stock_data d
        JOIN daily_delta_stock_data next_day
          ON d.ticker_id = next_day.ticker_id
         AND next_day.date_id = p_next_date_id
        SET d.next_day_change_in_price = next_day.change_in_price_over_day
        WHERE d.date_id = p_date_id
          AND d.next_day_change_in_price IS NULL;
    END IF;
"""

	next_day_price_change_to_extremes_sql =	"""
    -- 3. zero_centered_next_day_price_change = next_day_change_in_price - 1
    UPDATE daily_delta_stock_data
    SET zero_centered_next_day_price_change = next_day_change_in_price - 1
    WHERE date_id = p_date_id
      AND next_day_change_in_price IS NOT NULL
      AND zero_centered_next_day_price_change IS NULL;


    -- 4. next_day_price_change_to_extremes_0_1
    UPDATE daily_delta_stock_data
    SET next_day_price_change_to_extremes_0_1 =
        CASE
            WHEN next_day_change_in_price >= 1 THEN
                0.5 + next_day_change_in_price /
                      (2 * (next_day_change_in_price + 1))
            ELSE
                0.0 + next_day_change_in_price /
                      (2 * (next_day_change_in_price + 1))
        END
    WHERE date_id = p_date_id
      AND next_day_change_in_price IS NOT NULL
      AND next_day_price_change_to_extremes_0_1 IS NULL;


    -- 5. next_day_raise1_lower0
    UPDATE daily_delta_stock_data
    SET next_day_raise1_lower0 =
        CASE
            WHEN next_day_change_in_price >= 1 THEN 1
            ELSE 0
        END
    WHERE date_id = p_date_id
      AND next_day_change_in_price IS NOT NULL
      AND next_day_raise1_lower0 IS NULL;


    -- 6. next_day_top100_raise1_lower0
    -- This still requires a window function or a subquery to find top 100 for the date.
    -- But it's scoped to only the current date p_date_id.
    UPDATE daily_delta_stock_data d
    JOIN (
        SELECT
            ticker_id,
            ROW_NUMBER() OVER (
                ORDER BY next_day_change_in_price DESC
            ) AS rn
        FROM daily_delta_stock_data
        WHERE date_id = p_date_id
          AND next_day_change_in_price IS NOT NULL
    ) r
      ON d.ticker_id = r.ticker_id
    SET d.next_day_top100_raise1_lower0 = IF(r.rn <= 100, 1, 0)
    WHERE d.date_id = p_date_id
      AND d.next_day_change_in_price IS NOT NULL
      AND d.next_day_top100_raise1_lower0 IS NULL;


    -- 7. zero_center_extremes_neg1_pos1 = next_day_price_change_to_extremes_0_1 * 2 - 1
    UPDATE daily_delta_stock_data
    SET zero_center_extremes_neg1_pos1 =
        next_day_price_change_to_extremes_0_1 * 2 - 1
    WHERE date_id = p_date_id
      AND next_day_price_change_to_extremes_0_1 IS NOT NULL
      AND zero_center_extremes_neg1_pos1 IS NULL;


    -- 8. zero_center_top100_1_other_neg1 = next_day_top100_raise1_lower0 * 2 - 1
    UPDATE daily_delta_stock_data
    SET zero_center_top100_1_other_neg1 =
        next_day_top100_raise1_lower0 * 2 - 1
    WHERE date_id = p_date_id
      AND next_day_top100_raise1_lower0 IS NOT NULL
      AND zero_center_top100_1_other_neg1 IS NULL;


    -- 9. zero_center_raise1_lower_neg1 = next_day_raise1_lower0 * 2 - 1
    UPDATE daily_delta_stock_data
    SET zero_center_raise1_lower_neg1 =
        next_day_raise1_lower0 * 2 - 1
    WHERE date_id = p_date_id
      AND next_day_raise1_lower0 IS NOT NULL
      AND zero_center_raise1_lower_neg1 IS NULL;

END
"""


def get_ordered_date_ids(cursor) -> List[int]:
	"""Get ordered list of transaction date IDs from old to new."""
	cursor.execute("SELECT id FROM transaction_dates ORDER BY transaction_date ASC")
	return [row[0] for row in cursor.fetchall()]


def populate_deltas(conn, date_ids: List[int], start_date_id: Optional[int] = None, end_date_id: Optional[int] = None):
	"""Iterate through date IDs and call the stored procedure."""
	ensure_stored_procedure(conn)
	cursor = conn.cursor()
	total = len(date_ids)

	id_to_idx = {did: i for i, did in enumerate(date_ids)}

	try:
		for i in range(total):
			curr_id = date_ids[i]

			if start_date_id is not None and curr_id < start_date_id:
				continue
			if end_date_id is not None and curr_id > end_date_id:
				continue

			prior_id = date_ids[i - 1] if i > 0 else None
			next_id = date_ids[i + 1] if i < total - 1 else None

			if prior_id is None:
				# Skip the first date as we need prior day data for deltas
				continue

			print(f"Calling populate_daily_delta_stock_data({curr_id}, {prior_id}, {next_id})")

			# Call the procedure: populate_daily_delta_stock_data(p_date_id, p_prior_date_id, p_next_date_id)
			cursor.execute(
				"CALL populate_daily_delta_stock_data(%s, %s, %s)",
				(curr_id, prior_id, next_id)
			)
			# Committing every step or every N steps might be safer/better for long runs
			conn.commit()

	except Exception as e:
		print(f"Error during population: {e}")
		traceback.print_exc()
	finally:
		cursor.close()


def main(argv: Optional[List[str]] = None):
	parser = argparse.ArgumentParser(description="Populate daily delta stock data.")
	parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file")
	args = parser.parse_args(argv)

	conn = get_connection(dbinfo_file=args.dbinfo_file)
	try:
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
