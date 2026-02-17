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
            m = _re.match(r"stock_simulation_(\d+)_epoch_(\d+)\.csv$", fname)
            if m:
                files.append((int(m.group(1)), int(m.group(2)), os.path.join(run_path, fname)))
    except Exception:
        pass
    return files


def build_run_epoch_series(run_path: str) -> Tuple[Dict[Any, Dict[int, float]], Dict[Any, Dict[int, float]], Dict[Any, Dict[int, float]]]:
    """Build per-epoch series for a single run directory.

    Returns three mappings:
      - by_topn:        key -> {epoch -> final_value}
      - by_topn_first5: key -> {epoch -> best_first5_value}
      - by_topn_avgcdf: key -> {epoch -> avg_cdf_value}

    Keys use the same format as the viewers: 0 for ensemble total portfolio, or
    tuples (K, Method) where Method is one of "", "Sum", "Norm", "Rank".
    """
    by_topn: Dict[Any, Dict[int, float]] = {}
    by_topn_first5: Dict[Any, Dict[int, float]] = {}
    by_topn_avgcdf: Dict[Any, Dict[int, float]] = {}

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
        except Exception:
            # fall back to CSVs
            pass

    if not by_topn and not by_topn_first5:
        # Build from per-epoch CSVs
        for _set_size, epoch, fpath in (_find_epoch_files(run_path) or []):
            try:
                df_ep = read_csv(fpath)
            except Exception:
                continue
            if df_ep is None or df_ep.empty:
                continue

            def _norm(s: str) -> str:
                return s.lower().replace(" ", "").replace("_", "")

            n_to_col: Dict[Any, str] = {}
            for c in df_ep.columns:
                if not isinstance(c, str):
                    continue
                cn = _norm(c)
                if cn == "portfoliovalue":
                    n_to_col[0] = c
                    continue
                m = _re.match(r"^portfolio(?:return|value)top(\d+)(?:\((sum|norm|rank)\))?$", cn)
                if m:
                    k_val = int(m.group(1))
                    method_val = m.group(2).capitalize() if m.group(2) else ""
                    n_to_col[(k_val, method_val)] = c
                    continue
                m = _re.match(r"^portfoliotop(\d+)$", cn)
                if m:
                    n_to_col[(int(m.group(1)), "")] = c
                    continue
            if not n_to_col and "portfolio_value" in df_ep.columns:
                n_to_col[0] = "portfolio_value"

            for n, col in n_to_col.items():
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

    return by_topn, by_topn_first5, by_topn_avgcdf


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
        b1, b2, b3 = build_run_epoch_series(rp)
        # Lightweight sanity print to keep output small
        print(f"{os.path.basename(rp)}: final={sum(len(v) for v in b1.values())} pts, first5={sum(len(v) for v in b2.values())} pts, avgcdf={sum(len(v) for v in b3.values())} pts")
        total += 1
    dt = time.perf_counter() - t0
    print(f"Processed {total} runs in {dt:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
