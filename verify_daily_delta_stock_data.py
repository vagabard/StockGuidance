"""
Verify daily_delta_stock_data table against ratios computed from raw_stock_data.

Behavior:
- Randomly choose a trading date (date_id) from available raw_stock_data dates
  unless --date-id is provided.
- Determine the immediately previous trading date_id.
- For all tickers that have rows on the chosen day, compute ratios of
  current_day_value / prior_day_value for the following fields:
  open, high, low, close, volume, vwap, transactions.
- Compare these to the corresponding columns in daily_delta_stock_data for
  the chosen date. If they differ, print the transaction_date (YYYY-MM-DD),
  ticker_id, field name, stored value, computed value, and their difference.

Exit codes:
- 0 if no mismatches were found
- 1 if there were mismatches
- 2 for argument or environment problems (e.g., not enough dates)
"""
from __future__ import annotations

import argparse
import random
from typing import Dict, Optional, Tuple

from db_connect import get_connection


FIELDS = ("open", "high", "low", "close", "volume", "vwap", "transactions")


def parse_args(argv: Optional[list[str]] = None):
    p = argparse.ArgumentParser(description="Verify daily_delta_stock_data values vs raw_stock_data ratios.")
    p.add_argument("--host", default="localhost", help="MySQL host (default: localhost)")
    p.add_argument("--database", default="stockforecasting", help="Database/schema name (default: stockforecasting)")
    p.add_argument("--user", required=True, help="MySQL user")
    p.add_argument("--password", required=True, help="MySQL password")
    p.add_argument("--port", type=int, default=3306, help="MySQL port (default: 3306)")
    p.add_argument("--driver", default="auto", choices=["auto", "mysqlconnector", "pymysql"], help="DB driver (default: auto)")
    p.add_argument("--date-id", type=int, help="Specific date_id to verify (if omitted, choose randomly)")
    p.add_argument("--seed", type=int, help="Random seed (only used when choosing a random date)")
    p.add_argument(
        "--tolerance",
        type=float,
        default=0.0001,
        help="Numeric tolerance for comparison (default: 0.0001)",
    )
    p.add_argument("--repeat", type=int, default=1000, help="How many times to repeat the check (default: 1000)")
    return p.parse_args(argv)


def _pick_date_ids(cur, specified_date_id: Optional[int]) -> Tuple[int, int, str]:
    """Return (date_id, prior_date_id, date_str). Raises RuntimeError if not enough dates."""
    # Load all date_ids that appear in raw_stock_data, ordered
    cur.execute("SELECT DISTINCT date_id FROM raw_stock_data ORDER BY date_id ASC")
    date_ids = [int(r[0]) for r in cur.fetchall()]
    if len(date_ids) < 2:
        raise RuntimeError("Not enough dates in raw_stock_data to compute prior-day ratios.")

    if specified_date_id is not None:
        if specified_date_id not in date_ids:
            raise RuntimeError(f"Specified date_id {specified_date_id} not present in raw_stock_data.")
        idx = date_ids.index(specified_date_id)
        if idx == 0:
            raise RuntimeError(f"Specified date_id {specified_date_id} has no prior date in raw_stock_data.")
    else:
        # Pick randomly from all but the first (since first has no prior)
        idx = random.randrange(1, len(date_ids))

    date_id = date_ids[idx]
    prior_date_id = date_ids[idx - 1]

    # Fetch transaction_date string for display
    cur.execute("SELECT transaction_date FROM transaction_dates WHERE id = %s", (date_id,))
    row = cur.fetchone()
    date_str = str(row[0]) if row and row[0] is not None else f"date_id={date_id}"
    return date_id, prior_date_id, date_str


def _load_raw_for_dates(cur, date_id: int, prior_date_id: int) -> Tuple[Dict[int, Tuple], Dict[int, Tuple]]:
    """Return two dicts mapping ticker_id -> tuple(values...) for chosen and prior dates."""
    cur.execute(
        """
        SELECT ticker_id, `open`, high, `low`, `close`, volume, vwap, transactions
        FROM raw_stock_data
        WHERE date_id IN (%s, %s)
        """,
        (date_id, prior_date_id),
    )
    current: Dict[int, Tuple] = {}
    prior: Dict[int, Tuple] = {}
    for row in cur.fetchall():
        tid = int(row[0])
        values = tuple(row[1:])  # 7 values
        # Decide which bucket to place based on existence in current/prior dates
        # We need two separate queries or detect date per row; fetch again with date

    # Rerun with explicit date
    cur.execute(
        """
        SELECT date_id, ticker_id, `open`, high, `low`, `close`, volume, vwap, transactions
        FROM raw_stock_data
        WHERE date_id IN (%s, %s)
        """,
        (date_id, prior_date_id),
    )
    for row in cur.fetchall():
        did = int(row[0])
        tid = int(row[1])
        values = tuple(row[2:])
        if did == date_id:
            current[tid] = values
        else:
            prior[tid] = values
    return current, prior


