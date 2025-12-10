import os
import re
from typing import Any, Dict, List, Tuple

#import pandas as pd  # type: ignore
import streamlit as st  # type: ignore
from pandas import read_csv, DataFrame

from compare_runs_shared import (
    build_run_families,
    build_timestamp_list,
    compute_common_params,
    compute_selected_timestamps,
    group_runs_by_timestamp,
    plot_lines,
    runs_to_dataframe_dicts,
    scan_runs,
    debug,
)


def _find_epoch_files(run_path: str) -> List[Tuple[int, int, str]]:
    """Return ``(set_size, epoch, path)`` triples for epoch CSVs in *run_path*.

    Epoch files follow the pattern ``stock_simulation_<set_size>_epoch_<epoch>.csv``.
    They live directly inside a single run directory such as ``data/TS/``.
    """

    try:
        fnames = os.listdir(run_path)
    except Exception as e:
        print(f"Error in _find_epoch_files listdir for {run_path}: {e}")
        return []

    results: List[Tuple[int, int, str]] = []
    for fname in fnames:
        # Accept both legacy and new naming patterns, e.g.:
        #   stock_simulation_10_epoch_3.csv
        #   stock_simulation_10d_epoch_3.csv
        #   stock_simulation_10p_epoch_3.csv
        # We ignore the optional d/p variant when parsing set_size.
        m = re.match(r"stock_simulation_(\d+)(?:days|percent)?_epoch_(\d+)\.csv$", fname)
        if not m:
            continue
        try:
            set_size = int(m.group(1))
            epoch = int(m.group(2))
        except Exception as e:
            print(f"Error parsing epoch filename {fname}: {e}")
            continue
        full_path = os.path.normpath(os.path.join(run_path, fname))
        results.append((set_size, epoch, full_path))

    # Sort by set_size then epoch for deterministic processing
    results.sort(key=lambda t: (t[0], t[1]))
    return results


def _load_end_csv(run_path: str) -> Dict[int, str]:
    """Locate *non‑epoch* stock_simulation CSVs and return mapping set_size->path.

    These files follow ``stock_simulation_<set_size>.csv``.
    """

    try:
        fnames = os.listdir(run_path)
    except Exception as e:
        print(f"Error in _load_end_csv listdir for {run_path}: {e}")
        return {}

    mapping: Dict[int, str] = {}
    for fname in fnames:
        # Accept both legacy and new naming patterns, e.g.:
        #   stock_simulation_10.csv
        #   stock_simulation_10d.csv
        #   stock_simulation_10p.csv
        # The optional d/p suffix is ignored when deriving set_size.
        m = re.match(r"stock_simulation_(\d+)(?:days|percent)?\.csv$", fname)
        if not m:
            continue
        try:
            set_size = int(m.group(1))
        except Exception as e:
            print(f"Error parsing end csv filename {fname}: {e}")
            continue
        mapping[set_size] = os.path.normpath(os.path.join(run_path, fname))
    return mapping


