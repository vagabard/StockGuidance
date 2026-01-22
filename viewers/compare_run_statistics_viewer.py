import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
import re as _re

# Add project root to sys.path to allow imports from sibling packages like 'database'
root_path = Path(__file__).resolve().parent.parent
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

#import pandas as pd  # type: ignore
import streamlit as st  # type: ignore
from pandas import read_csv, DataFrame


from viewers.compare_runs_shared import (
    build_run_families,
    build_timestamp_list,
    compute_common_params,
    compute_selected_timestamps,
    group_runs_by_timestamp,
    plot_lines,
    render_common_params,
    runs_to_dataframe_dicts,
    scan_runs,
    sidebar_family_and_build_selector,
    has_any_simulation_files,
    load_preferences,
    save_preference,
)


def _has_any_simulation_files(path: str) -> bool:
    return has_any_simulation_files(path)


def _discover_best_run_dir(base_dir: str, ts_name: str, run_list: List[Tuple[str, Dict[str, Any]]]) -> str | None:
    """Choose the most appropriate directory to read statistics from for a timestamp.

    Preference:
      1) The timestamp directory itself (ensemble) if it has any stock_simulation files.
      2) Any ``rep*`` (or first run's ``_dir``) under the timestamp that has files.
    Returns normalized absolute path or None if nothing found.
    """

    try:
        base_dir = os.path.normpath(base_dir)
    except Exception:
        pass

    # Start with the obvious timestamp directory
    ts_path = os.path.normpath(os.path.join(base_dir, ts_name))

    # Some pipelines store _dir like "20251215_211039/rep1". Use its parent
    # to refine ts_path if it exists on disk.
    dname_0 = (run_list[0][1] or {}).get("_dir") if run_list else None
    if dname_0:
        parent_rel = os.path.dirname(dname_0)
        candidate = os.path.normpath(os.path.join(base_dir, parent_rel))
        if os.path.isdir(candidate):
            ts_path = candidate

    # 1) Prefer the timestamp directory itself
    if _has_any_simulation_files(ts_path):
        print(f"[crsv] Using timestamp dir for stats: {ts_path}")
        return ts_path

    # 2) Search rep* subdirectories beneath the timestamp directory
    if os.path.isdir(ts_path):
        try:
            for fname in sorted(os.listdir(ts_path)):
                # Rep folders are commonly named rep1, rep2, ... but allow any dir
                sub_path = os.path.join(ts_path, fname)
                if not os.path.isdir(sub_path):
                    continue
                if not re.match(r"rep\d+", fname):
                    # allow non-matching names as well if they contain files
                    pass
                if _has_any_simulation_files(sub_path):
                    print(f"[crsv] Using subdir for stats: {sub_path}")
                    return os.path.normpath(sub_path)
        except Exception as e:
            print(f"[crsv] Error scanning subdirs of {ts_path}: {e}")

    # 3) Try first run's explicit _dir as a last resort
    if dname_0:
        alt_path = os.path.normpath(os.path.join(base_dir, dname_0))
        if _has_any_simulation_files(alt_path):
            print(f"[crsv] Using _dir for stats: {alt_path}")
            return alt_path

    print(f"[crsv] No simulation files for timestamp {ts_name} under base {base_dir}")
    return None