def _safe_div(n, d):
    if n is None or d is None:
        return None
    try:
        d = float(d)
        if d == 0.0:
            return None
        return float(n) / d
    except Exception:
        return None


def _compute_ratios(current: Dict[int, Tuple], prior: Dict[int, Tuple]) -> Dict[int, Dict[str, Optional[float]]]:
    ratios: Dict[int, Dict[str, Optional[float]]] = {}
    for tid, cur_vals in current.items():
        prev_vals = prior.get(tid)
        result: Dict[str, Optional[float]] = {}
        if prev_vals is None:
            # No prior row -> all ratios are None
            for f in FIELDS:
                result[f] = None
        else:
            for i, f in enumerate(FIELDS):
                result[f] = _safe_div(cur_vals[i], prev_vals[i])
        ratios[tid] = result
    return ratios


def _load_daily_delta(cur, date_id: int) -> Dict[int, Dict[str, Optional[float]]]:
    sql = (
        """
        SELECT ticker_id,
               open_ratio_to_prior,
               high_ratio_to_prior,
               low_ratio_to_prior,
               close_ratio_to_prior,
               volume_ratio_to_prior,
               vwap_ratio_to_prior,
               transactions_ratio_to_prior
        FROM daily_delta_stock_data
        WHERE date_id = %s
        """
    )
    cur.execute(sql, (date_id,))
    out: Dict[int, Dict[str, Optional[float]]] = {}
    for row in cur.fetchall():
        tid = int(row[0])
        vals = row[1:]
        out[tid] = {f: (None if v is None else float(v)) for f, v in zip(FIELDS, vals)}
    return out


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.seed is not None:
        random.seed(args.seed)

    conn = get_connection(
        host=args.host,
        database=args.database,
        user=args.user,
        password=args.password,
        port=args.port,
        driver=args.driver,
    )
    try:
        cur = conn.cursor()

        total_mismatches = 0
        iterations_with_mismatch = 0
        tol = float(args.tolerance or 0.0)

        repeats = max(1, int(args.repeat))
        for i in range(1, repeats + 1):
            date_id, prior_date_id, date_str = _pick_date_ids(cur, args.date_id)

            current, prior = _load_raw_for_dates(cur, date_id, prior_date_id)
            computed = _compute_ratios(current, prior)
            stored = _load_daily_delta(cur, date_id)

            mismatches = 0

            # Compare across tickers that exist in either set for completeness
            all_tickers = set(computed.keys()) | set(stored.keys())
            for tid in sorted(all_tickers):
                comp_map = computed.get(tid, {f: None for f in FIELDS})
                stor_map = stored.get(tid, {f: None for f in FIELDS})
                for f in FIELDS:
                    cv = comp_map.get(f)
                    sv = stor_map.get(f)
                    equal: bool
                    if cv is None and sv is None:
                        equal = True
                    elif cv is None or sv is None:
                        equal = False
                    else:
                        diff = abs(cv - sv)
                        equal = diff <= tol
                    if not equal:
                        # Recompute diff for display
                        if cv is None or sv is None:
                            diff_str = "n/a"
                        else:
                            diff_str = f"{cv - sv:.10f}"
                        print(
                            f"[iter {i}] {date_str}  date_id={date_id} prior_date_id={prior_date_id}    ticker_id={tid} field={f} stored={sv} computed={cv} diff={diff_str}  "
                        )
                        mismatches += 1

            if mismatches == 0:
                print(f"[iter {i}] All ratios match for date {date_str} (date_id={date_id}).")
            else:
                print(f"[iter {i}] Found {mismatches} mismatches for date {date_str} (date_id={date_id}).")
                iterations_with_mismatch += 1
                total_mismatches += mismatches

        if total_mismatches == 0:
            print(f"Done. {repeats} iterations, no mismatches found.")
            return 0
        else:
            print(
                f"Done. {repeats} iterations, mismatches found in {iterations_with_mismatch} iteration(s), total mismatches: {total_mismatches}."
            )
            return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