def _compute_run_statistics_for_path(run_path: str) -> Dict[str, Any]:
    """Compute summary statistics for a single run directory.

    The statistics correspond to the description in the previous task:

    * ``endX`` — ending value from ``stock_simulation_X.csv`` (last row).
    * ``maxX`` / ``max_epochX`` — maximum final value across all
      ``stock_simulation_X_epoch_*.csv`` files and the epoch where that
      maximum occurs.
    * ``first5_maxX`` / ``first5_epochX`` — for each epoch file we look at
      the first five rows, take the maximum value, and then find the best of
      those maxima across all epochs, recording both the value and the epoch.

    ``X`` here is the trading set size (1, 3, 5, ...).  The code is written to
    be generic over ``X`` so it will automatically pick up additional
    ``stock_simulation_*`` files if the training harness is extended later.
    """

    stats: Dict[str, Any] = {}

    # --- End-of-training values from stock_simulation_<set_size>.csv ---------
    #
    # Legacy runs exposed ``portfolio_top<N>`` columns, whereas the newer
    # JSON/SingleRun-based pipeline writes a single ``portfolio_value``
    # column.  To stay compatible we first look for any ``portfolio_top<N>``
    # columns; if none are found we fall back to treating
    # ``portfolio_value`` as the series to summarise.
    print(f"[crsv] computing statistics for run_path={run_path}")
    end_files = _load_end_csv(run_path)
    print(f"[crsv] end_files discovered: {list(end_files.values())}")
    for _set_size, fpath in end_files.items():
        try:
            df = read_csv(fpath)
        except Exception:
            print(f"[crsv] failed to read end CSV {fpath}")
            continue
        if df.empty:
            print(f"[crsv] end CSV empty: {fpath}")
            continue

        # Prefer legacy portfolio_top<N> columns when present.
        top_cols = [
            c
            for c in df.columns
            if isinstance(c, str) and c.startswith("portfolio_top")
        ]

        if top_cols:
            for col in top_cols:
                suffix = col[len("portfolio_top") :]
                try:
                    n = int(suffix)
                except Exception as e:
                    print(f"[crsv] Error parsing end-file column suffix {col}: {e}")
                    continue

                try:
                    end_val = float(df[col].iloc[-1])
                except Exception as e:
                    print(f"[crsv] Error extracting end value for {col} in {_set_size}: {e}")
                    continue

                # If multiple files provide the same N we simply let the last
                # one win; in typical layouts each N appears in a single file.
                stats[f"end{n}"] = end_val
        else:
            # Newer schema: single portfolio_value column.
            if "portfolio_value" in df.columns:
                try:
                    end_val = float(df["portfolio_value"].iloc[-1])
                except Exception as e:
                    print(f"[crsv] Error extracting end portfolio_value: {e}")
                    end_val = None
                if end_val is not None:
                    # Use a fixed index (0) when no explicit N exists so
                    # callers can still rely on end0.
                    stats["end0"] = end_val

    # --- Per‑epoch statistics from stock_simulation_<set_size>_epoch_*.csv ---
    epoch_files = _find_epoch_files(run_path)
    if epoch_files:
        sample = [p for _, _, p in epoch_files[:3]]
        print(f"[crsv] epoch_files discovered: {len(epoch_files)} files. Sample: {sample}")
    else:
        print("[crsv] epoch_files discovered: []")

    if not epoch_files:
        return stats

    # Instead of tying statistics to the single set_size encoded in the
    # filename, scan all ``portfolio_top<N>`` columns in each epoch file so
    # we expose metrics for N = 1, 3, 5, ... as separate columns (max1,
    # max3, max5, etc.).  When no ``portfolio_top<N>`` columns exist (new
    # CSV schema), we fall back to ``portfolio_value`` with an implicit
    # index ``0`` so callers can still reason about max0 / max_epoch0.

    max_val: Dict[int, float] = {}
    max_epoch: Dict[int, int] = {}
    first5_best: Dict[int, float] = {}
    first5_best_epoch: Dict[int, int] = {}

    for _set_size, epoch, fpath in epoch_files:
        try:
            df = read_csv(fpath)
        except Exception as e:
            print(f"[crsv] failed to read epoch CSV {fpath}: {e}")
            continue
        if df.empty:
            print(f"[crsv] epoch CSV empty: {fpath}")
            continue
        # Legacy: one or more portfolio_top<N> columns.
        top_cols = [
            c
            for c in df.columns
            if isinstance(c, str) and c.startswith("portfolio_top")
        ]

        # New schema: fall back to single portfolio_value column.
        if not top_cols and "portfolio_value" in df.columns:
            top_cols = ["portfolio_value"]

        if not top_cols:
            print(f"[crsv] no portfolio columns found in epoch CSV {fpath}, cols={list(df.columns)}")
            continue

        for col in top_cols:
            if col == "portfolio_value":
                n = 0
            else:
                suffix = col[len("portfolio_top") :]
                try:
                    n = int(suffix)
                except Exception as e:
                    print(f"[crsv] Error parsing epoch-file column suffix {col}: {e}")
                    continue

            series = df[col]

            # Final value for this epoch / portfolio_topN (or portfolio_value when n == 0)
            try:
                final_val = float(series.iloc[-1])
            except Exception as e:
                print(f"[crsv] Error extracting final val for {col} in epoch {epoch}: {e}")
                final_val = None

            if final_val is not None:
                prev = max_val.get(n)
                if prev is None or final_val > prev:
                    max_val[n] = final_val
                    max_epoch[n] = epoch

            # Best of the first five days for this epoch / portfolio_topN or portfolio_value
            try:
                head = series.iloc[:5]
                if not head.empty:
                    head_max = float(head.max())
                else:
                    head_max = None
            except Exception as e:
                print(f"[crsv] Error extracting first5 for {col} in epoch {epoch}: {e}")
                head_max = None

            if head_max is not None:
                prev_first5 = first5_best.get(n)
                if prev_first5 is None or head_max > prev_first5:
                    first5_best[n] = head_max
                    first5_best_epoch[n] = epoch

    for n, value in max_val.items():
        stats[f"max{n}"] = value
    for n, epoch in max_epoch.items():
        stats[f"max_epoch{n}"] = epoch
    for n, value in first5_best.items():
        stats[f"first5_max{n}"] = value
    for n, epoch in first5_best_epoch.items():
        stats[f"first5_epoch{n}"] = epoch

    return stats