def _discover_all_run_dirs(base_dir: str, ts_name: str, run_list: List[Tuple[str, Dict[str, Any]]]) -> List[Tuple[str, str]]:
    """Return a list of all candidate run directories for a timestamp.

    Each item is a tuple ``(path, label)`` where ``label`` is one of:
      - "ensemble" for the timestamp directory itself
      - the subdirectory name (e.g. "rep1", "rep2", or any folder name)

    The function prefers the timestamp directory (if it contains simulation
    files) and then includes any subdirectories that contain files. If neither
    are present, it will attempt to include the first run's explicit ``_dir``
    if valid.
    """

    results: List[Tuple[str, str]] = []

    try:
        base_dir = os.path.normpath(base_dir)
    except Exception:
        pass

    ts_path = os.path.normpath(os.path.join(base_dir, ts_name))

    # Try to refine timestamp directory using parent of first run's _dir
    dname_0 = (run_list[0][1] or {}).get("_dir") if run_list else None
    if dname_0:
        parent_rel = os.path.dirname(dname_0)
        candidate = os.path.normpath(os.path.join(base_dir, parent_rel))
        if os.path.isdir(candidate):
            ts_path = candidate

    # 1) Timestamp directory as ensemble
    if os.path.isdir(ts_path) and _has_any_simulation_files(ts_path):
        print(f"[crsv] Using timestamp dir for stats: {ts_path}")
        results.append((ts_path, "ensemble"))

    # 2) Subdirectories beneath timestamp directory
    if os.path.isdir(ts_path):
        try:
            for fname in sorted(os.listdir(ts_path)):
                sub_path = os.path.join(ts_path, fname)
                if not os.path.isdir(sub_path):
                    continue
                if _has_any_simulation_files(sub_path):
                    print(f"[crsv] Using subdir for stats: {sub_path}")
                    results.append((os.path.normpath(sub_path), fname))
        except Exception as e:
            print(f"[crsv] Error scanning subdirs of {ts_path}: {e}")

    # 3) Fallback: first run's explicit _dir
    if not results and dname_0:
        alt_path = os.path.normpath(os.path.join(base_dir, dname_0))
        if _has_any_simulation_files(alt_path):
            print(f"[crsv] Using _dir for stats: {alt_path}")
            # Label derived from last component
            results.append((alt_path, os.path.basename(alt_path) or "_dir"))

    if not results:
        print(f"[crsv] No simulation files for timestamp {ts_name} under base {base_dir}")

    return results


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
        #   stock_simulation_ensemble_10d_epoch_3.csv
        # We ignore the optional d/p variant when parsing set_size.
        m = re.match(r"stock_simulation_(?:ensemble_)?(\d+)(?:days|percent)?_epoch_(\d+)\.csv$", fname)
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
        #   stock_simulation_ensemble_10d.csv
        # The optional d/p suffix is ignored when deriving set_size.
        m = re.match(r"stock_simulation_(?:ensemble_)?(\d+)(?:days|percent)?\.csv$", fname)
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
    # Column compatibility matrix:
    #   - Legacy:      portfolio_top<N>
    #   - New single:  portfolio_value
    #   - Recent CSVs: "Portfolio return top<N>" or "Portfolio value top<N>"
    # Handle case/space/underscore variations robustly.
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

        # Find any portfolio-like columns and map them to N indices.
        n_to_col: Dict[int, str] = {}
        # Normalise helper: collapse to lowercase and drop spaces/underscores.
        def _norm(s: str) -> str:
            return s.lower().replace(" ", "").replace("_", "")

        for c in df.columns:
            if not isinstance(c, str):
                continue
            cn = _norm(c)
            # portfolio_value (no N)
            if cn == "portfoliovalue":
                n_to_col[0] = c
                continue
            # portfolio_top<N>
            m = _re.match(r"^portfoliotop(\d+)$", cn)
            if m:
                n_to_col[int(m.group(1))] = c
                continue
            # portfolio(return|value)top<N>
            m = _re.match(r"^portfolio(?:return|value)top(\d+)$", cn)
            if m:
                n_to_col[int(m.group(1))] = c

        if not n_to_col and "portfolio_value" in df.columns:
            n_to_col[0] = "portfolio_value"

        for n, col in n_to_col.items():
            try:
                end_val = float(df[col].iloc[-1])
            except Exception as e:
                print(f"[crsv] Error extracting end value for {col} in {_set_size}: {e}")
                continue
            stats[f"end{n}"] = end_val

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
    # filename, scan all portfolio-like columns in each epoch file so we
    # expose metrics for N = 1, 3, 5, ... as separate columns (max1, max3,
    # max5, etc.). When no N exists (single series), we use index 0.

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
        # Build mapping N -> column name using the same robust detection as above.
        n_to_col: Dict[int, str] = {}
        def _norm(s: str) -> str:
            return s.lower().replace(" ", "").replace("_", "")
        for c in df.columns:
            if not isinstance(c, str):
                continue
            cn = _norm(c)
            if cn == "portfoliovalue":
                n_to_col[0] = c
                continue
            m = _re.match(r"^portfoliotop(\d+)$", cn)
            if m:
                n_to_col[int(m.group(1))] = c
                continue
            m = _re.match(r"^portfolio(?:return|value)top(\d+)$", cn)
            if m:
                n_to_col[int(m.group(1))] = c

        if not n_to_col and "portfolio_value" in df.columns:
            n_to_col[0] = "portfolio_value"

        if not n_to_col:
            print(f"[crsv] no portfolio columns found in epoch CSV {fpath}, cols={list(df.columns)}")
            continue

        for n, col in n_to_col.items():
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

        # Discover all relevant directories (ensemble + reps)
        variants = _discover_all_run_dirs(base_dir, ts_name, run_list)

        if not variants:
           # debug("No accessible run directory for %s", ts_name)
            rows.append(augmented)
            continue

        # Emit a row per variant so the UI can chart ensemble and each rep
        for run_path, label in variants:
            if not os.path.isdir(run_path):
                continue
            stats = _compute_run_statistics_for_path(run_path)
            row = dict(augmented)
            row.update(stats)
            row.setdefault("_ts", ts_name)
            row["_variant"] = label
            # Provide a stable display id distinct per variant
            row["run_id"] = f"{ts_name} [{label}]"
            rows.append(row)

    return rows


