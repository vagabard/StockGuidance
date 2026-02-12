import os
import argparse
import csv
import json
import re
import numpy as np
import traceback
from typing import List, Dict, Optional, Tuple, Any
from pathlib import Path

# Add project root to sys.path to allow imports from sibling packages like 'database'
import sys
root_path = Path(__file__).resolve().parent.parent
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from database.db_connect import get_connection

def fetch_market_deltas(min_date_id: int, max_date_id: int) -> Dict[int, List[float]]:
    """Fetch per-date market returns using daily_delta_stock_data.change_in_price_over_day."""
    conn = None
    try:
        conn = get_connection(dbinfo_file="dbinfo.id")
        cur = conn.cursor()
        query = """
            SELECT ddsd.date_id,
                   ddsd.change_in_price_over_day
            FROM daily_delta_stock_data ddsd
            WHERE ddsd.date_id BETWEEN %s AND %s
              AND ddsd.change_in_price_over_day IS NOT NULL
        """
        cur.execute(query, (min_date_id, max_date_id))
        rows = cur.fetchall()
        results: Dict[int, List[float]] = {}
        for row in rows:
            did = int(row[0])
            val = float(row[1])
            if did not in results:
                results[did] = []
            results[did].append(val)
        return results
    except Exception as e:
        print(f"Error fetching market data: {e}")
        return {}
    finally:
        if conn:
            conn.close()

def fetch_rank_ratios(date_ticker_pairs: List[Tuple[int, int]]) -> Dict[str, float]:
    """Return rank_ratio for each (date_id, ticker_id) using change_in_price_over_day."""
    if not date_ticker_pairs:
        return {}

    conn = None
    try:
        conn = get_connection(dbinfo_file="dbinfo.id")
        cur = conn.cursor()

        # Build VALUES list for the target pairs
        values_list = []
        for d, t in date_ticker_pairs:
            values_list.append(f"({int(d)},{int(t)})")
        values_clause = ",".join(values_list)

        query = f"""
            WITH targets(date_id, ticker_id) AS (
                VALUES {values_clause}
            ),
            target_deltas AS (
                SELECT t.date_id, t.ticker_id, ddsd.change_in_price_over_day as target_val
                FROM targets t
                JOIN daily_delta_stock_data ddsd ON (ddsd.date_id = t.date_id AND ddsd.ticker_id = t.ticker_id)
            ),
            market_counts AS (
                SELECT ddsd.date_id,
                       COUNT(*) as total_count,
                       SUM(CASE WHEN ddsd.change_in_price_over_day < td.target_val THEN 1 ELSE 0 END) as count_less
                FROM daily_delta_stock_data ddsd
                JOIN target_deltas td ON ddsd.date_id = td.date_id
                WHERE ddsd.change_in_price_over_day IS NOT NULL
                GROUP BY ddsd.date_id
            )
            SELECT td.date_id, td.ticker_id,
                   CAST(mc.count_less AS FLOAT) / CAST(NULLIF(mc.total_count, 0) AS FLOAT) as rank_ratio
            FROM target_deltas td
            JOIN market_counts mc ON td.date_id = mc.date_id
        """
        cur.execute(query)
        rows = cur.fetchall()
        # Key as string "date_id,ticker_id" for JSON serialization
        return {f"{row[0]},{row[1]}": float(row[2]) for row in rows}
    except Exception as e:
        print(f"Error fetching rank ratios: {e}")
        return {}
    finally:
        if conn:
            conn.close()

