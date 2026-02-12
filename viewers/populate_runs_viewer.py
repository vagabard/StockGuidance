import os
import sys
import io
import json
import re
from typing import Dict, List, Any, Tuple
import streamlit as st
import pandas as pd
import numpy as np

# Data-building helpers for profiling and reuse

def build_metrics_df_for_run(run_dir_abs: str) -> pd.DataFrame | None:
    """Load a metrics DataFrame from a run directory (absolute path).
    Tries `metrics.csv` then `training_metrics.csv`.
    """
    p = os.path.join(run_dir_abs, "metrics.csv")
    if not os.path.exists(p):
        p = os.path.join(run_dir_abs, "training_metrics.csv")
    if os.path.exists(p):
        try:
            return pd.read_csv(p)
        except Exception:
            return None
    return None


def main(argv: List[str] | None = None) -> int:
    """Profile metric DataFrame loading for one or more run directories.

    Usage: python viewers/populate_runs_viewer.py <run_dir> [<run_dir> ...]
    """
    import time
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Usage: python viewers/populate_runs_viewer.py <run_dir> [<run_dir> ...]")
        return 2

    t0 = time.perf_counter()
    ok = 0
    for rp in args:
        df = build_metrics_df_for_run(rp)
        rows = 0 if df is None else len(df)
        cols = 0 if df is None else len(df.columns)
        print(f"{os.path.basename(rp)}: df={'None' if df is None else f'{rows}x{cols}'}")
        ok += 1
    dt = time.perf_counter() - t0
    print(f"Processed {ok} runs in {dt:.3f}s")
    return 0


def populate_runs_graphs(
    selected_runs: List[str],
    display_rows: List[Dict[str, Any]],
    ts_to_runs: Dict[str, List[Tuple[str, Dict[str, Any]]]],
    show_extended: bool,
    base_dir: str,
    ignore_head: int,
    log_y: bool,
    single_chart: bool,
    plot_lines_func, # Pass the function to avoid duplication or circular imports
    viewer_metric_checks: Dict[str, bool],
):
    if not selected_runs:
        st.info("No runs selected for viewing yet. Use the sidebar or the grid above to pick some runs.")
        return

    chosen_params: List[Dict[str, Any]] = []
    for run_id in selected_runs:
        match = None
        for r in display_rows:
            if (r.get("_ts") or r.get("_dir")) == run_id:
                match = r
                break
        if match:
            if not show_extended:
                ts_name = match.get("_ts")
                run_list = ts_to_runs.get(str(ts_name), [])
                for _name, p in run_list:
                    chosen_params.append(p)
            else:
                chosen_params.append(match)

    def _load_metrics_df(run_dir_name: str) -> pd.DataFrame | None:
        p = os.path.join(base_dir, run_dir_name, "metrics.csv")
        if not os.path.exists(p):
            p = os.path.join(base_dir, run_dir_name, "training_metrics.csv")
        if os.path.exists(p):
            try:
                return pd.read_csv(p)
            except Exception:
                pass
        return None

    def _build_title(params: Dict[str, Any], diff_keys: List[str]) -> str:
        ts = params.get("_ts", "unknown")
        dname = params.get("_dir", "")
        is_ensemble = dname and (dname.endswith("ensemble") or "ensemble" in dname.lower())
        parts = []
        for k in diff_keys:
            v = params.get(k)
            if v is not None:
                parts.append(f"{k}={v}")
        suffix = ("-" + ", ".join(parts)) if parts else ""
        title = f"{ts}{suffix}"
        if is_ensemble: title += " [ensemble]"
        return title

    metric_dfs = []
    all_metric_cols: List[str] = []
    for p in chosen_params:
        dname = p.get("_dir")
        dfm = _load_metrics_df(str(dname)) if dname else None
        metric_dfs.append((p, dfm))
        if dfm is not None:
            try:
                num_cols = [c for c in dfm.columns if dfm[c].dtype.kind in "if" and c]
            except Exception:
                num_cols = list(dfm.columns)
            for c in num_cols:
                if c not in all_metric_cols:
                    all_metric_cols.append(c)

    candidate_x = ["epoch", "step", "iteration", "batch", "index"]
    x_axis = None
    for cand in candidate_x:
        if any((df is not None and cand in df.columns) for _, df in metric_dfs):
            x_axis = cand
            break

    diff_keys = [k for k in sorted({k for p in chosen_params for k in p.keys()}) if k not in ["_ts", "_dir"] and not k.startswith("_")]

    enabled_metrics = [m for m, val in viewer_metric_checks.items() if val]
    if not enabled_metrics:
        st.info("No metrics selected. Check some boxes above to see charts.")
        return

    for col_name in enabled_metrics:
        st.subheader(f"Metric: {col_name}")
        if single_chart:
            combined_df = pd.DataFrame()
            for p, dfm in metric_dfs:
                if dfm is not None and col_name in dfm.columns:
                    title = _build_title(p, diff_keys)
                    temp_df = dfm[[x_axis, col_name]].copy() if x_axis and x_axis in dfm.columns else dfm[[col_name]].copy()
                    if x_axis not in temp_df.columns:
                        temp_df[x_axis or "index"] = range(len(temp_df))
                    temp_df = temp_df.iloc[ignore_head:]
                    temp_df["run"] = title
                    combined_df = pd.concat([combined_df, temp_df], ignore_index=True)
            if not combined_df.empty:
                import plotly.express as px
                fig = px.line(combined_df, x=x_axis or "index", y=col_name, color="run", log_y=log_y)
                st.plotly_chart(fig, use_container_width=True)
        else:
            cols = st.columns(2)
            for i, (p, dfm) in enumerate(metric_dfs):
                if dfm is not None and col_name in dfm.columns:
                    with cols[i % 2]:
                        title = _build_title(p, diff_keys)
                        st.caption(title)
                        plot_df = dfm[[x_axis, col_name]].copy() if x_axis and x_axis in dfm.columns else dfm[[col_name]].copy()
                        if x_axis not in plot_df.columns:
                            plot_df[x_axis or "index"] = range(len(plot_df))
                        plot_df = plot_df.iloc[ignore_head:]
                        plot_lines_func(plot_df, x_axis or "index", height=200)

if __name__ == "__main__":
    raise SystemExit(main())
