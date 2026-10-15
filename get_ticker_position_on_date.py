from __future__ import annotations

import argparse
from typing import List, Optional, Dict

import numpy as np
from database.db_connect import get_connection

def get_ticker_positions(
    ticker_ids: List[int],
    date_id: int,
    dbinfo_file: str = "dbinfo.id",
    pool_ticker_ids: Optional[List[int]] = None
) -> Dict[int, float]:
    """
    Calculates the relative performance position for a list of ticker IDs on a specific date.

    Position = (Number of tickers with a lower close/open ratio) / (Total number of tickers)
    Results are between 0 and 1.

    If pool_ticker_ids is provided, the distribution is filtered to only include those tickers.
    """
    conn = get_connection(dbinfo_file=dbinfo_file)
    try:
        cur = conn.cursor()

        # 1. Fetch ratios for ALL tickers on that date to build the distribution
        # We handle open=0 or NULL by ignoring them or treating them as NaN
        sql_all = """
            SELECT ticker_id, (close / open) as ratio
            FROM raw_stock_data
            WHERE date_id = %s
              AND open IS NOT NULL
              AND open > 0
              AND close IS NOT NULL
        """
        cur.execute(sql_all, (date_id,))

        ticker_to_ratio = {}
        pool_set = set(pool_ticker_ids) if pool_ticker_ids is not None else None

        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                tid = int(row[0])
                if pool_set is not None and tid not in pool_set:
                    continue
                ticker_to_ratio[tid] = float(row[1])

        if not ticker_to_ratio:
            return {tid: 0.0 for tid in ticker_ids}

        all_ratios = np.array(list(ticker_to_ratio.values()))
        total_count = len(all_ratios)
        all_ratios_sorted = np.sort(all_ratios)

        results = {}
        for tid in ticker_ids:
            if tid not in ticker_to_ratio:
                # Ticker not found on this date (or not in pool)
                results[tid] = 0.0
                continue

            ratio = ticker_to_ratio[tid]
            # count the number of tickers that have a lower ratio
            # searchsorted with side='left' gives the index of the first element >= ratio
            # which is exactly the count of elements < ratio.
            lower_count = np.searchsorted(all_ratios_sorted, ratio, side='left')

            position = float(lower_count) / total_count
            results[tid] = position

        return results

    finally:
        try:
            conn.close()
        except Exception:
            pass

def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="Get ticker position on date based on close/open ratio.")
    parser.add_argument("--ticker-ids", type=str, required=True, help="Comma-separated list of ticker IDs")
    parser.add_argument("--date-id", type=int, required=True, help="Date ID to query")
    parser.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file")
    return parser.parse_args(argv)

def main(argv: Optional[List[str]] = None):
    args = parse_args(argv)

    ticker_ids = [int(tid.strip()) for tid in args.ticker_ids.split(",") if tid.strip()]

    positions = get_ticker_positions(ticker_ids, args.date_id, args.dbinfo_file)

    print(f"Positions for date_id {args.date_id}:")
    for tid in ticker_ids:
        pos = positions.get(tid, 0.0)
        print(f"  Ticker {tid}: {pos:.4f}")

if __name__ == "__main__":
    main()
