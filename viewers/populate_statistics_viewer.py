import os
import sys
import json
import ast
import re as _re
from typing import Any, Dict, List, Tuple
from pandas import read_csv

def _find_epoch_files(run_path: str) -> List[Tuple[int, int, str]]:
    files: List[Tuple[int, int, str]] = []
    try:
        for fname in os.listdir(run_path):
            # Matches:
            # - stock_simulation_ensemble_22days_epoch_100.csv
            # - stock_simulation_22days_epoch_100.csv
            # - stock_simulation_100_epoch_100.csv (old format)
            # Group 1: the epoch number
            m = _re.match(r"stock_simulation_(?:ensemble_)?(?:.*?_)?epoch_(\d+)\.csv$", fname)
            if m:
                # We don't really have a 'set_size' in the filename anymore for ensemble or individual reps,
                # so we can use 0 for ensemble or just return the epoch twice if it expects a certain structure.
                # The old regex was: stock_simulation_(\d+)_epoch_(\d+)\.csv$
                # Where group 1 was the set_size (often 0 or 22 or similar).
                # Since the current return type is Tuple[int, int, str] (set_size, epoch, path),
                # let's try to extract set_size if it looks like an integer, otherwise use 0.
                set_size = 0
                # Try to find a number in the part before _epoch_
                m_size = _re.search(r"(\d+)days", fname)
                if m_size:
                    set_size = int(m_size.group(1))
                else:
                    # Fallback to old integer-based set_size if present
                    m_old = _re.match(r"stock_simulation_(\d+)_epoch_(\d+)\.csv$", fname)
                    if m_old:
                        set_size = int(m_old.group(1))

                epoch_val = int(m.group(1))
                files.append((set_size, epoch_val, os.path.join(run_path, fname)))
            elif (m := _re.match(r"stock_simulation_(\d+)\.csv$", fname)):
                # Handle cases without epoch in name if they exist, though unlikely to be "series"
                files.append((int(m.group(1)), 0, os.path.join(run_path, fname)))
    except Exception:
        pass
    return files