def _run_as_script() -> None:
    """Streamlit entry point.

    Usage::

        streamlit run compare_run_statistics_viewer.py
    """

    st.set_page_config(page_title="Run Statistics Viewer", layout="wide")

    default_base = os.path.join(os.getcwd(), "../data")
    win_default = r"C:\\Projects\\StockGuidance\\data"
    base_dir = st.sidebar.text_input(
        "Base data directory",
        value=(win_default if os.path.isdir(win_default) else default_base),
    )

    runs = scan_runs(base_dir)

    # --- Filtering based on "Runs with simulation" checkbox ---
    st.sidebar.subheader("Filter options")

    # Initialise session state from preferences on first load
    prefs = load_preferences()
    if "crsv_sim_only" not in st.session_state:
        st.session_state["crsv_sim_only"] = prefs.get("crsv_sim_only", False)

    sim_only = st.sidebar.checkbox(
        "Runs with simulation",
        value=st.session_state["crsv_sim_only"],
        key="crsv_sim_only",
        on_change=lambda: save_preference("crsv_sim_only", st.session_state["crsv_sim_only"]),
    )

    if sim_only:
        filtered_runs = []
        for rel, params in runs:
            # Check if either the run directory itself or its parent timestamp directory has simulation files
            # rel is typically "timestamp/repN" or just "timestamp"
            run_path = os.path.normpath(os.path.join(base_dir, rel))
            ts_name = params.get("_ts")
            ts_path = os.path.normpath(os.path.join(base_dir, ts_name)) if ts_name else None

            if has_any_simulation_files(run_path) or (ts_path and has_any_simulation_files(ts_path)):
                filtered_runs.append((rel, params))
        runs = filtered_runs

    if not runs:
        st.info(
            "No runs found. Ensure the directory contains timestamped subfolders like YYYYMMDD_HHMMSS."
        )
        return

    ts_to_runs = group_runs_by_timestamp(runs)
    timestamp_names = build_timestamp_list(ts_to_runs)

    # Build run families using the shared helper.
    family_to_ts, family_names = build_run_families(
        runs,
        require_multiple_param_sets=False,
    )

    st.title("Compare Run Statistics")
    st.caption(
        "Summarise end values and per‑epoch stock simulation statistics for each run, "
        "and visualise how the optimal training epoch varies."
    )

    # ------------------------------------------------------------------
    # Sidebar selection: families + builds (shared across viewers)
    # ------------------------------------------------------------------
    st.sidebar.button("Refresh runs list")
    st.sidebar.subheader("Select families and builds")

    def update_list_of_runs_selected(selected_runs: List[str]) -> None:
        st.session_state["crsv_runs_selected"] = list(selected_runs)

    selected_builds, selected_families, _available_builds = sidebar_family_and_build_selector(
        timestamp_names=timestamp_names,
        family_names=family_names,
        family_to_ts=family_to_ts,
        key_prefix="crsv",
        # Keep the viewer light on initial load: require the user to pick a
        # family before we build the large statistics table and charts.
        default_select_all_builds=False,
        empty_builds_means_all=False,
        on_runs_updated=update_list_of_runs_selected,
    )

    # Match the behavior of the other runs viewer: do not populate the grid
    # or draw charts until at least one run family has been selected.
    if not selected_families:
        st.info("Select one or more run families in the sidebar to begin.")
        return
    selected_builds_set = set(selected_builds)

    # Build per‑timestamp statistics rows
    stat_rows = _build_statistics_rows(base_dir, ts_to_runs)
    common = compute_common_params(stat_rows)
    common_keys = sorted(common.keys())

    # Shared common-params display
    render_common_params(common, title="Common parameters across runs", expanded=False)

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
        if "crsv_search_text" not in st.session_state:
            st.session_state["crsv_search_text"] = prefs.get("crsv_search_text", "")
        search = st.text_input(
            "Substring filter (any column)",
            value=st.session_state["crsv_search_text"],
            key="crsv_search_text",
            on_change=lambda: save_preference("crsv_search_text", st.session_state["crsv_search_text"]),
        )

    # Hide rep rows by default in the main table; keep them available below
    # in a separate expander. We still compute charts for all variants.
    fdf = df.copy()
    if "_variant" in fdf.columns:
        # Prefer ensemble rows if present, else leave as-is.
        has_ensemble = (fdf["_variant"].astype(str) == "ensemble").any()
        if has_ensemble:
            fdf = fdf[fdf["_variant"].astype(str) == "ensemble"]
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
    if vis_key not in st.session_state:
        st.session_state[vis_key] = prefs.get(vis_key, all_cols)

    vis_default = [c for c in st.session_state[vis_key] if c in all_cols]
    if not vis_default:
        vis_default = all_cols

    visible_cols = st.multiselect(
        "Visible columns",
        options=all_cols,
        default=vis_default,
        key=vis_key,
        on_change=lambda: save_preference(vis_key, st.session_state[vis_key]),
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

    # Provide a separate view for rep rows so the user can expand to them
    # without cluttering the main table.
    if "_variant" in df.columns:
        rep_df = df[df["_variant"].astype(str) != "ensemble"].copy()
        if not rep_df.empty:
            with st.expander("Show rep rows (read-only)", expanded=False):
                st.dataframe(rep_df.reset_index(drop=True), use_container_width=True)

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

    # Checkboxes for graph visibility
    st.markdown("##### Graph visibility")
    col1, col2, col3 = st.columns(3)

    if "crsv_show_final" not in st.session_state:
        st.session_state["crsv_show_final"] = prefs.get("crsv_show_final", True)
    if "crsv_show_first5" not in st.session_state:
        st.session_state["crsv_show_first5"] = prefs.get("crsv_show_first5", True)
    if "crsv_show_avgcdf" not in st.session_state:
        st.session_state["crsv_show_avgcdf"] = prefs.get("crsv_show_avgcdf", True)

    with col1:
        show_final = st.checkbox(
            "Final value",
            value=st.session_state["crsv_show_final"],
            key="crsv_show_final",
            on_change=lambda: save_preference("crsv_show_final", st.session_state["crsv_show_final"]),
        )
    with col2:
        show_first5 = st.checkbox(
            "Best first 5 days",
            value=st.session_state["crsv_show_first5"],
            key="crsv_show_first5",
            on_change=lambda: save_preference("crsv_show_first5", st.session_state["crsv_show_first5"]),
        )
    with col3:
        show_avgcdf = st.checkbox(
            "Average CDF",
            value=st.session_state["crsv_show_avgcdf"],
            key="crsv_show_avgcdf",
            on_change=lambda: save_preference("crsv_show_avgcdf", st.session_state["crsv_show_avgcdf"]),
        )

    # Toggle for grouping mode
    grouping_key = "crsv_chart_grouping"
    grouping_options = [
        "Per run (overlay N lines)",
        "Across runs (overlay runs)",
    ]
    if grouping_key not in st.session_state:
        st.session_state[grouping_key] = prefs.get(grouping_key, grouping_options[0])

    grouping_mode = st.radio(
        "Chart grouping",
        options=grouping_options,
        index=grouping_options.index(st.session_state[grouping_key]) if st.session_state[grouping_key] in grouping_options else 0,
        key=grouping_key,
        help=(
            "Per run: for each run/variant, show one chart per metric with N=1/3/5 overlays.\n"
            "Across runs: for each metric+N, overlay all selected runs (ensemble and reps) on one chart."
        ),
        on_change=lambda: save_preference(grouping_key, st.session_state[grouping_key]),
    )

    # Allow the user to toggle a logarithmic Y‑axis for all charts in this
    # viewer.  The setting is persisted across reruns via session_state so
    # switching selections does not reset the scale preference.
    if "crsv_log_y" not in st.session_state:
        st.session_state["crsv_log_y"] = prefs.get("crsv_log_y", False)

    log_y = st.checkbox(
        "Logarithmic Y-axis",
        value=st.session_state["crsv_log_y"],
        key="crsv_log_y",
        help="Apply a log scale to the Y axis for all epoch statistics charts.",
        on_change=lambda: save_preference("crsv_log_y", st.session_state["crsv_log_y"]),
    )

    if not selected_ts:
        st.info("Select one or more runs in the table above to view charts.")
        return

    # Collectors used for the "Across runs" grouping mode
    acc_final: Dict[int, Dict[str, Dict[int, float]]] = {}
    acc_first5: Dict[int, Dict[str, Dict[int, float]]] = {}
    acc_avgcdf: Dict[int, Dict[str, Dict[int, float]]] = {}

    for ts_name in selected_ts:
        run_list = ts_to_runs.get(ts_name)
        if not run_list:
            continue

        # Determine timestamp directory path (may be nested under run_family)
        ts_path = os.path.join(base_dir, ts_name)
        if run_list:
            dname = run_list[0][1].get("_dir")
            if dname:
                parent_rel = os.path.dirname(dname)
                candidate = os.path.join(base_dir, parent_rel)
                if os.path.isdir(candidate):
                    ts_path = candidate

        run_paths: List[Tuple[str, str]] = []  # (label, path)

        # Ensemble at timestamp level
        if os.path.isdir(ts_path) and _find_epoch_files(ts_path):
            run_paths.append(("ensemble", ts_path))

        # Rep sub-runs
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
            if any(existing_path == rep_path for _label, existing_path in run_paths):
                continue
            run_paths.append((rep_name, rep_path))

        if not run_paths:
            continue

        if grouping_mode == "Per run (overlay N lines)":
            st.markdown(f"### Run {ts_name}")

        for label, run_path in run_paths:
            epoch_files = _find_epoch_files(run_path)

            # per-run collections
            by_topn: Dict[int, Dict[int, float]] = {}
            by_topn_first5: Dict[int, Dict[int, float]] = {}
            by_topn_avgcdf: Dict[int, Dict[int, float]] = {}

            for _set_size, epoch, fpath in (epoch_files or []):
                try:
                    df_ep = read_csv(fpath)
                except Exception as e:
                    print(f"Error reading csv {fpath}: {e}")
                    continue
                if df_ep.empty:
                    continue

                def _norm(s: str) -> str:
                    return s.lower().replace(" ", "").replace("_", "")

                n_to_col: Dict[int, str] = {}
                for c in df_ep.columns:
                    if not isinstance(c, str):
                        continue
                    cn = _norm(c)
                    if cn == "portfoliovalue":
                        n_to_col[0] = c
                        continue
                    m1 = _re.match(r"^portfoliotop(\d+)$", cn)
                    if m1:
                        n_to_col[int(m1.group(1))] = c
                        continue
                    m2 = _re.match(r"^portfolio(?:return|value)top(\d+)$", cn)
                    if m2:
                        n_to_col[int(m2.group(1))] = c

                if not n_to_col and "portfolio_value" in df_ep.columns:
                    n_to_col[0] = "portfolio_value"

                for n, col in n_to_col.items():
                    series = df_ep[col]

                    # Final value (last row)
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
                        first5_val = float(head.max()) if not head.empty else None
                    except Exception as e:
                        print(f"Error extracting first 5 value for {col}: {e}")
                        first5_val = None
                    if first5_val is not None:
                        by_topn_first5.setdefault(n, {})[epoch] = first5_val

                    # Average CDF per epoch (mean of the series values)
                    try:
                        avgcdf_val = float(series.mean()) if len(series) > 0 else None
                    except Exception as e:
                        print(f"Error computing avg CDF for {col}: {e}")
                        avgcdf_val = None
                    if avgcdf_val is not None:
                        by_topn_avgcdf.setdefault(n, {})[epoch] = avgcdf_val

            # Placeholders if nothing collected
            used_placeholder = False
            if not by_topn and not by_topn_first5 and not by_topn_avgcdf:
                used_placeholder = True
                print(f"[crsv] Using placeholder data for chart: ts={ts_name}, label={label}")
                placeholder_epochs = [0, 1, 2, 3, 4]
                base = 1000.0
                for n, step in [(1, 2.0), (3, 1.2), (5, 0.8)]:
                    vals = {e: base + step * e + (0.0 if e != 3 else -1.0) for e in placeholder_epochs}
                    by_topn[n] = vals.copy()
                    by_topn_first5[n] = {e: base + step * min(e, 4) for e in placeholder_epochs}
                    by_topn_avgcdf[n] = {e: base + step * 0.5 * e for e in placeholder_epochs}

            series_label = f"{ts_name} [{label}]"

            if grouping_mode == "Per run (overlay N lines)":
                # Section label per variant
                st.markdown(f"#### {label}")

                # Final by epoch (overlay N)
                if show_final and by_topn:
                    # Build a unified epoch set
                    all_epochs = sorted({e for m in by_topn.values() for e in m.keys()})
                    plot_df = DataFrame({"epoch": all_epochs})
                    for n, epoch_map in sorted(by_topn.items()):
                        plot_df[f"final_top{n}" if n != 0 else "final_value"] = [epoch_map.get(e) for e in all_epochs]
                    # Badge for placeholder data
                    if used_placeholder:
                        st.markdown("**Final value by epoch** — `FAKE`")
                        try:
                            import plotly.io as pio  # type: ignore
                        except Exception:
                            pass
                    else:
                        st.markdown("**Final value by epoch**")
                    plot_lines(
                        plot_df,
                        "epoch",
                        height=260,
                        y_ref=1000.0,
                        log_y=log_y,
                        watermark_text=("FAKE" if used_placeholder else None),
                        key=f"crsv_perrun_final__{ts_name}__{label}",
                    )

                # First5 by epoch (overlay N)
                if show_first5 and by_topn_first5:
                    all_epochs = sorted({e for m in by_topn_first5.values() for e in m.keys()})
                    plot_df = DataFrame({"epoch": all_epochs})
                    for n, epoch_map in sorted(by_topn_first5.items()):
                        plot_df[f"first5_best_top{n}" if n != 0 else "first5_best_value"] = [epoch_map.get(e) for e in all_epochs]
                    if used_placeholder:
                        st.markdown("**Best first‑5‑day value by epoch** — `FAKE`")
                    else:
                        st.markdown("**Best first‑5‑day value by epoch**")
                    plot_lines(
                        plot_df,
                        "epoch",
                        height=260,
                        y_ref=1000.0,
                        log_y=log_y,
                        watermark_text=("FAKE" if used_placeholder else None),
                        key=f"crsv_perrun_first5__{ts_name}__{label}",
                    )

                # Avg CDF by epoch (overlay N)
                if show_avgcdf and by_topn_avgcdf:
                    all_epochs = sorted({e for m in by_topn_avgcdf.values() for e in m.keys()})
                    plot_df = DataFrame({"epoch": all_epochs})
                    for n, epoch_map in sorted(by_topn_avgcdf.items()):
                        plot_df[f"avg_cdf_top{n}" if n != 0 else "avg_cdf_value"] = [epoch_map.get(e) for e in all_epochs]
                    if used_placeholder:
                        st.markdown("**Average CDF by epoch** — `FAKE`")
                    else:
                        st.markdown("**Average CDF by epoch**")
                    plot_lines(
                        plot_df,
                        "epoch",
                        height=260,
                        y_ref=1000.0,
                        log_y=log_y,
                        watermark_text=("FAKE" if used_placeholder else None),
                        key=f"crsv_perrun_avgcdf__{ts_name}__{label}",
                    )

            else:
                # Accumulate for Across-runs mode
                for n, epoch_map in by_topn.items():
                    acc_final.setdefault(n, {})[series_label] = dict(epoch_map)
                for n, epoch_map in by_topn_first5.items():
                    acc_first5.setdefault(n, {})[series_label] = dict(epoch_map)
                for n, epoch_map in by_topn_avgcdf.items():
                    acc_avgcdf.setdefault(n, {})[series_label] = dict(epoch_map)

    # Render Across-runs charts if that mode is selected
    if grouping_mode == "Across runs (overlay runs)":
        # Final
        if show_final:
            for n in sorted(acc_final.keys()):
                series_dict = acc_final[n]
                # Union of all epochs across runs
                all_epochs = sorted({e for m in series_dict.values() for e in m.keys()})
                plot_df = DataFrame({"epoch": all_epochs})
                for series_label, epoch_map in sorted(series_dict.items()):
                    plot_df[series_label] = [epoch_map.get(e) for e in all_epochs]
                title = f"Final value by epoch — top{n}" if n != 0 else "Final value by epoch"
                st.markdown(f"**{title}**")
                plot_lines(plot_df, "epoch", height=300, y_ref=1000.0, log_y=log_y, key=f"crsv_across_final__top{n}")

        # First5
        if show_first5:
            for n in sorted(acc_first5.keys()):
                series_dict = acc_first5[n]
                all_epochs = sorted({e for m in series_dict.values() for e in m.keys()})
                plot_df = DataFrame({"epoch": all_epochs})
                for series_label, epoch_map in sorted(series_dict.items()):
                    plot_df[series_label] = [epoch_map.get(e) for e in all_epochs]
                title = f"Best first‑5‑day by epoch — top{n}" if n != 0 else "Best first‑5‑day by epoch"
                st.markdown(f"**{title}**")
                plot_lines(plot_df, "epoch", height=300, y_ref=1000.0, log_y=log_y, key=f"crsv_across_first5__top{n}")

        # Avg CDF
        if show_avgcdf:
            for n in sorted(acc_avgcdf.keys()):
                series_dict = acc_avgcdf[n]
                all_epochs = sorted({e for m in series_dict.values() for e in m.keys()})
                plot_df = DataFrame({"epoch": all_epochs})
                for series_label, epoch_map in sorted(series_dict.items()):
                    plot_df[series_label] = [epoch_map.get(e) for e in all_epochs]
                title = f"Average CDF by epoch — top{n}" if n != 0 else "Average CDF by epoch"
                st.markdown(f"**{title}**")
                plot_lines(plot_df, "epoch", height=300, y_ref=1000.0, log_y=log_y, key=f"crsv_across_avgcdf__top{n}")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    _run_as_script()