def generate_stats_for_csv(csv_path: str) -> Dict[str, Any]:
    print(f"Processing {csv_path}...")
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        if df.empty:
            return {}

        date_col = "open_date_id"
        if date_col not in df.columns:
            return {}

        dates = df[date_col].values
        valid_dates = [int(d) for d in dates if d > 0]
        if not valid_dates:
            return {}

        min_d = min(valid_dates)
        max_d = max(valid_dates)

        # Value columns (portfolio_value, etc.)
        value_cols = [c for c in df.columns if df[c].dtype.kind in "if" and c not in [date_col, "feature_date_id", "close_date_id"]]

        # Parse topNRecommends for rank ratios
        topn_pairs = []
        top1_map = {}
        if "topNRecommends" in df.columns:
            for idx, val in enumerate(df["topNRecommends"].astype(str).values):
                if idx >= len(dates): continue
                curr_date = int(dates[idx])
                parts = [p for p in str(val).split("|") if p]
                if parts:
                    try:
                        tid = int(parts[0])
                        topn_pairs.append((curr_date, tid))
                        top1_map[curr_date] = tid
                    except: continue

        rank_ratios = {}
        market_deltas = {}
        if topn_pairs:
            rank_ratios = fetch_rank_ratios(topn_pairs)
        else:
            market_deltas = fetch_market_deltas(min_d, max_d)

        ret_override = None
        if "top1_daily_return" in df.columns:
            ret_override = df["top1_daily_return"].astype(float).values

        results = {
            "csv_path": csv_path,
            "cdfs": {}
        }

        for col in value_cols:
            series = df[col].values
            cdf_series = []
            for i in range(len(series)):
                curr_date = int(dates[i])
                if ret_override is not None:
                    ret = ret_override[i] if i < len(ret_override) else np.nan
                elif i == 0:
                    ret = np.nan
                else:
                    prev_val = series[i-1]
                    curr_val = series[i]
                    ret = (curr_val / prev_val) - 1.0 if prev_val > 0 else np.nan

                rank_val = None
                top_tid = top1_map.get(curr_date)
                if top_tid is not None and rank_ratios:
                    rank_val = rank_ratios.get(f"{curr_date},{top_tid}")
                elif market_deltas:
                    m_vals = market_deltas.get(curr_date)
                    if m_vals and np.isfinite(ret):
                        count = sum(1 for x in m_vals if x < ret)
                        rank_val = count / float(len(m_vals))

                cdf_series.append(rank_val)

            results["cdfs"][col] = cdf_series

        return results
    except Exception:
        traceback.print_exc()
        return {}

def _norm(s: str) -> str:
    """Normalize column names for robust matching."""
    return s.lower().replace(" ", "").replace("_", "")

def get_portfolio_columns(columns: List[str]) -> Dict[Any, str]:
    """Identify portfolio-like columns and map them to (N, Method) keys."""
    n_to_col = {}
    for c in columns:
        if not isinstance(c, str):
            continue
        cn = _norm(c)
        if cn == "portfoliovalue":
            n_to_col[0] = c
            continue

        # portfolio_top<K> (Method)
        m = re.match(r"^portfolio(?:return|value)top(\d+)(?:\((sum|norm|rank)\))?$", cn)
        if m:
            k_val = int(m.group(1))
            method_val = m.group(2).capitalize() if m.group(2) else ""
            n_to_col[(k_val, method_val)] = c
            continue

        # Legacy portfolio_top<N>
        m = re.match(r"^portfoliotop(\d+)$", cn)
        if m:
            n_to_col[(int(m.group(1)), "")] = c
            continue

    if not n_to_col and "portfolio_value" in columns:
        n_to_col[0] = "portfolio_value"
    return n_to_col