def build_run_epoch_series(run_path: str) -> Tuple[Dict[Any, Dict[int, float]], Dict[Any, Dict[int, float]], Dict[Any, Dict[int, float]], Dict[Any, Dict[int, float]]]:
    """Build per-epoch series for a single run directory.

    Returns four mappings:
      - by_topn:        key -> {epoch -> final_value}
      - by_topn_first5: key -> {epoch -> best_first5_value}
      - by_topn_avgcdf: key -> {epoch -> avg_cdf_value}
      - by_topn_rank:   key -> {epoch -> mean_rank_score}

    Keys use the same format as the viewers: 0 for ensemble total portfolio, or
    tuples (K, Method) where Method is one of "", "Sum", "Norm", "Rank".
    """
    by_topn: Dict[Any, Dict[int, float]] = {}
    by_topn_first5: Dict[Any, Dict[int, float]] = {}
    by_topn_avgcdf: Dict[Any, Dict[int, float]] = {}
    by_topn_rank: Dict[Any, Dict[int, float]] = {}

    # Prefer precomputed JSON if present
    summary_file = os.path.join(run_path, "run_summary_stats.json")
    if os.path.exists(summary_file):
        try:
            def _parse_series_key(n_str: str) -> Any:
                if n_str == "0":
                    return 0
                if n_str.startswith(("[", "(")):
                    try:
                        parsed = ast.literal_eval(n_str)
                        if isinstance(parsed, list):
                            parsed = tuple(parsed)
                        if isinstance(parsed, tuple) and len(parsed) == 2:
                            k_val, method_val = parsed
                            if isinstance(method_val, str):
                                return (int(k_val), method_val.capitalize())
                        return parsed
                    except Exception:
                        pass
                m = _re.match(r"^(\d+)(sum|norm|rank)$", n_str, flags=_re.IGNORECASE)
                if m:
                    return (int(m.group(1)), m.group(2).capitalize())
                if n_str.isdigit():
                    return int(n_str)
                return n_str

            with open(summary_file, "r") as f:
                summary = json.load(f)
            series_data = summary.get("epoch_series", {})
            for n_str, epochs_map in series_data.items():
                int_epochs_map = {int(k): float(v) for k, v in epochs_map.items()}
                key = _parse_series_key(n_str)
                by_topn[key] = int_epochs_map

            first5_series_data = summary.get("first5_series", {})
            for n_str, epochs_map in first5_series_data.items():
                int_epochs_map = {int(k): float(v) for k, v in epochs_map.items()}
                key = _parse_series_key(n_str)
                by_topn_first5[key] = int_epochs_map

            rank_series_data = summary.get("rank_series", {})
            if rank_series_data:
                for n_str, epochs_map in rank_series_data.items():
                    int_epochs_map = {int(k): float(v) for k, v in epochs_map.items()}
                    key = _parse_series_key(n_str)
                    by_topn_rank[key] = int_epochs_map
            else:
                # Fallback for old summary files or if not in JSON: try to build from by_topn_rank if CSV logic was used
                pass
        except Exception:
            # fall back to CSVs
            pass

    # If some data is still missing, try to build from per-epoch CSVs
    # (Existing by_topn might be from JSON, but might not have simulation metrics)
    epoch_files = _find_epoch_files(run_path) or []
    if epoch_files:
        for _set_size, epoch, fpath in epoch_files:
            try:
                df_ep = read_csv(fpath)
            except Exception:
                continue
            if df_ep is None or df_ep.empty:
                continue

            def _norm(s: str) -> str:
                return s.lower().replace(" ", "").replace("_", "")

            n_to_col: Dict[Any, str] = {}
            n_to_rank_col: Dict[Any, str] = {}
            for c in df_ep.columns:
                if not isinstance(c, str):
                    continue
                cn = _norm(c)
                if cn == "portfoliovalue":
                    n_to_col[0] = c
                    continue
                # Match "Portfolio return topN" or "Portfolio value topN"
                # Optional (Sum), (Norm), (Rank)
                # Note: "Portfolio return top1" normalized is "portfolioreturntop1"
                m = _re.match(r"^portfolio(?:return|value)top(\d+)(?:\((sum|norm|rank)\))?$", cn)
                if m:
                    k_val = int(m.group(1))
                    method_val = m.group(2).capitalize() if m.group(2) else ""
                    n_to_col[(k_val, method_val)] = c
                    continue

                # Match "Rank Percent topN"
                mr = _re.match(r"^rankpercenttop(\d+)(?:\((sum|norm|rank)\))?$", cn)
                if mr:
                    k_val = int(mr.group(1))
                    method_val = mr.group(2).capitalize() if mr.group(2) else ""
                    n_to_rank_col[(k_val, method_val)] = c

            if not n_to_col and "portfolio_value" in df_ep.columns:
                n_to_col[0] = "portfolio_value"

            for n, col in n_to_col.items():
                # Only add if not already present for this epoch from JSON
                if n in by_topn and epoch in by_topn[n]:
                    continue

                series = df_ep[col]
                try:
                    final_val = float(series.iloc[-1])
                except Exception:
                    final_val = None
                if final_val is not None:
                    by_topn.setdefault(n, {})[epoch] = final_val

                try:
                    head = series.iloc[:5]
                    first5_val = float(head.max()) if not head.empty else None
                except Exception:
                    first5_val = None
                if first5_val is not None:
                    by_topn_first5.setdefault(n, {})[epoch] = first5_val

                # Very lightweight approximation for avg CDF when a CDF-style series is present.
                # If not present, skip silently.
                if any("cdf" in str(c).lower() for c in df_ep.columns):
                    try:
                        avgcdf_val = float(series.mean()) if len(series) > 0 else None
                    except Exception:
                        avgcdf_val = None
                    if avgcdf_val is not None:
                        by_topn_avgcdf.setdefault(n, {})[epoch] = avgcdf_val

            for n, col in n_to_rank_col.items():
                if n in by_topn_rank and epoch in by_topn_rank[n]:
                    continue

                series = df_ep[col]
                try:
                    avg_rank = float(series.mean()) if len(series) > 0 else None
                except Exception:
                    avg_rank = None
                if avg_rank is not None:
                    by_topn_rank.setdefault(n, {})[epoch] = avg_rank

    return by_topn, by_topn_first5, by_topn_avgcdf, by_topn_rank


def main(argv: List[str] | None = None) -> int:
    """Profile data-frame building for one or more run directories.

    Usage: python viewers/populate_statistics_viewer.py <run_dir> [<run_dir> ...]
    Returns exit code 0 on success.
    """
    import time
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Usage: python viewers/populate_statistics_viewer.py <run_dir> [<run_dir> ...]")
        return 2

    t0 = time.perf_counter()
    total = 0
    for rp in args:
        b1, b2, b3, b4 = build_run_epoch_series(rp)
        # Lightweight sanity print to keep output small
        print(f"{os.path.basename(rp)}: final={sum(len(v) for v in b1.values())} pts, first5={sum(len(v) for v in b2.values())} pts, avgcdf={sum(len(v) for v in b3.values())} pts, rank={sum(len(v) for v in b4.values())} pts")
        total += 1
    dt = time.perf_counter() - t0
    print(f"Processed {total} runs in {dt:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