def _build_statistics_rows(base_dir: str, ts_to_runs: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    """Augment per‑timestamp parameter dicts with computed statistics.

    We treat each top‑level timestamp (``YYYYMMDD_HHMMSS``) as one logical run
    for the purposes of this viewer.  When multiple ``rep*`` runs exist under
    a timestamp, we currently compute statistics based on the *timestamp
    directory itself* ("ensemble") if epoch files live there; otherwise we
    fall back to the first rep directory.
    """

    rows: List[Dict[str, Any]] = []

    for ts_name, run_list in ts_to_runs.items():
        if not run_list:
            continue

        # Representative parameters dictionary: start with the first run's
        # params so that core metadata (learning rate, layers, etc.) appears
        # in the table, then add our derived columns.
        _run_name, params = run_list[0]
        augmented = dict(params)

        # Decide which directory to inspect for statistics.
        # Preference order:
        #   1. Timestamp directory itself (for ensemble epoch files)
        #   2. First rep or run directory underneath the timestamp

        ts_path = os.path.join(base_dir, ts_name)
        # If the run is nested, ts_name alone isn't enough to find the directory.
        # Use the first run's _dir to find the actual timestamp container.
        dname_0 = params.get("_dir")
        if dname_0:
            parent_rel = os.path.dirname(dname_0)
            candidate = os.path.join(base_dir, parent_rel)
            if os.path.isdir(candidate):
                ts_path = candidate

        run_path = ts_path if os.path.isdir(ts_path) else None

        if run_path is None or not _find_epoch_files(run_path):
            # Fall back to the first run's _dir if it is different
            dname = params.get("_dir")
            if dname:
                alt_path = os.path.join(base_dir, dname)
                if os.path.isdir(alt_path):
                    run_path = alt_path

        if run_path is None or not os.path.isdir(run_path):
            debug("No accessible run directory for %s", ts_name)
            rows.append(augmented)
            continue

        stats = _compute_run_statistics_for_path(run_path)
        augmented.update(stats)
        augmented.setdefault("_ts", ts_name)
        rows.append(augmented)

    return rows


def _run_as_script() -> None:
    """Streamlit entry point.

    Usage::

        streamlit run compare_run_statistics_viewer.py
    """

    st.set_page_config(page_title="Run Statistics Viewer", layout="wide")

    default_base = os.path.join(os.getcwd(), "data")
    win_default = r"C:\\Projects\\StockGuidance\\data"
    base_dir = st.sidebar.text_input(
        "Base data directory",
        value=(win_default if os.path.isdir(win_default) else default_base),
    )

    runs = scan_runs(base_dir)
    if not runs:
        st.info(
            "No runs found. Ensure the directory contains timestamped subfolders like YYYYMMDD_HHMMSS."
        )
        return

    ts_to_runs = group_runs_by_timestamp(runs)
    timestamp_names = build_timestamp_list(ts_to_runs)

    # Build run families using the shared helper. We request only families
    # that have *multiple distinct parameter sets*, so families that merely
    # repeat the same configuration across reps are excluded from the
    # Start/End run-family selectors.
    family_to_ts, family_names = build_run_families(runs, require_multiple_param_sets=True)

    st.title("Compare Run Statistics")
    st.caption(
        "Summarise end values and per‑epoch stock simulation statistics for each run, "
        "and visualise how the optimal training epoch varies."
    )

    # ------------------------------------------------------------------
    # Sidebar selection: run family -> underlying builds (timestamps)
    # ------------------------------------------------------------------
    # Provide a small refresh control above the selectors. Pressing this
    # button causes Streamlit to rerun the script, re-executing
    # ``scan_runs(base_dir)`` and repopulating the family/build lists with
    # any newly created timestamp directories.
    st.sidebar.button("Refresh runs list")
    st.sidebar.subheader("Select family and builds")

    # Primary selector: run families derived from ``initial_run``. The
    # "(all families)" option allows cross-family comparisons based purely
    # on build (timestamp) selection.
    family_options = ["(all families)"] + family_names
    fam_key = "crsv_family_sel"
    cur_family = st.session_state.get(fam_key, "(all families)")
    if cur_family not in family_options:
        cur_family = "(all families)"
    family_sel = st.sidebar.selectbox(
        "Run family",
        options=family_options,
        index=family_options.index(cur_family),
        key=fam_key,
    )

    # Determine which timestamped builds are available for the chosen
    # family. When "(all families)" is selected we expose every
    # discovered timestamp.
    if family_sel == "(all families)":
        available_builds = timestamp_names
    else:
        available_builds = family_to_ts.get(family_sel, [])

    # Multiselect of builds (logical single runs) within the chosen
    # family. Persisted across reruns.
    builds_ms_key = "crsv_build_multiselect"
    builds_default = st.session_state.get(builds_ms_key)
    if not builds_default:
        builds_default = list(available_builds)
    builds_default = [b for b in builds_default if b in available_builds]
    selected_builds = st.sidebar.multiselect(
        "Builds (timestamps)",
        options=available_builds,
        default=builds_default,
        key=builds_ms_key,
        help="Select which timestamped single runs to include in the statistics table.",
    )

    selected_builds_set = set(selected_builds or available_builds)

    # Build per‑timestamp statistics rows
    stat_rows = _build_statistics_rows(base_dir, ts_to_runs)
    common = compute_common_params(stat_rows)
    common_keys = sorted(common.keys())

    # Show common parameters for quick inspection
    with st.expander("Common parameters across runs", expanded=False):
        if not common:
            st.caption("No common attributes across discovered runs.")
        else:
            st.json(common)

    # Construct DataFrame using the shared helper so that identifying
    # columns (``run`` / ``run_timestamp``) show up consistently.
    exclude_keys = ["_dir"] + common_keys
    table_rows = runs_to_dataframe_dicts(stat_rows, exclude_keys)
    df = DataFrame(table_rows)

    # Apply the family/build selection from the sidebar to the grid so
    # that only the chosen builds are visible (and included in downloads).
    if selected_builds_set and "run" in df.columns:
        df = df[df["run"].astype(str).isin(selected_builds_set)]

    # Simple substring filter (applied on top of the Start/End range).
    with st.expander("Filter runs", expanded=False):
        search = st.text_input("Substring filter (any column)", key="crsv_search_text")

    fdf = df.copy()
    if search:
        s = str(search).lower()
        mask = fdf.astype(str).agg(" ".join, axis=1).str.lower().str.contains(s)
        fdf = fdf[mask]

    # After filtering, reset the index so that the positional row indices
    # reported by ``st.dataframe`` selection correspond directly to the
    # DataFrame's index labels.  Without this, selecting a row after a
    # filter could yield indices (0..N-1) that no longer match the
    # underlying index (e.g. [5, 7, 9]), causing our lookup via
    # ``grid_df.loc[i, "run"]`` to fail and resulting in no charts being
    # shown for the selected rows.
    fdf = fdf.reset_index(drop=True)

    st.subheader("Run selection and statistics")

    # Column visibility selector so that the new metric columns can be
    # enabled/disabled easily, mirroring compare_runs_viewer.
    all_cols = list(fdf.columns)
    vis_key = "crsv_visible_cols"
    if vis_key in st.session_state:
        vis_default = [c for c in st.session_state[vis_key] if c in all_cols]
        if not vis_default:
            vis_default = all_cols
    else:
        vis_default = all_cols

    visible_cols = st.multiselect(
        "Visible columns",
        options=all_cols,
        default=vis_default,
        key=vis_key,
    )

    # Determine which columns to *display* in the grid. Internally we always
    # keep the full ``fdf`` (including the ``run`` identifier column) so that
    # selection works even if the user hides ``run`` via the Visible columns
    # control.
    grid_cols = [c for c in visible_cols if c in fdf.columns]
    if not grid_cols:
        grid_cols = all_cols

    # Ensure the identifying "run" column is present in the DataFrame passed
    # to ``st.dataframe`` so that we can reliably map selected rows back to
    # timestamp identifiers regardless of the user's column visibility
    # choices.
    if "run" not in grid_cols and "run" in fdf.columns:
        grid_cols = ["run"] + grid_cols

    grid_df = fdf.copy()
    grid_display_df = grid_df[grid_cols]

    dataframe_viewer_return = st.dataframe(
        grid_display_df,
        on_select="rerun",
        width="stretch",
        selection_mode="multi-row",
    )

    # Determine which runs are selected from the grid
    try:
        selection = (dataframe_viewer_return or {}).get("selection") or {}
        selected_rows = selection.get("rows") or []
    except Exception:
        selected_rows = []

    if selected_rows:
        try:
            selected_ts = [
                str(grid_df.loc[i, "run"])
                for i in selected_rows
                if i in grid_df.index and "run" in grid_df.columns
            ]
        except Exception:
            selected_ts = []
    else:
        # Fallback: nothing selected in the grid. Default to all visible
        # runs from the filtered table, already constrained by the
        # family/build selection in the sidebar.
        visible_runs = [str(r) for r in fdf.get("run", [])]
        selected_ts = visible_runs

    # Persist last selection so navigation within the app feels stable
    st.session_state["crsv_last_selection"] = selected_ts

    # Download button for the (filtered) statistics table
    csv_buf = fdf.to_csv(index=False)
    st.download_button(
        "Download statistics CSV",
        data=csv_buf,
        file_name="run_statistics_comparison.csv",
        mime="text/csv",
    )

    # ------------------------------------------------------------------
    # Charts: for each selected timestamp we build two plots:
    #   1. final portfolio value vs epoch
    #   2. best first‑five‑days value vs epoch
    # ------------------------------------------------------------------

    st.subheader("Per‑run epoch statistics")

    # Allow the user to toggle a logarithmic Y‑axis for all charts in this
    # viewer.  The setting is persisted across reruns via session_state so
    # switching selections does not reset the scale preference.
    log_y_default = bool(st.session_state.get("crsv_log_y", False))
    log_y = st.checkbox(
        "Logarithmic Y-axis",
        value=log_y_default,
        key="crsv_log_y",
        help="Apply a log scale to the Y axis for all epoch statistics charts.",
    )

    if not selected_ts:
        st.info("Select one or more runs in the table above to view charts.")
        return

    for ts_name in selected_ts:
        run_list = ts_to_runs.get(ts_name)
        if not run_list:
            continue

        # For a given timestamp we may have an ensemble run (epoch CSVs
        # directly under the timestamp directory) as well as multiple
        # ``rep*`` sub‑runs.  Previously this viewer only plotted a single
        # directory per timestamp, which meant that only one rep's curves
        # were visible even when multiple reps existed.  Here we instead
        # build a list of *all* relevant run directories for the timestamp
        # and render charts for each one.

        # Determine the correct path to the timestamp directory.  For nested runs
        # (inside run_family_...), the timestamp directory is not directly under
        # base_dir, so we derive its location from the first run's _dir.
        ts_path = os.path.join(base_dir, ts_name)
        if run_list:
            dname = run_list[0][1].get("_dir")
            if dname:
                parent_rel = os.path.dirname(dname)
                candidate = os.path.join(base_dir, parent_rel)
                if os.path.isdir(candidate):
                    ts_path = candidate

        run_paths: List[Tuple[str, str]] = []  # (label, path)

        # 1) Optional ensemble-level epoch files directly under the
        #    timestamp directory.  If present, we show them first.
        if os.path.isdir(ts_path) and _find_epoch_files(ts_path):
            run_paths.append(("ensemble", ts_path))

        # 2) Per‑rep sub‑runs (e.g. 20251201_161053/rep1, /rep2, ...).
        #    We derive their paths from the stored ``_dir`` parameter so we
        #    only include reps that belong to this timestamp.
        for _run_name, params in run_list:
            dname = params.get("_dir")
            if not dname:
                continue
            parts = re.split(r'[\\/]', str(dname))
            if len(parts) < 2:
                continue

            rep_name = parts[-1]
            ts_part = parts[-2]

            if ts_part != ts_name:
                continue

            rep_path = str(os.path.join(base_dir, dname))
            if not os.path.isdir(rep_path):
                continue
            if not _find_epoch_files(rep_path):
                continue

            # Avoid duplicates if multiple entries reference the same
            # directory.
            if any(existing_path == rep_path for _label, existing_path in run_paths):
                continue

            run_paths.append((rep_name, rep_path))

        if not run_paths:
            continue

        st.markdown(f"### Run {ts_name}")

        for label, run_path in run_paths:
            epoch_files = _find_epoch_files(run_path)
            if not epoch_files:
                continue

            # Organise per portfolio_top<N> and per epoch.  Newer CSVs expose
            # multiple ``portfolio_top<N>`` columns (typically N = 1, 3, 5,
            # regardless of the numeric suffix in the filename), so we scan all
            # such columns just like ``_compute_run_statistics_for_path`` does.
            by_topn: Dict[int, Dict[int, float]] = {}
            by_topn_first5: Dict[int, Dict[int, float]] = {}

            for _set_size, epoch, fpath in epoch_files:
                try:
                    df = read_csv(fpath)
                except Exception as e:
                    print(f"Error reading csv {fpath}: {e}")
                    continue
                if df.empty:
                    continue

                # Identify columns to plot
                top_cols = [c for c in df.columns if isinstance(c, str) and c.startswith("portfolio_top")]
                if not top_cols and "portfolio_value" in df.columns:
                    top_cols = ["portfolio_value"]

                for col in top_cols:
                    if col == "portfolio_value":
                        n = 0
                    else:
                        suffix = col[len("portfolio_top") :]
                        try:
                            n = int(suffix)
                        except Exception as e:
                            print(f"Error parsing column suffix {col}: {e}")
                            # Ignore columns that don't match the expected pattern
                            # (e.g. portfolio_topX where X is not an int).
                            continue

                    series = df[col]

                    # Final value
                    try:
                        final_val = float(series.iloc[-1])
                    except Exception as e:
                        print(f"Error extracting final value for {col}: {e}")
                        final_val = None

                    if final_val is not None:
                        by_topn.setdefault(n, {})[epoch] = final_val

                    # Best of first five days
                    try:
                        head = series.iloc[:5]
                        if not head.empty:
                            first5_val = float(head.max())
                        else:
                            first5_val = None
                    except Exception as e:
                        print(f"Error extracting first 5 value for {col}: {e}")
                        first5_val = None

                    if first5_val is not None:
                        by_topn_first5.setdefault(n, {})[epoch] = first5_val

            if not by_topn:
                continue

            # Label the subsection so the user can distinguish ensemble vs
            # individual reps.
            st.markdown(f"#### {label}")

            # 1) Final portfolio value vs epoch
            for n, epoch_map in sorted(by_topn.items()):
                if not epoch_map:
                    continue
                epochs = sorted(epoch_map.keys())
                values = [epoch_map[e] for e in epochs]

                label_key = f"final_top{n}" if n != 0 else "final_value"
                plot_df = DataFrame({"epoch": epochs, label_key: values})

                st.markdown(f"**Final {label_key} by epoch**")
                plot_lines(plot_df, "epoch", height=260, y_ref=1000.0, log_y=log_y)

            # 2) Best first‑five‑days value vs epoch
            for n, epoch_map in sorted(by_topn_first5.items()):
                if not epoch_map:
                    continue
                epochs = sorted(epoch_map.keys())
                values = [epoch_map[e] for e in epochs]

                label_key = f"first5_best_top{n}" if n != 0 else "first5_best_value"
                plot_df = DataFrame({"epoch": epochs, label_key: values})

                st.markdown(f"**Best first‑5‑day {label_key} by epoch**")
                plot_lines(plot_df, "epoch", height=260, y_ref=1000.0, log_y=log_y)


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    _run_as_script()