def generate_run_summary(run_path: str) -> Dict[str, Any]:
    """Compute summary statistics for a run directory by scanning all simulation CSVs."""
    print(f"Generating run summary for {run_path}...")
    import pandas as pd

    summary = {
        "end_stats": {},
        "epoch_stats": {}, # epoch -> { n -> value }
        "max_stats": {},   # n -> { max_val, max_epoch, first5_max, first5_epoch }
        "epoch_series": {} # n -> { epoch -> value }
    }

    # 1. End stats
    end_pattern = re.compile(r"stock_simulation_(\d+)\.csv$")
    for fname in os.listdir(run_path):
        m = end_pattern.match(fname)
        if m:
            set_size = int(m.group(1))
            fpath = os.path.join(run_path, fname)
            try:
                df = pd.read_csv(fpath)
                if not df.empty:
                    n_to_col = get_portfolio_columns(df.columns)
                    for n, col in n_to_col.items():
                        key = f"{set_size}_{n}" if isinstance(n, int) else f"{set_size}_{n[0]}{n[1]}"
                        summary["end_stats"][key] = float(df[col].iloc[-1])
            except Exception as e:
                print(f"Error processing end CSV {fname}: {e}")

    # 2. Epoch stats
    epoch_pattern = re.compile(r"stock_simulation_(?:ensemble_)?(?:(\d+)days_)?epoch_(\d+)\.csv$")
    # Also handle the other pattern seen in CRSV: stock_simulation_(\d+)_epoch_(\d+)\.csv
    epoch_pattern2 = re.compile(r"stock_simulation_(\d+)_epoch_(\d+)\.csv$")

    max_val = {}
    max_epoch = {}
    first5_best = {}
    first5_best_epoch = {}
    epoch_series = {}
    first5_series = {}

    for fname in os.listdir(run_path):
        m = epoch_pattern.match(fname) or epoch_pattern2.match(fname)
        if not m: continue

        # Determine epoch from groups
        if "epoch_" in fname:
             # pattern 1: ensemble_..._epoch_(\d+) or (\d+)days_epoch_(\d+)
             # pattern 2: (\d+)_epoch_(\d+)
             epoch = int(m.groups()[-1])
        else:
             continue

        fpath = os.path.join(run_path, fname)
        try:
            df = pd.read_csv(fpath)
            if df.empty: continue

            n_to_col = get_portfolio_columns(df.columns)
            for n, col in n_to_col.items():
                series = df[col]
                final_val = float(series.iloc[-1])

                # Update series
                key = str(n) # JSON keys must be strings
                if key not in epoch_series: epoch_series[key] = {}
                epoch_series[key][epoch] = final_val

                # Update max
                if n not in max_val or final_val > max_val[n]:
                    max_val[n] = final_val
                    max_epoch[n] = epoch

                # First 5
                head = series.iloc[:5]
                if not head.empty:
                    head_max = float(head.max())
                    # Series for charting
                    key = str(n)
                    if key not in first5_series: first5_series[key] = {}
                    first5_series[key][epoch] = head_max

                    if n not in first5_best or head_max > first5_best[n]:
                        first5_best[n] = head_max
                        first5_best_epoch[n] = epoch
        except Exception as e:
            print(f"Error processing epoch CSV {fname}: {e}")

    # Format max stats for output
    for n in max_val:
        key = str(n)
        summary["max_stats"][key] = {
            "max_val": max_val[n],
            "max_epoch": max_epoch.get(n),
            "first5_max": first5_best.get(n),
            "first5_epoch": first5_best_epoch.get(n)
        }
    summary["epoch_series"] = epoch_series
    summary["first5_series"] = first5_series

    return summary

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", help="CSV files or directories to scan")
    parser.add_argument("--force", action="store_true", help="Force regeneration")
    parser.add_argument("--summary-only", action="store_true", help="Only generate run_summary_stats.json")
    args = parser.parse_args()

    # Identify directories to process for summaries
    dirs_to_process = set()
    csv_files = []

    for p in args.paths:
        if os.path.isfile(p):
            if p.endswith(".csv"):
                csv_files.append(p)
                dirs_to_process.add(os.path.dirname(os.path.abspath(p)))
        elif os.path.isdir(p):
            dirs_to_process.add(os.path.abspath(p))
            # Also find all subdirectories that might be runs
            for root, dirs, files in os.walk(p):
                if any(f.startswith("stock_simulation_") for f in files):
                    dirs_to_process.add(os.path.abspath(root))
                for f in files:
                    if f.startswith("stock_simulation_") and f.endswith(".csv"):
                        csv_files.append(os.path.join(root, f))

    if not args.summary_only:
        for csv_file in csv_files:
            stats_file = csv_file + ".stats.json"
            if os.path.exists(stats_file) and not args.force:
                print(f"Stats already exist for {csv_file}, skipping.")
                continue

            stats = generate_stats_for_csv(csv_file)
            if stats:
                with open(stats_file, 'w') as f:
                    json.dump(stats, f)
                print(f"Saved stats to {stats_file}")

    for run_dir in dirs_to_process:
        summary_file = os.path.join(run_dir, "run_summary_stats.json")
        if os.path.exists(summary_file) and not args.force:
            print(f"Summary already exists for {run_dir}, skipping.")
            continue

        summary = generate_run_summary(run_dir)
        if summary and (summary["end_stats"] or summary["max_stats"]):
            with open(summary_file, 'w') as f:
                json.dump(summary, f)
            print(f"Saved summary to {summary_file}")

if __name__ == "__main__":
    main()
