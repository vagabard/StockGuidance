import io
import json
import os
import re
import traceback
from math import ceil, sqrt
from typing import Dict, List, Any, Tuple

try:
	import streamlit as st
	import pandas as pd
	import numpy as np
except ImportError:
	print("This viewer requires Streamlit, pandas, and numpy.")
	st = None
	pd = None
	np = None

from compare_runs_shared import (
	build_run_families,
	compute_selected_timestamps,
	group_runs_by_timestamp,
	scan_runs,
)
from db_connect import get_connection

from compare_runs_shared import scan_runs  # type: ignore[redefined-builtin]


@st.cache_data
def _fetch_market_deltas(min_date_id: int, max_date_id: int) -> Dict[int, List[float]]:
	"""Fetch per-date market returns using daily_delta_stock_data.change_in_price_over_day.

	Returns
	-------
	Dict[int, List[float]]
		Mapping of ``date_id -> list of change_in_price_over_day`` values.
	"""
	conn = None
	try:
		print(
			f"[_fetch_market_deltas] Requesting range min_date_id={min_date_id}, max_date_id={max_date_id}"
		)
		conn = get_connection(user="Redbeard", password="3YZs#z5GOJKdea")
		cur = conn.cursor()
		query = """
			SELECT ddsd.date_id,
			       ddsd.change_in_price_over_day
			FROM daily_delta_stock_data ddsd
			WHERE ddsd.date_id BETWEEN %s AND %s
			  AND ddsd.change_in_price_over_day IS NOT NULL
		"""
		params = (min_date_id, max_date_id)
		print("[_fetch_market_deltas] Executing SQL:\n" + query.strip())
		print(f"[_fetch_market_deltas] Params: {params}")
		cur.execute(query, params)
		rows = cur.fetchall()
		print(
			f"[_fetch_market_deltas] Raw rows fetched: {len(rows)}; sample={rows[:5]}"
		)
		results: Dict[int, List[float]] = {}
		for row in rows:
			try:
				did = int(row[0])
				val = float(row[1])
			except Exception:
				continue
			if did not in results:
				results[did] = []
			results[did].append(val)
		print(
			f"[_fetch_market_deltas] Fetched {len(results)} dates from DB; sample_keys={sorted(list(results.keys()))[:5]}"
		)
		# If the DB returned nothing (e.g., missing rows), fall back to a small
		# deterministic synthetic distribution so the CDF lines still render for
		# debugging. This keeps the secondary axis from disappearing entirely.
		if len(results) == 0:
			print("[_fetch_market_deltas] DB returned no rows; using synthetic fallback data")
			if max_date_id < min_date_id:
				min_date_id, max_date_id = max_date_id, min_date_id
			for did in range(int(min_date_id), int(max_date_id) + 1):
				base = ((did % 10) - 5) / 100.0
				results[did] = [base - 0.02, base - 0.01, base, base + 0.01, base + 0.02]
		return results
	except Exception as e:
		print(f"[_fetch_market_deltas] Error fetching market data: {e}")
		traceback.print_exc()
		return {}
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _fetch_rank_ratios(date_ticker_pairs: List[Tuple[int, int]]) -> Dict[Tuple[int, int], float]:
	"""Return rank_ratio for each (date_id, ticker_id) using change_in_price_over_day.

	For each target pair, rank_ratio = (# market rows with change < target) / (total rows for that date).
	"""
	if not date_ticker_pairs:
		return {}

	conn = None
	try:
		conn = get_connection(user="Redbeard", password="3YZs#z5GOJKdea")
		cur = conn.cursor()

		# Build VALUES list for the target pairs
		values_clause = ",".join([f"ROW({date_id},{ticker_id})" for (date_id,ticker_id) in date_ticker_pairs])
		params: List[Any] = []



		for d, t in date_ticker_pairs:
			params.extend([int(d), int(t)])

		query = f"""
WITH target AS (
    -- **MySQL 8.0.19+ Compatible Syntax for Ad-Hoc Table**
    -- Using ROW() to define the pairs, which is clean and efficient.
    SELECT date_id, ticker_id FROM (
        -- Replace this with your actual, prepared (date_id, ticker_id) pairs
        VALUES
            {values_clause}
    ) AS tmp(date_id, ticker_id)
),
target_ret AS (
    -- Get the specific return (target_ret) for the selected pairs
    SELECT
        t.date_id,
        t.ticker_id,
        d.change_in_price_over_day AS target_ret
    FROM target t
    JOIN daily_delta_stock_data d
        ON d.date_id = t.date_id AND d.ticker_id = t.ticker_id
    WHERE d.change_in_price_over_day IS NOT NULL
)
SELECT
    tr.date_id,
    tr.ticker_id,
    -- 1. Conditional Count: Count how many stocks performed worse than target_ret
    SUM(CASE WHEN d2.change_in_price_over_day < tr.target_ret THEN 1 ELSE 0 END) AS worse_count,

    -- 2. Total Count: Count total stocks on that date
    COUNT(d2.ticker_id) AS total_count,

    -- 3. The final ratio (Note: Use floating point numbers for division)
    SUM(CASE WHEN d2.change_in_price_over_day < tr.target_ret THEN 1.0 ELSE 0.0 END) / COUNT(d2.ticker_id) AS rank_ratio
FROM target_ret tr
JOIN daily_delta_stock_data d2
    ON d2.date_id = tr.date_id -- Join to ALL stocks for that date
WHERE d2.change_in_price_over_day IS NOT NULL
-- GROUP BY is correct for strict mode (ONLY_FULL_GROUP_BY)
GROUP BY
    tr.date_id,
    tr.ticker_id,
    tr.target_ret;		"""

#		print("Sql query is ", query, " with params:")
#		print("[_fetch_rank_ratios] Executing SQL with pairs=", params)
		cur.execute(query  )  #, params)
		rows = cur.fetchall()
		print(f"[_fetch_rank_ratios] fetched {len(rows)} rows; sample={rows[:5]}")

		out: Dict[Tuple[int, int], float] = {}
		for row in rows:
			try:
				did = int(row[0]); tid = int(row[1]); ratio = float(row[4])
				out[(did, tid)] = ratio
			except Exception:
				continue
		return out
	except Exception as e:
		print(f"[_fetch_rank_ratios] Error: {e}")
		traceback.print_exc()
		return {}
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _compute_common_params(selected: List[Dict[str, Any]]) -> Dict[str, Any]:
	if not selected:
		return {}
	common: Dict[str, Any] = {}
	# Intersect keys
	keys = set(selected[0].keys())
	for r in selected[1:]:
		keys &= set(r.keys())
	for k in sorted(keys):
		# Skip internal/meta keys
		if k.startswith("_"):
			continue
		vals = {json.dumps(selected[i][k], sort_keys=True, default=str) for i in range(len(selected))}
		if len(vals) == 1:
			# Re-hydrate original value from the first run
			common[k] = selected[0][k]
	return common


def _to_dataframe_dicts(runs: List[Dict[str, Any]], exclude_keys: List[str]) -> List[Dict[str, Any]]:
	rows: List[Dict[str, Any]] = []
	for r in runs:
		row: Dict[str, Any] = {k: v for k, v in r.items() if k not in exclude_keys}
		# Always include identifying columns at front
		# Prefer the logical timestamp identifier ("_ts") when present so
		# that comparison tables and selectors operate at the timestamp level
		# rather than on individual rep subdirectories.
		row = {
			"run": r.get("_ts") or r.get("_dir"),
			"run_timestamp": r.get("run_timestamp"),
		} | row
		rows.append(row)
	return rows


def _run_as_script():
	"""Streamlit app entrypoint.

	Usage:
		streamlit run compare_runs_viewer.py
	"""
	if st is None:
		print("This viewer requires Streamlit and pandas.")
		print("Install with: pip install streamlit pandas")
		return

	st.set_page_config(page_title="Training Runs Viewer", layout="wide")

	# Sidebar: base directory
	default_base = os.path.join(os.getcwd(), "data")
	# Prefer project default path if running elsewhere
	win_default = r"C:\\Projects\\StockGuidance\\data"
	base_dir = st.sidebar.text_input("Base data directory", value=(win_default if os.path.isdir(win_default) else default_base))

	runs = scan_runs(base_dir)

	# Group discovered runs by their timestamp directory so that the UI can
	# present a single entry per timestamp (e.g. "20251124_031117") even when
	# multiple rep* sub‑runs exist underneath it. Charts and stock simulation
	# views can still fan out to all reps within a selected timestamp.
	ts_to_runs = group_runs_by_timestamp(runs)

	# Use timestamp keys sorted so that the most recent runs appear first.
	# Since the directories are named using a ``YYYYMMDD_HHMMSS`` pattern,
	# simple reverse lexicographical ordering corresponds to descending
	# chronological order.
	timestamp_names: List[str] = sorted(ts_to_runs.keys(), reverse=True)

	# Derive "run families" based on the initial_run identifier so that
	# compound runs (and harness-driven runs) can be grouped together.
	# We only expose families that contain *multiple distinct parameter
	# sets*; families whose only multiplicity comes from rep sub‑runs with
	# identical parameters are filtered out.
	family_to_ts, family_names = build_run_families(runs, require_multiple_param_sets=True)

	st.title("Compare Training Runs")
	st.caption("Scan a data folder of timestamped run directories and compare run parameters.")

	# --- Persistent viewer/session state ---
	if "viewer_metric_checks" not in st.session_state:
		st.session_state["viewer_metric_checks"] = {}  # column -> bool
	# Persist sidebar Start/End selections across reruns
	if "start_run_sel" not in st.session_state:
		st.session_state["start_run_sel"] = "(none)"
	if "end_run_sel" not in st.session_state:
		st.session_state["end_run_sel"] = "(none)"
	# Persist Start/End run-family selections across reruns
	if "start_family_sel" not in st.session_state:
		st.session_state["start_family_sel"] = "(none)"
	if "end_family_sel" not in st.session_state:
		st.session_state["end_family_sel"] = "(none)"
	# Track the previous Start/End selections so we can detect when the
	# user changes the range and recompute the multiselect accordingly.
	if "last_start_run_sel" not in st.session_state:
		st.session_state["last_start_run_sel"] = st.session_state["start_run_sel"]
	if "last_end_run_sel" not in st.session_state:
		st.session_state["last_end_run_sel"] = st.session_state["end_run_sel"]
	# Likewise track the last run-family selections so that changing the
	# family range also recomputes the sidebar multiselect.
	if "last_start_family_sel" not in st.session_state:
		st.session_state["last_start_family_sel"] = st.session_state["start_family_sel"]
	if "last_end_family_sel" not in st.session_state:
		st.session_state["last_end_family_sel"] = st.session_state["end_family_sel"]

	if not runs:
		st.info("No runs found. Ensure the directory contains subfolders like YYYYMMDD_HHMMSS with a runParameters.txt inside.")
		return

	# ------------------------------------------------------------------
	# Sidebar selection: run family -> underlying builds (timestamps)
	# ------------------------------------------------------------------
	# Provide a lightweight way to rescan the base directory for new
	# run families without restarting the Streamlit app. Clicking this
	# button triggers a rerun and therefore re-runs ``scan_runs``.
	st.sidebar.button("Refresh runs list")
	st.sidebar.subheader("Select family and builds")

	# Primary selector: run families derived from ``initial_run``.
	# We add an "(all families)" option so users can still compare
	# builds across families when desired.
	family_options = ["(all families)"] + family_names
	fam_key = "crv_family_sel"
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
	# discovered timestamp so cross-family comparisons remain possible.
	if family_sel == "(all families)":
		available_builds = timestamp_names
	else:
		available_builds = family_to_ts.get(family_sel, [])

	# Multiselect of builds (logical single runs) within the chosen
	# family. Persisted across reruns.
	sidebar_ms_key = "crv_build_multiselect"
	ms_default = st.session_state.get(sidebar_ms_key)
	if not ms_default:
		# Default to no preselected builds to avoid heavy initial rendering.
		ms_default = []
	# Ensure defaults only contain still-valid options
	ms_default = [m for m in ms_default if m in available_builds]
	multi = st.sidebar.multiselect(
		"Builds (timestamps)",
		options=available_builds,
		default=ms_default,
		key=sidebar_ms_key,
		help="Select which timestamped single runs to compare for the chosen family.",
	)

	selected_set = set(multi)

	# Allow empty selection: we'll still render the grid to let users pick rows there
	if not selected_set:
		st.info("Tip: Select rows in the grid below or use the sidebar pickers.")

	# Prepare selected timestamp "run" entries for the comparison table.
	# For each selected timestamp, we collapse its underlying reps to their
	# common parameters so that the table shows one logical row per
	# timestamp directory while charts can still expand to all reps.
	selected_params: List[Dict[str, Any]] = []
	for ts_name in timestamp_names:
		if ts_name not in selected_set:
			continue

		run_list = ts_to_runs.get(ts_name, [])
		if not run_list:
			continue

		run_params = [p for _, p in run_list]
		common = _compute_common_params(run_params)
		# Record the logical timestamp identifier and a representative
		# directory path for downstream consumers.
		row_params: Dict[str, Any] = dict(common)
		row_params["_ts"] = ts_name
		# Keep a representative _dir so existing logic that expects it as a
		# filesystem path (e.g. for metrics/CSV loading) can still function
		# when operating on a single run.
		row_params.setdefault("_dir", run_list[0][0])
		selected_params.append(row_params)

	# Compute common vs varying
	common = _compute_common_params(selected_params)
	common_keys = set(common.keys())

	with st.expander("Common attributes (read-only)", expanded=False):
		if not common:
			st.caption("No common attributes across all selected runs.")
		else:
			st.json(common)

	st.subheader("Run parameters (differences)")

	# Build table from non-common + identifiers
	# try:
	# 	import pandas as pd  # type: ignore
	# except Exception:
	# 	st.error("pandas is required to render tables. Install with: pip install pandas")
	# 	return

	exclude = ["_dir", *common_keys]
	rows = _to_dataframe_dicts(selected_params, exclude)
	df = pd.DataFrame(rows)

	# Column selection
	all_cols = list(df.columns)
	default_cols = [c for c in ["run"] if c in all_cols]
	other_cols = [c for c in all_cols if c not in default_cols]
	# Persist column visibility preferences across reruns.
	vis_key = "crv_visible_cols"
	if vis_key in st.session_state:
		vis_default = [c for c in st.session_state[vis_key] if c in all_cols]
		if not vis_default:
			vis_default = default_cols + other_cols
	else:
		vis_default = default_cols + other_cols
	visible_cols = st.multiselect(
		"Visible columns",
		options=all_cols,
		default=vis_default,
		key=vis_key,
	)

	# Simple filtering
	with st.expander("Filters"):
		col1, col2, col3 = st.columns(3)
		# Persist simple filter/sort controls across reruns.
		search = col1.text_input(
			"Substring filter (any column)",
			key="crv_search_text",
		)

	fdf = df.copy()
	if search:
		s = str(search).lower()
		fdf = fdf[[s in str(v).lower() for v in fdf.astype(str).agg(" ".join, axis=1)]]


	# --- Simplified grid display using dataframe (read-only) ---
	# import streamlit as st  # type: ignore

	# For now, we show the comparison table via st.dataframe without in-grid
	# checkboxes. Run selection for the charts remains driven by the sidebar
	# controls (selected_set).

	grid_df = fdf.copy()
	# Ensure identifying column exists for clarity
	if "run" not in grid_df.columns:
		grid_df.insert(0, "run", df.get("run", list(range(len(grid_df)))))

	grid_cols = [c for c in visible_cols if c in grid_df.columns]
	if "run" not in grid_cols and "run" in grid_df.columns:
		grid_cols.insert(0, "run")

	dataframe_viewer_return = st.dataframe(
		grid_df[grid_cols],
		on_select="rerun",
		width="stretch",
		selection_mode="multi-row",
	)

	# Debug helper: log which rows (by original table index) are currently
	# selected in the Streamlit dataframe widget, along with their
	# corresponding run IDs.  Also detect when the selection *changes* across
	# reruns so we can more easily debug interaction issues.
	try:
		selection = (dataframe_viewer_return or {}).get("selection") or {}
		selected_rows = selection.get("rows") or []
	except Exception:
		selection = {}
		selected_rows = []

	# Map currently selected row indices to run IDs (if column available).
	if selected_rows:
		# The indices in "rows" are based on the original entries passed to
		# st.dataframe (i.e., the index of grid_df[grid_cols]). Use .loc so we
		# respect the index labels even if they are not 0..N.
		try:
			if "run" in grid_df.columns:
				selected_runs = [
					grid_df.loc[i, "run"]
					for i in selected_rows
					if i in grid_df.index
				]
			else:
				selected_runs = []
		except Exception:
			selected_runs = []
	else:
		selected_runs = []

	# --- Selection change detection --------------------------------------
	#
	# Streamlit reruns the script on every interaction. To notice when the
	# grid selection actually changes, we keep the last seen selection in
	# session_state and compare it with the current one.
	ss_key_rows = "crv_last_grid_rows"
	ss_key_runs = "crv_last_grid_runs"

	prev_rows = list(st.session_state.get(ss_key_rows, []))  # type: ignore[index]
	prev_runs = list(st.session_state.get(ss_key_runs, []))  # type: ignore[index]

	# Normalise to lists of simple, JSON‑serialisable values so comparisons
	# are stable across reruns.
	curr_rows = list(selected_rows)
	curr_runs = [str(r) for r in selected_runs]

	if curr_rows != prev_rows or curr_runs != prev_runs:
		# Persist the new selection for the next rerun *before* logging.
		st.session_state[ss_key_rows] = curr_rows
		st.session_state[ss_key_runs] = curr_runs


		# Whenever the grid selection changes, update the set of runs that the
		# metrics viewer at the bottom should focus on.  We do this via
		# session_state so that the subsequent rerun can pick up the new
		# selection when computing runs_for_view.
		#
		# Note: runs_for_view is built from this key near the end of
		# _run_as_script using:
		#   persisted = st.session_state.get("last_grid_selection", [])
		# so keeping this value in sync here is enough to drive which charts
		# are displayed.
		st.session_state["last_grid_selection"] = curr_runs

	# No in-grid selection; rely on sidebar-selected_set and last_grid_selection
	# for determining which runs to visualize.
	selected_current: List[str] = []

	# --- Metrics Viewer helpers ---
	def _find_training_metrics_csv(run_dir_path: str) -> str | None:
		try:
			for fname in os.listdir(run_dir_path):
				if fname.endswith("_training_metrics.csv"):
					return os.path.join(run_dir_path, fname)
		except Exception:
			pass
		return None

	def _load_metrics_df(run_dir_name: str):
		run_path = os.path.join(base_dir, run_dir_name)
		csv_path = _find_training_metrics_csv(run_path)
		if not csv_path:
			return None
		try:
			# import pandas as pd  # type: ignore
			dfm = pd.read_csv(csv_path)
			return dfm
		except Exception:
			return None

	def _build_title(params: Dict[str, Any], diff_keys: List[str]) -> str:
		"""Build a human-readable title for a run.

		Previously, this function attempted to keep titles very compact by
		showing at most two differing parameters and then appending a
		"+N more" suffix (e.g. "+2 more"). This made it hard to see exactly
		which parameters differed between runs. The requirement now is to
		*spell out all the changes* in the chart titles.

		We still apply mild value-shortening (for long lists/strings) so
		that titles remain readable, but all diff_keys are included
		explicitly in the suffix instead of being collapsed into
		"+N more".
		"""
		ts = params.get("run_timestamp") or params.get("_dir") or ""
		is_ensemble = bool(params.get("_is_ensemble"))

		def _short_val(v: Any) -> str:
			try:
				if isinstance(v, list):
					s = "+".join(str(x) for x in v[:8])
					if len(v) > 8:
						s += "+..."
				else:
					s = str(v)
			except Exception:
				s = str(v)
			# Mild guard against runaway strings
			if len(s) > 64:
				s = s[:61] + "..."
			return s

		parts: List[str] = []
		for k in diff_keys:
			# Skip internal / private keys
			if k.startswith("_"):
				continue
			v = params.get(k)
			if v is None:
				continue
			parts.append(f"{k}={_short_val(v)}")

		suffix = ("-" + ", ".join(parts)) if parts else ""

		title = f"{ts}{suffix}"
		# Make ensemble runs visually distinct in charts so they can be
		# easily identified next to individual reps.
		if is_ensemble:
			title += " [ensemble]"
		return title

	def _viewer_panel(selected_timestamps: List[str]):
		"""Render the metrics and stock simulation viewer.

		The selection coming into this panel is expressed purely in terms of
		timestamp directory names (e.g. "20251124_031117").  For each
		timestamp we expand to all underlying rep* runs using *ts_to_runs* so
		that charts still reflect every repetition even though the UI only
		exposes timestamp-level entries.
		"""

		st.header("Metrics viewer")
		if not selected_timestamps:
			st.info("No runs selected for viewing yet. Use the sidebar or the grid above to pick some runs.")
			return

		# Collect params for all reps belonging to the selected timestamps,
		# preserving the timestamp ordering and the per-timestamp rep
		# ordering inside each group.
		chosen_params: List[Dict[str, Any]] = []
		for ts_name in selected_timestamps:
			run_list = ts_to_runs.get(str(ts_name), [])
			for _name, params in run_list:
				chosen_params.append(params)
		# Diff keys for titles
		common_local = _compute_common_params(chosen_params)
		diff_keys = [k for k in sorted({k for p in chosen_params for k in p.keys()}) if k not in set(common_local.keys()) and not k.startswith("_")]

		# Load metrics and compute union of metric columns
		metric_dfs = []
		all_metric_cols: List[str] = []
		for p in chosen_params:
			dname = p.get("_dir")
			if not dname:
				metric_dfs.append((p, None))
				continue
			dfm = _load_metrics_df(str(dname))
			metric_dfs.append((p, dfm))
			if dfm is not None:
				# Prefer numeric columns
				try:
					num_cols = [c for c in dfm.columns if dfm[c].dtype.kind in "if" and c]
				except Exception:
					num_cols = list(dfm.columns)
				for c in num_cols:
					if c not in all_metric_cols:
						all_metric_cols.append(c)

		# Choose x-axis
		candidate_x = ["epoch", "step", "iteration", "batch", "index"]
		x_axis = None
		for cand in candidate_x:
			if any((df is not None and cand in df.columns) for _, df in metric_dfs):
				x_axis = cand
				break
		# If no named x, use DataFrame index later

		# Checkbox controls for metrics (top of viewer)
		st.subheader("Metrics to show")
		# Global toggle for logarithmic Y-axis across all charts in the viewer.
		# Persisted via session_state so it survives tab switches and reruns.
		log_y_default = bool(st.session_state.get("viewer_log_y", False))
		log_y = st.checkbox("Logarithmic Y-axis", value=log_y_default, key="viewer_log_y")

		# Number of initial points to ignore from the beginning of each run's series.
		# Implemented as a small dropdown (0	6) so it's quick to adjust and consistent
		# across all charts. Persisted in session_state.
		ignore_head_default = int(st.session_state.get("viewer_ignore_head", 0) or 0)
		ignore_head_default = max(0, min(5, ignore_head_default))
		ignore_head = st.selectbox(
			"Ignore first N points",
			options=list(range(0, 6)),
			index=ignore_head_default,
			help="Number of leading data points to drop from each run before plotting.",
			key="viewer_ignore_head",
			width=100,
		)

		# Layout toggle: either one chart per run (original behaviour) or
		# a single combined chart that overlays all selected runs to make
		# comparisons easier.
		single_chart_default = bool(st.session_state.get("viewer_single_chart", False))
		single_chart = st.checkbox(
			"Single chart (overlay all selected runs)",
			value=single_chart_default,
			key="viewer_single_chart",
			help=(
				"When enabled, show all selected runs together in a single chart. "
				"Each run/metric pair will appear as a separate line in the legend."
			),
		)
		if not all_metric_cols:
			st.warning("No metric CSVs found for selected runs. Ensure each run contains a *_training_metrics.csv file.")
		# Default: show all numeric metrics except the x-axis if present
		current_checks = st.session_state.get("viewer_metric_checks", {})
		# Initialize missing keys
		for col in all_metric_cols:
			if col == x_axis:
				continue
			if col not in current_checks:
				current_checks[col] = True
		# Remove stale keys no longer present
		for k in list(current_checks.keys()):
			if k not in all_metric_cols:
				current_checks.pop(k, None)
		st.session_state["viewer_metric_checks"] = current_checks

		# Render checkboxes in 4 columns
		if all_metric_cols:
			cols = st.columns(4)
			for i, col in enumerate([c for c in all_metric_cols if c != x_axis]):
				with cols[i % 4]:
					current_checks[col] = st.checkbox(col, value=bool(current_checks.get(col, True)), key=f"metric_check_{col}")

		selected_metrics = [c for c, on in current_checks.items() if on]

		# Render charts in n x n grid
		n = len(metric_dfs)
		if n == 0:
			st.info("Nothing to plot.")
			return
		cols_per_row = max(1, int(ceil(sqrt(n))))

		try:
			# import pandas as pd  # type: ignore
			import altair as alt  # type: ignore
		except Exception:
			alt = None

		def _render_single_chart():
			"""Render all selected runs and metrics on a single chart.

			This focuses on comparative analysis across runs. It prefers
			Plotly or Matplotlib backends; if neither is available, it
			gracefully falls back to the original per-run grid layout.
			"""

			# Collect all traces as (x, y, label) tuples.
			series: List[Tuple[List[float], List[float], str]] = []

			for p, dfm in metric_dfs:
				if dfm is None or dfm.empty:
					continue
				# Pick x column for this run (may differ between runs).
				if x_axis is not None and x_axis in dfm.columns:
					local_x = dfm[x_axis].tolist()
					base_df = dfm[[x_axis] + [c for c in selected_metrics if c in dfm.columns]].copy()
					x_name = x_axis
				else:
					base_df = dfm[[c for c in selected_metrics if c in dfm.columns]].copy()
					base_df.insert(0, "index", range(len(base_df)))
					local_x = base_df["index"].tolist()
					x_name = "index"

				if not base_df.columns.tolist() or len(base_df) == 0:
					continue

				# Apply head trimming for this run.
				try:
					trim_n = int(ignore_head or 0)
				except Exception:
					trim_n = 0
				if trim_n > 0:
					if len(local_x) <= trim_n:
						continue
					local_x = local_x[trim_n:]
					base_df = base_df.iloc[trim_n:].reset_index(drop=True)

				# Legend-friendly description of this run.
				run_title = _build_title(p, diff_keys)

				for metric in selected_metrics:
					if metric not in base_df.columns:
						continue
					y_vals = base_df[metric].tolist()
					if not y_vals:
						continue
					label = f"{run_title}: {metric}"
					series.append((local_x, y_vals, label))

			if not series:
				st.caption("No data available to render a combined chart with the current selection.")
				return

			# Prefer Plotly for rich interactive comparison.
			try:
				import plotly.graph_objects as go  # type: ignore

				fig = go.Figure()
				for xs, ys, label in series:
					fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name=label))

				fig.update_layout(
					height=360,
					margin=dict(l=10, r=10, t=10, b=10),
					legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
					xaxis_title=x_axis or "index",
					yaxis_title="value",
				)
				if log_y:
					fig.update_yaxes(type="log")
				st.plotly_chart(fig, width='stretch')
				return
			except Exception:
				pass

			# Fallback: Matplotlib.
			try:
				import matplotlib.pyplot as plt  # type: ignore

				fig, ax = plt.subplots(figsize=(7.0, 4.0))
				for xs, ys, label in series:
					ax.plot(xs, ys, label=label)
				ax.set_xlabel(x_axis or x_name)
				ax.set_ylabel("value")
				if log_y:
					ax.set_yscale("log")
				ax.legend(loc="best", fontsize="x-small")
				ax.grid(True, alpha=0.3)
				st.pyplot(fig, clear_figure=True)
				return
			except Exception:
				pass

			# If we get here we have no suitable plotting backend; fall back
			# to the original per-run layout so the user still sees charts.
			st.warning(
				"Single chart mode could not find a supported plotting backend; "
				"showing per-run charts instead. Install Plotly or Matplotlib "
				"for best combined-view support."
			)

		# Local plotting helper that avoids Streamlit's built-in charts (which import Altair)
		def _plot_lines(plot_df, x_enc: str, height: int = 260, y_ref: float | None = None):
			"""Plot one or more series as lines.

			Parameters
			----------
			plot_df:
				DataFrame with x_enc plus one or more value columns.
			x_enc:
				Name of the x-axis column.
			height:
				Target height in pixels.
			y_ref:
				Optional horizontal reference line (e.g. 1000 for
				stock_simulation_10.csv charts). When None, no line is drawn.
			"""
			# Prefer Plotly (no Altair dependency)
			try:
				import plotly.graph_objects as go  # type: ignore

				fig = go.Figure()
				for col in [c for c in plot_df.columns if c != x_enc]:
					fig.add_trace(go.Scatter(x=plot_df[x_enc], y=plot_df[col], mode="lines", name=str(col)))
				# Optional horizontal reference line
				if y_ref is not None:
					try:
						fig.add_hline(y=y_ref, line=dict(color="red", dash="dash", width=1))
					except Exception:
						pass
				fig.update_layout(
					height=height,
					margin=dict(l=10, r=10, t=10, b=10),
					legend=dict(orientation="h"),
				)
				if log_y:
					# Use logarithmic scale on Y axis; non-positive values will be ignored by Plotly.
					fig.update_yaxes(type="log")
				st.plotly_chart(fig, width='stretch')
				return
			except Exception:
				pass

			# Fallback to matplotlib
			try:
				import matplotlib.pyplot as plt  # type: ignore

				fig, ax = plt.subplots(figsize=(6, max(2.0, height / 130.0)))
				for col in [c for c in plot_df.columns if c != x_enc]:
					ax.plot(plot_df[x_enc], plot_df[col], label=str(col))
				ax.set_xlabel(x_enc)
				ax.set_ylabel("value")
				if y_ref is not None:
					try:
						ax.axhline(y_ref, color="red", linestyle="--", linewidth=1)
					except Exception:
						pass
				if log_y:
					# Logarithmic scaling on Y; Matplotlib will handle non-positive values by skipping them.
					ax.set_yscale("log")
				ax.legend(loc="best", fontsize="small")
				ax.grid(True, alpha=0.3)
				st.pyplot(fig, clear_figure=True)
				return
			except Exception:
				pass

			# Final fallback: render a lightweight Chart.js line chart via HTML (no extra Python deps)
			try:
				import json as _json  # reuse json safely
				from streamlit.components.v1 import html as _html  # type: ignore

				labels = plot_df[x_enc].tolist()
				datasets = []
				palette = [
					"#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
					"#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
				]
				idx_color = 0
				value_cols = [c for c in plot_df.columns if c != x_enc]
				for col in value_cols:
					datasets.append({
						"label": str(col),
						"data": plot_df[col].tolist(),
						"borderColor": palette[idx_color % len(palette)],
						"fill": False,
						"tension": 0.1,
					})
					idx_color += 1

				# Optional reference line rendered as a separate dataset
				if y_ref is not None and len(plot_df[x_enc]) > 0:
					datasets.append({
						"label": f"y={y_ref}",
						"data": [y_ref] * len(plot_df[x_enc]),
						"borderColor": "#ff0000",
						"borderDash": [5, 5],
						"fill": False,
						"tension": 0.0,
						"pointRadius": 0,
					})

				payload = {
					"labels": labels,
					"datasets": datasets,
					"xlabel": x_enc,
					"log_y": bool(log_y),
				}

				_html(
					f"""
					<div>
					  <canvas id='c' height='{height}'></canvas>
					</div>
					<script src='https://cdn.jsdelivr.net/npm/chart.js'></script>
					<script>
					  const payload = {_json.dumps(payload)};
					  const ctx = document.getElementById('c').getContext('2d');
					  new Chart(ctx, {{
						type: 'line',
						data: {{ labels: payload.labels, datasets: payload.datasets }},
						options: {{
						  responsive: true,
						  maintainAspectRatio: false,
						  plugins: {{ legend: {{ display: true, position: 'bottom' }} }},
						  scales: {{
							x: {{ title: {{ display: true, text: payload.xlabel }} }},
							y: {{
							  type: payload.log_y ? 'logarithmic' : 'linear',
							  title: {{ display: true, text: 'value' }}
							}}
						  }}
						}}
					  }});
					</script>
					""",
					height=height + 40,
				)
				return
			except Exception:
				pass

			# Last resort: show the data frame
			st.dataframe(plot_df.set_index(x_enc), width='stretch')

		# If the user asked for a single combined chart, render that instead
		# of the per-run grid layout. The stock simulation section that
		# follows is rendered regardless of this choice.
		if single_chart and selected_metrics:
			_render_single_chart()
		else:
			# CSS for per-chart titles: make them smaller and allow long text to wrap.
			# This helps when many run parameters are encoded into the title.
			try:
				st.markdown(
					"""
					<style>
					.run-chart-title {
						/* Roughly half the size of the default Streamlit h5 title */
						font-size: 0.7rem;
						font-weight: 600;
						line-height: 1.2;
						/* Allow long titles to wrap instead of truncating */
						white-space: normal !important;
						word-wrap: break-word;
						overflow-wrap: anywhere;
						margin: 0 0 0.25rem 0;
					}
					</style>
					""",
					unsafe_allow_html=True,
				)
			except Exception:
				# Styling is best-effort; if it fails the charts still render.
				pass

			rows_rendered = 0
			current_row_cols = None
			for idx, (p, dfm) in enumerate(metric_dfs):
				if idx % cols_per_row == 0:
					current_row_cols = st.columns(cols_per_row)
				col_slot = current_row_cols[idx % cols_per_row]
				with col_slot:
					title = _build_title(p, diff_keys)
					# Use a smaller, wrapping title so long parameter descriptions fit better.
					st.markdown(f"<div class='run-chart-title'>{title}</div>", unsafe_allow_html=True)
					if dfm is None or dfm.empty or not selected_metrics:
						st.caption("No data to plot for this run.")
						continue
					# Build plotting DataFrame
					plot_df = dfm.copy()
					if x_axis and x_axis in plot_df.columns:
						plot_df = plot_df[[x_axis] + [m for m in selected_metrics if m in plot_df.columns]].copy()
						x_enc = x_axis
					else:
						plot_df = plot_df[[m for m in selected_metrics if m in plot_df.columns]].copy()
						plot_df.insert(0, "index", range(len(plot_df)))
						x_enc = "index"
					# Apply head trimming based on the user's dropdown choice.
					# This drops the first N rows (shared across all metrics and x-axis).
					try:
						trim_n = int(ignore_head or 0)
					except Exception:
						trim_n = 0
					if trim_n > 0:
						if len(plot_df) <= trim_n:
							st.caption(f"Not enough points to plot after ignoring first {trim_n} values.")
							continue
						plot_df = plot_df.iloc[trim_n:].reset_index(drop=True)
					# Melt to long for Altair
					try:
						mdf = plot_df.melt(id_vars=[x_enc], var_name="metric", value_name="value")
						if alt is not None:
							y_encoding = alt.Y(
								"value:Q",
								title="value",
								scale=alt.Scale(type="log") if log_y else alt.Undefined,
							)
							chart = (
								alt.Chart(mdf)
								.mark_line()
								.encode(
									x=alt.X(f"{x_enc}:Q", title=x_enc),
									y=y_encoding,
									color=alt.Color("metric:N", legend=alt.Legend(title="metric")),
									tooltip=[x_enc, "metric", "value"],
								)
								.properties(height=260)
							)
							st.altair_chart(chart, width='stretch')
						else:
							# Our own fallback (Plotly/Matplotlib) to avoid Altair import issues
							_plot_lines(plot_df, x_enc, height=260)
					except Exception:
						_plot_lines(plot_df, x_enc, height=260)

		# --- Stock simulation viewer (stock_simulation_*.csv) ---
		# This section is rendered *under* the standard metrics charts and is
		# handled independently: it looks for a stock_simulation_<N>.csv in
		# each selected run directory (for any integer N) and plots any
		# columns containing "_top" in their name on dedicated charts.

		st.subheader("Stock simulation (stock_simulation_*.csv)")

		# Locate and load stock_simulation_<N>.csv for each selected run. If
		# multiple files exist we prefer the one with the highest <N>.
		#
		# In addition, when the selected run corresponds to a ``rep*``
		# directory underneath a timestamp directory (e.g.
		# ``20251123_233753/rep1``), we also look for an "ensemble" stock
		# simulation CSV directly under the timestamp directory itself. This
		# file represents the aggregate/ensemble simulation across all reps
		# and should be plotted *before* the individual reps.
		sim_dfs: List[Tuple[Dict[str, Any], Any]] = []
		all_top_cols: List[str] = []

		print("[stock_sim] starting stock simulation section")

		# Detect and load ensemble stock_simulation_*_epoch_*.csv files that
		# live directly in the *timestamp* directory.  With the new
		# run-family layout, a rep's ``_dir`` looks like
		#   run_family_.../<timestamp>/repN
		# so we derive the timestamp directory by stripping the final
		# component while preserving the family prefix.
		#
		# We key cached ensemble DataFrames by the timestamp string (``_ts``)
		# so we can insert them ahead of the corresponding rep runs below.
		ensemble_per_ts: Dict[str, Any] = {}
		try:
			import pandas as pd  # type: ignore

			print(f"[stock_sim] total runs from scan_runs: {len(runs)}")
			for name, params in runs:
				dname = params.get("_dir")
				if not dname:
					continue

				parts = str(dname).split("/")
				if not parts:
					continue
				rep_part = parts[-1]
				if not rep_part.lower().startswith("rep"):
					continue

				# Timestamp identifier (without family prefix) comes from
				# the params metadata; this remains stable regardless of
				# whether runs live under run_family_*.
				ts_name = str(params.get("_ts") or "")
				if not ts_name:
					continue

				# Only consider timestamps that the user has actually
				# selected via *chosen_params*.
				if not any(str(p.get("_ts")) == ts_name for p in chosen_params):
					continue

				if ts_name in ensemble_per_ts:
					# Already processed this timestamp.
					continue

				# Derive the timestamp directory relative to base_dir by
				# stripping the trailing rep component from ``_dir``.
				ts_rel = "/".join(parts[:-1])
				if not ts_rel:
					print(f"[stock_sim] empty ts_rel for dname={dname}")
					continue
				ts_path = os.path.join(base_dir, ts_rel)
				if not os.path.isdir(ts_path):
					print(f"[stock_sim] ts_path not dir: {ts_path}")
					continue

				best_csv_path = None
				best_epoch = None
				print(f"[stock_sim] scanning ensemble dir: {ts_path}")
				try:
					for fname in os.listdir(ts_path):
						# Match the current naming convention
						#   stock_simulation_*_epoch_<epoch>.csv
						m = re.match(r"stock_simulation_.*_epoch_(\d+)\.csv$", fname)
						if not m:
							continue
						epoch_idx = int(m.group(1))
						if best_epoch is None or epoch_idx > best_epoch:
							best_epoch = epoch_idx
							best_csv_path = os.path.join(ts_path, fname)
				except FileNotFoundError:
					best_csv_path = None

				if not best_csv_path or not os.path.exists(best_csv_path):
					print(f"[stock_sim] no ensemble csv chosen for ts={ts_name}")
					continue

				try:
					dfs_ens = pd.read_csv(best_csv_path)
				except Exception as e:
					print(f"[stock_sim] failed to read ensemble csv {best_csv_path}: {e}")
					dfs_ens = None

				ensemble_per_ts[ts_name] = dfs_ens
				print(f"[stock_sim] loaded ensemble for ts={ts_name}: path={best_csv_path}, has_df={dfs_ens is not None}")

				# Collect numeric columns from this ensemble file as well so
				# they are available to the checkbox UI even if a particular
				# rep is missing some of them.
				if dfs_ens is not None:
					try:
						num_cols = [
							c
							for c in dfs_ens.columns
							if isinstance(c, str)
							and dfs_ens[c].dtype.kind in "if"
							and c not in {"feature_date_id", "open_date_id", "close_date_id"}
						]
					except Exception:
						num_cols = [
							c
							for c in dfs_ens.columns
							if isinstance(c, str)
							and c not in {"feature_date_id", "open_date_id", "close_date_id"}
						]
					for c in num_cols:
						if c not in all_top_cols:
							all_top_cols.append(c)
		except Exception as e:
			# If anything goes wrong while looking for ensemble files, we
			# silently fall back to the per-run behaviour below.
			print(f"[stock_sim] exception while building ensemble_per_ts: {e}")
			ensemble_per_ts = {}

		# Now load the per-run stock_simulation CSVs, inserting an ensemble
		# entry per timestamp *before* the corresponding reps.
		seen_ensemble_ts: set[str] = set()
		seen_rep_dirs: set[str] = set()
		for p in chosen_params:
			dname = p.get("_dir")
			if not dname:
				sim_dfs.append((p, None))
				continue

			str_dname = str(dname)
			# Prefer the explicit _ts metadata (independent of family prefix)
			# so ensemble entries are keyed consistently with the grids.
			ts_name = str(p.get("_ts") or "")

			# If this is the *first* rep we encounter for a timestamp that has
			# an ensemble CSV, insert a synthetic ensemble entry ahead of it.
			if ts_name and ts_name in ensemble_per_ts and ts_name not in seen_ensemble_ts:
				ens_params = {k: v for k, v in p.items() if not k.startswith("_")}
				ens_params["_dir"] = ts_name  # logical identifier
				ens_params["_is_ensemble"] = True
				ens_params["_timestamp"] = ts_name
				# Make it obvious in the title that this line is the
				# ensemble/aggregate across reps.
				ens_params.setdefault("rep", "ensemble")

				sim_dfs.append((ens_params, ensemble_per_ts.get(ts_name)))
				seen_ensemble_ts.add(ts_name)

			# For the selected timestamp, include *all* reps under its
			# directory, not just the representative run in chosen_params.
			# This ensures we see ensemble + rep1 + rep2 + rep3, etc.
			if ts_name:
				# Find all runs from `runs` that share this _ts.
				for run_name, run_params in runs:
					if str(run_params.get("_ts") or "") != ts_name:
						continue
					rep_dir = run_params.get("_dir")
					if not rep_dir or rep_dir in seen_rep_dirs:
						continue
					seen_rep_dirs.add(rep_dir)

					rep_path = os.path.join(base_dir, str(rep_dir))
					best_csv_path = None
					best_epoch = None
					try:
						for fname in os.listdir(rep_path):
							m = re.match(r"stock_simulation_.*_epoch_(\d+)\.csv$", fname)
							if not m:
								continue
							epoch_idx = int(m.group(1))
							if best_epoch is None or epoch_idx > best_epoch:
								best_epoch = epoch_idx
								best_csv_path = os.path.join(rep_path, fname)
					except FileNotFoundError:
						best_csv_path = None

					if not best_csv_path or not os.path.exists(best_csv_path):
						rep_dfs = None
					else:
						try:
							# import pandas as pd  # type: ignore
							rep_dfs = pd.read_csv(best_csv_path)

							# Collect numeric/top columns from this rep so they are available
							# even if the ensemble file didn't have them.
							rep_cols = [
								c for c in rep_dfs.columns
								if c not in ["feature_date_id", "open_date_id", "close_date_id"]
								and "top" in c
							]
							all_top_cols = list(sorted(set(all_top_cols + rep_cols)))
						except Exception:
							rep_dfs = None

					# Use run_params so per-rep parameters (e.g. rep id) are
					# reflected in titles and downstream logic.
					# We explicitly inject 'rep' from the directory name if missing.
					p_copy = dict(run_params)
					parts = str(p_copy.get("_dir", "")).split("/")
					if parts and parts[-1].lower().startswith("rep"):
						p_copy["rep"] = parts[-1]

					sim_dfs.append((p_copy, rep_dfs))

		# Fallback: if for some reason the above loop did not add any
		# entries (e.g. no matching runs for chosen_params), keep the
		# previous single-run behaviour so the viewer still works.
		if not sim_dfs:
			for p in chosen_params:
				dname = p.get("_dir")
				if not dname:
					sim_dfs.append((p, None))
					continue
				str_dname = str(dname)
				run_path = os.path.join(base_dir, str_dname)
				best_csv_path = None
				best_epoch = None
				try:
					for fname in os.listdir(run_path):
						m = re.match(r"stock_simulation_.*_epoch_(\d+)\.csv$", fname)
						if not m:
							continue
						epoch_idx = int(m.group(1))
						if best_epoch is None or epoch_idx > best_epoch:
							best_epoch = epoch_idx
							best_csv_path = os.path.join(run_path, fname)
				except FileNotFoundError:
					best_csv_path = None

				if not best_csv_path or not os.path.exists(best_csv_path):
					dfs = None
				else:
					try:
						# import pandas as pd  # type: ignore
						dfs = pd.read_csv(best_csv_path)
					except Exception:
						dfs = None
				sim_dfs.append((p, dfs))

		# At this point sim_dfs should contain an ensemble entry (if
		# available) plus one entry per rep under the selected timestamp.
		print(f"[stock_sim] total sim_dfs entries: {len(sim_dfs)}, numeric cols={all_top_cols}")

		if not any(df is not None for _, df in sim_dfs):
			st.caption("No stock_simulation_*.csv files found for the currently selected runs.")
			return

		# Do not early-return on all_top_cols being empty; if necessary we
		# will detect numeric columns per run below.

		# --- Column visibility controls for stock simulation charts ---
		# Determine the universe of columns we can toggle. Prefer the
		# pre-computed ``all_top_cols`` but fall back to scanning the first
		# non-empty DataFrame if needed.
		sim_cols: List[str] = []
		if all_top_cols:
			# all_top_cols already excludes non-simulation identifier columns.
			sim_cols = list(all_top_cols)
		else:
			for _p, _df in sim_dfs:
				if _df is None or _df.empty:
					continue
				try:
					cand_cols = [
						c
						for c in _df.columns
						if isinstance(c, str)
						and _df[c].dtype.kind in "if"
						and c not in {"feature_date_id", "open_date_id", "close_date_id"}
					]
				except Exception:
					cand_cols = [
						c
						for c in _df.columns
						if isinstance(c, str)
						and c not in {"feature_date_id", "open_date_id", "close_date_id"}
					]
				if cand_cols:
					sim_cols = cand_cols
					break

		# Persist per-column visibility in session_state so choices survive
		# reruns and tab switches.
		current_sim_checks = dict(st.session_state.get("stock_sim_checks", {}))
		for col in sim_cols:
			if col not in current_sim_checks:
				current_sim_checks[col] = True
		# Drop any stale entries from previous selections.
		for k in list(current_sim_checks.keys()):
			if k not in sim_cols:
				current_sim_checks.pop(k, None)

		# Compute whether *currently* all columns are selected *before* we
		# apply any new Select All action. This is used to initialise the
		# checkbox so its visual state reflects the per-column selection.
		all_selected_before = bool(sim_cols) and all(current_sim_checks.get(c, False) for c in sim_cols)
		prev_select_all = bool(st.session_state.get("stock_sim_select_all", True))

		st.markdown("**Stock simulation columns to show**")
		select_all = st.checkbox(
			"Select all",
			value=all_selected_before,
			key="stock_sim_select_all",
			help=(
				"Toggle visibility of all stock simulation graphs. If all are currently "
				"selected and this is unchecked, every graph will be hidden."
			),
		)

		# Special behaviour for Select All:
		#  - If it transitions from checked to unchecked, we clear *all*
		#    column checkboxes, regardless of their previous mix of states.
		#  - If it transitions from unchecked to checked, we select every
		#    column regardless of previous per-column state.
		if prev_select_all and not select_all:
			for col in sim_cols:
				current_sim_checks[col] = False
		elif (not prev_select_all) and select_all:
			for col in sim_cols:
				current_sim_checks[col] = True

		# Render individual checkboxes in a compact multi-column layout.
		if sim_cols:
			cols_ui = st.columns(4)
			for idx, col in enumerate(sim_cols):
				with cols_ui[idx % 4]:
					current_sim_checks[col] = st.checkbox(
						col,
						value=bool(current_sim_checks.get(col, True)),
						key=f"stock_sim_col_{col}",
					)

		# Persist updated state for next rerun.
		st.session_state["stock_sim_checks"] = current_sim_checks
		st.session_state["stock_sim_all_selected_prev"] = bool(
			bool(sim_cols) and all(current_sim_checks.get(c, False) for c in sim_cols)
		)

		# Final list of columns that are actually enabled for plotting.
		selected_sim_cols = [c for c in sim_cols if current_sim_checks.get(c, False)]

		# Bin width for histograms of day-over-day change magnitudes in the
		# stock simulation curves. Expressed as a ratio (e.g. 0.01 == 1%).
		# Kept separate from the main metrics controls so it only affects the
		# stock simulation section.

		default_hist_bin_width = 0.01
		hist_bin_width = st.number_input(
			"Histogram bin width (ratio)",
			min_value=0.001,
			max_value=0.5,
			value=float(st.session_state.get("stock_sim_hist_bin_width", default_hist_bin_width) or default_hist_bin_width),
			step=0.001,
			format="%.3f",
			key="stock_sim_hist_bin_width",
			help=(
				"Width of each histogram bin expressed as a day-over-day change ratio. "
				"For example, 0.01 groups changes in 1% increments by |ratio|."
			),
		)

		# Independent control for whether CDF overlays are shown on the
		# per-run stock simulation charts. This ensures the CDF lines are
		# also user-controllable, as requested.
		show_cdf_default = bool(st.session_state.get("stock_sim_show_cdf", True))
		show_cdf = st.checkbox(
			"Show CDF overlays",
			value=show_cdf_default,
			key="stock_sim_show_cdf",
			help=(
				"When enabled, overlay CDF percentile curves alongside portfolio values "
				"for each stock simulation run."
			),
		)

		# Independent control for combining all *_top curves on a single chart.
		sim_single_chart = st.checkbox(
			"Display on single chart",
			value=False,
			key="stock_sim_single_chart",
			help="When enabled, overlay *_top curves from all runs on one chart.",
		)

		# Determine x-axis for stock simulations: prefer feature_date_id if present.
		preferred_sim_x = "feature_date_id"

		if sim_single_chart:
			# Build combined series using Plotly/Matplotlib directly.
			series: List[Tuple[List[float], List[float], str]] = []

			for p, dfs in sim_dfs:
				if dfs is None or dfs.empty:
					continue

				if preferred_sim_x in dfs.columns:
					xs = dfs[preferred_sim_x].tolist()
					x_name = preferred_sim_x
				else:
					dfs = dfs.reset_index(drop=True)
					xs = list(range(len(dfs)))
					x_name = "index"

				run_title = _build_title(p, diff_keys)

				# Respect the per-column visibility selection when building
				# the combined chart. If the user has turned everything off,
				# we simply render an empty combined view below.
				cols_for_run = selected_sim_cols if selected_sim_cols else []
				for col in cols_for_run:
					if col not in dfs.columns:
						continue
					ys = dfs[col].tolist()
					label = f"{run_title}: {col}"
					series.append((xs, ys, label))

			if not series:
				st.caption("No *_top data available to render a combined stock simulation chart.")
				return

			# Prefer Plotly for the combined simulation view
			try:
				import plotly.graph_objects as go  # type: ignore

				fig = go.Figure()
				for xs, ys, label in series:
					fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name=label))

				# Horizontal reference line at 1000
				try:
					fig.add_hline(y=1000.0, line=dict(color="red", dash="dash", width=1))
				except Exception:
					pass

				fig.update_layout(
					height=360,
					margin=dict(l=10, r=10, t=10, b=10),
					legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
					xaxis_title=x_name,
					yaxis_title="portfolio value",
				)
				st.plotly_chart(fig, width='stretch')
				return
			except Exception:
				pass

			# Fallback: Matplotlib for combined simulation chart
			try:
				import matplotlib.pyplot as plt  # type: ignore

				fig, ax = plt.subplots(figsize=(7.0, 4.0))
				for xs, ys, label in series:
					ax.plot(xs, ys, label=label)
				ax.set_xlabel(x_name)
				ax.set_ylabel("portfolio value")
				try:
					ax.axhline(1000.0, color="red", linestyle="--", linewidth=1)
				except Exception:
					pass
				ax.legend(loc="best", fontsize="x-small")
				ax.grid(True, alpha=0.3)
				st.pyplot(fig, clear_figure=True)
				return
			except Exception:
				pass

			# If we reach here, fall back to a simple per-run layout so at
			# least some charts are visible.
			st.warning(
				"Unable to render the combined stock simulation chart with the available backends; "
				"showing per-run charts instead."
			)

		# Per-run stock simulation charts (one chart per run, stacked).
		print(f"[stock_sim] total sim_dfs entries: {len(sim_dfs)}, numeric cols={all_top_cols}")

		for p, dfs in sim_dfs:
			base_title = _build_title(p, diff_keys)
			# Make it clear whether this series is an ensemble or a
			# particular repetition so multiple charts for the same
			# timestamp are distinguishable.
			tag: str | None = None
			if p.get("_is_ensemble"):
				tag = "ensemble"
			else:
				rep_label = p.get("rep") or p.get("_rep")
				if rep_label is not None:
					tag = str(rep_label)

			title = base_title if not tag else f"{base_title} ({tag})"
			st.markdown(f"**{title}**")

			if dfs is None or dfs.empty:
				print(f"[stock_sim] empty dfs for title={title}, dir={p.get('_dir')}")
				st.caption("No stock_simulation_*.csv data for this run.")
				continue

			# Restrict to the numeric simulation columns we detected earlier
			# (typically ``portfolio_value`` for the new CSV schema).
			# If all_top_cols is empty (e.g. no ensemble found), we detect
			# columns on the fly from this dataframe.
			if all_top_cols:
				cols_here = [c for c in all_top_cols if c in dfs.columns]
			else:
				try:
					cols_here = [
						c
						for c in dfs.columns
						if isinstance(c, str)
						and dfs[c].dtype.kind in "if"
						and c not in {"feature_date_id", "open_date_id", "close_date_id"}
					]
				except Exception:
					cols_here = []

			# Apply the global stock-simulation column visibility filter. When
			# the user unchecks all columns via the Select All toggle, this
			# will intentionally hide all curves for every run.
			if selected_sim_cols:
				cols_here = [c for c in cols_here if c in selected_sim_cols]

			if not cols_here:
				print(
					f"[stock_sim] no numeric/visible cols for title={title}, "
					f"available={list(dfs.columns)}, selected={selected_sim_cols}"
				)
				if selected_sim_cols:
					st.caption(
						"None of the enabled stock simulation columns are present in this run's "
						"stock_simulation_*.csv file."
					)
				else:
					st.caption("All stock simulation columns are currently hidden.")
				continue

			# Calculate CDFs dynamically for all portfolio columns
			# Filter numeric cols to exclude any existing CDF/percentile columns for the primary axis
			primary_cols = [c for c in cols_here if "percentile" not in c and "CDF" not in c]

			# Build plot_df with just primary cols first
			cols_needed = primary_cols[:]

			if preferred_sim_x in dfs.columns:
				plot_df = dfs[[preferred_sim_x] + cols_needed].copy()
				x_enc = preferred_sim_x
			else:
				plot_df = dfs[cols_needed].copy()
				plot_df.insert(0, "index", range(len(plot_df)))
				x_enc = "index"

			# Dynamic CDF calculation (optional)
			cdf_cols = []
			date_col = "open_date_id"

			if show_cdf and date_col in dfs.columns and not dfs.empty and primary_cols:
				print(
					f"[stock_sim] starting CDF calc for title={title}: date_col='{date_col}', "
					f"rows={len(dfs)}, primary_cols={primary_cols}"
				)
				try:
					dates = dfs[date_col].values
					valid_dates = [int(d) for d in dates if d > 0]
					min_d = min(valid_dates) if valid_dates else None
					max_d = max(valid_dates) if valid_dates else None
					if min_d is not None and max_d is not None:
						print(f"[stock_sim] date range for CDF query: min={min_d} max={max_d}")

					# Parse topNRecommends -> (date_id, top1 ticker)
					topn_pairs: List[Tuple[int, int]] = []
					top1_map: Dict[int, int] = {}
					if "topNRecommends" in dfs.columns:
						try:
							for idx, val in enumerate(dfs["topNRecommends"].astype(str).values):
								if idx >= len(dates):
									continue
								curr_date = int(dates[idx])
								parts = [p for p in str(val).split("|") if p]
								if parts:
									try:
										tid = int(parts[0])
										topn_pairs.append((curr_date, tid))
										top1_map[curr_date] = tid
									except Exception:
										continue
						except Exception as e_topn:
							print(f"[stock_sim] error parsing topNRecommends: {e_topn}")

					# Prefer SQL rank ratios when available
					rank_ratios: Dict[Tuple[int, int], float] = {}
					if topn_pairs:
						rank_ratios = _fetch_rank_ratios(topn_pairs)
						print(f"[stock_sim] rank_ratios fetched: {len(rank_ratios)}; sample={list(rank_ratios.items())[:3]}")
						market_deltas: Dict[int, List[float]] = {}
					else:
						market_deltas = _fetch_market_deltas(min_d, max_d) if (min_d is not None and max_d is not None) else {}
						print(
							f"[stock_sim] market_deltas keys count={len(market_deltas)}, "
							f"sample_keys={sorted(list(market_deltas.keys()))[:5]}"
						)

					ret_override = None
					if "top1_daily_return" in dfs.columns:
						try:
							ret_override = dfs["top1_daily_return"].astype(float).values
						except Exception:
							ret_override = None

					for col in primary_cols:
						series = dfs[col].values
						cdf_series: List[float] = []

						for i in range(len(series)):
							curr_date = int(dates[i])

							# Use top1_daily_return when available; else portfolio day-over-day
							if ret_override is not None:
								ret = ret_override[i] if i < len(ret_override) else np.nan
							elif i == 0:
								ret = np.nan
							else:
								prev_val = series[i - 1]
								curr_val = series[i]
								ret = (curr_val / prev_val) - 1.0 if prev_val > 0 else np.nan

							rank_val = np.nan
							top_tid = top1_map.get(curr_date)
							if top_tid is not None and rank_ratios:
								rank_val = rank_ratios.get((curr_date, top_tid), np.nan)
							elif market_deltas:
								m_vals_raw = market_deltas.get(curr_date)
								if m_vals_raw and np.isfinite(ret):
									m_vals = [float(x) for x in m_vals_raw if np.isfinite(x)] if hasattr(np, "isfinite") else [float(x) for x in m_vals_raw]
									if m_vals:
										count = sum(1 for x in m_vals if x < ret)
										rank_val = count / float(len(m_vals)) if len(m_vals) > 0 else np.nan

							cdf_series.append(rank_val)

						cdf_name = f"CDF ({col})"
						plot_df[cdf_name] = cdf_series
						cdf_cols.append(cdf_name)
						valid_count = int(np.isfinite(plot_df[cdf_name]).sum()) if hasattr(np, "isfinite") else len(cdf_series)
						print(
							f"[stock_sim] CDF series computed for col={col}: "
							f"len={len(cdf_series)}, non_nan={valid_count}"
						)
				except Exception as e:
					print(f"[stock_sim] Error computing CDFs: {e}")

			has_cdf = len(cdf_cols) > 0
			print(f"[stock_sim] has_cdf={has_cdf} for title={title}, cdf_cols={cdf_cols}")

			if has_cdf:
				try:
					import plotly.graph_objects as go
					from plotly.subplots import make_subplots

					fig = make_subplots(specs=[[{"secondary_y": True}]])

					# Primary: Portfolio Values
					for col in primary_cols:
						fig.add_trace(
							go.Scatter(x=plot_df[x_enc], y=plot_df[col], mode="lines", name=col),
							secondary_y=False
						)

					# Secondary: CDFs
					for cdf_name in cdf_cols:
						fig.add_trace(
							go.Scatter(
								x=plot_df[x_enc],
								y=plot_df[cdf_name],
								mode="lines",
								name=cdf_name,
								line=dict(width=3, dash="dot", color="black"),
								opacity=0.85,
							),
							secondary_y=True
						)

					# Reference lines
					fig.add_hline(y=1000.0, line=dict(color="red", dash="dash", width=1), secondary_y=False)
					fig.add_hline(y=0.5, line=dict(color="gray", dash="dash", width=1), secondary_y=True)

					fig.update_layout(
						height=350,
						margin=dict(l=10, r=10, t=10, b=10),
						legend=dict(orientation="h"),
					)
					fig.update_yaxes(title_text="Portfolio Value", secondary_y=False)
					fig.update_yaxes(title_text="Percentile", range=[-0.05, 1.05], secondary_y=True)

					st.plotly_chart(fig, width="stretch")

				except Exception as e:
					print(f"[stock_sim] Plotly dual-axis error: {e}")
					_plot_lines(plot_df[[x_enc] + primary_cols], x_enc, height=260, y_ref=1000.0)
			else:
				# Standard plotting
				_plot_lines(plot_df[[x_enc] + primary_cols], x_enc, height=260, y_ref=1000.0)

			# ------------------------------------------------------------------
			# Histogram of day-over-day change magnitudes for this run.
			#
			# We interpret "change-in-price-over-day" as the ratio of
			# consecutive portfolio values for each selected simulation column,
			# i.e. ratio_t = (v_t / v_{t-1}) - 1, and use |ratio_t| as the
			# magnitude to be histogrammed.
			# ------------------------------------------------------------------
			try:
				import numpy as _np  # type: ignore

				magnitudes: list[float] = []
				for col in cols_here:
					series = dfs[col].to_numpy(dtype=float)
					if series.size < 2:
						continue
					prev = series[:-1]
					curr = series[1:]
					with _np.errstate(divide="ignore", invalid="ignore"):
						ratio = curr / prev - 1.0
					ratio = _np.abs(ratio)
					# Drop NaN/inf values that may arise from zero or invalid
					# previous values.
					ratio = ratio[_np.isfinite(ratio)]
					if ratio.size:
						magnitudes.extend(ratio.tolist())

				if not magnitudes:
					print(f"[stock_sim] no histogram magnitudes for title={title}")
					continue

				mags = _np.asarray(magnitudes, dtype=float)
				max_mag = float(_np.max(mags))
				min_mag = 0.0

				if hist_bin_width <= 0.0:
					print(f"[stock_sim] invalid hist_bin_width={hist_bin_width} for title={title}")
					continue

				# Build bin edges using the configured window size.
				# Ensure at least one bin even for very small ranges.
				span = max(max_mag - min_mag, float(hist_bin_width))
				n_bins = max(1, int(ceil(span / float(hist_bin_width))))
				edges = _np.linspace(min_mag, min_mag + n_bins * float(hist_bin_width), n_bins + 1)
				counts, bin_edges = _np.histogram(mags, bins=edges)

				# Representative x value for each bin: use the bin centre.
				bin_centres = (bin_edges[:-1] + bin_edges[1:]) / 2.0

				# Prefer Plotly for the histogram; fall back to Matplotlib if
				# Plotly is not available.
				try:
					import plotly.graph_objects as _go  # type: ignore

					fig_hist = _go.Figure()
					fig_hist.add_trace(_go.Bar(x=bin_centres, y=counts, name="|day-over-day change ratio|"))
					fig_hist.update_layout(
						height=220,
						margin=dict(l=10, r=10, t=10, b=10),
						xaxis_title="|change ratio| (day-over-day)",
						yaxis_title="frequency",
						showlegend=False,
					)
					# Use explicit width with a unique key to avoid duplicate-element errors
					st.plotly_chart(fig_hist, width="stretch", key=f"stock_sim_hist_{title}")
				except Exception as e:
					print(f"[stock_sim] Plotly histogram error: {e}")
					try:
						import matplotlib.pyplot as _plt  # type: ignore

						fig_hist, ax_hist = _plt.subplots(figsize=(7.0, 3.0))
						ax_hist.bar(bin_centres, counts, width=float(hist_bin_width) * 0.9)
						ax_hist.set_xlabel("|change ratio| (day-over-day)")
						ax_hist.set_ylabel("frequency")
						ax_hist.grid(True, alpha=0.3)
						st.pyplot(fig_hist, clear_figure=True)
					except Exception as e_plt:
						print(f"[stock_sim] Matplotlib histogram error: {e_plt}")
						# Fallback to streamlit native chart
						try:
							st.caption("Plotly/Matplotlib missing; using native Streamlit chart.")
							import pandas as _pd
							# Format bin centres for readable x-axis labels
							labels = [f"{x:.3f}" for x in bin_centres]
							hist_df = _pd.DataFrame({"frequency": counts}, index=labels)
							st.bar_chart(hist_df)
						except Exception as e_native:
							print(f"[stock_sim] Native chart error: {e_native}")
							# If all backends fail, silently skip the histogram so
							# at least the main line chart remains visible.
							pass
			except Exception as e:
				# Any failure in histogram computation/rendering should not
				# break the main stock simulation charts.
				print(f"[stock_sim] Histogram error: {e}")
				import traceback
				traceback.print_exc()

		# ------------------------------------------------------------------
		# Per-epoch stock simulation history for a *single* selected run.
		#
		# When exactly one logical run directory is selected we additionally
		# look for files named
		#   stock_simulation_<set_size>_epoch_<epoch>.csv
		# inside that run directory. If present, we render a series of
		# stacked charts (one per epoch file) at the very bottom of the
		# viewer, ordered by epoch and clearly labelled.
		# ------------------------------------------------------------------

		# Only activate this section when there is a single underlying run
		# directory in the current selection. This keeps behaviour for
		# multi-run comparison unchanged, as requested.
		unique_run_dirs = sorted({str(p.get("_dir")) for p in chosen_params if p.get("_dir")})
		if len(unique_run_dirs) == 1:
			# import pandas as pd  # type: ignore

			run_rel = unique_run_dirs[0]
			run_path = os.path.join(base_dir, run_rel)
			try:
				fnames = os.listdir(run_path)
			except Exception:
				fnames = []

			epoch_files: List[Tuple[int, int, str]] = []
			# Match patterns like stock_simulation_5_epoch_10.csv
			for fname in fnames:
				m = re.match(r"stock_simulation_(\d+)_epoch_(\d+)\.csv$", fname)
				if not m:
					continue
				try:
					sim_set = int(m.group(1))
					epoch = int(m.group(2))
				except Exception:
					continue
				epoch_files.append((epoch, sim_set, os.path.join(run_path, fname)))

			if epoch_files:
				# Order charts by increasing epoch, then by simulate set size.
				epoch_files.sort(key=lambda t: (t[0], t[1]))

				st.subheader("Per-epoch stock simulation history")
				st.caption(
					"Showing stock_simulation_<set_size>_epoch_<epoch>.csv files "
					"for the currently selected run."
				)

				for epoch, sim_set, fpath in epoch_files:
					try:
						dfs_epoch = pd.read_csv(fpath)
					except Exception:
						continue

					if dfs_epoch is None or dfs_epoch.empty:
						continue

					# Identify *_top numeric columns, as in the main
					# stock_simulation viewer above.
					try:
						cols_here_epoch = [
							c
							for c in dfs_epoch.columns
							if isinstance(c, str)
							and "_top" in c
							and dfs_epoch[c].dtype.kind in "if"
						]
					except Exception:
						cols_here_epoch = [
							c
							for c in dfs_epoch.columns
							if isinstance(c, str) and "_top" in c
						]

					if not cols_here_epoch:
						continue

					# Build plotting DataFrame and x-axis, reusing the same
					# convention as for the main stock simulation charts.
					if preferred_sim_x in dfs_epoch.columns:
						plot_df_epoch = dfs_epoch[[preferred_sim_x] + cols_here_epoch].copy()
						x_enc_epoch = preferred_sim_x
					else:
						plot_df_epoch = dfs_epoch[cols_here_epoch].copy()
						plot_df_epoch.insert(0, "index", range(len(plot_df_epoch)))
						x_enc_epoch = "index"

					# Label each chart clearly with the set size and epoch.
					st.markdown(f"**Simulation set {sim_set}, epoch {epoch}**")
					_plot_lines(plot_df_epoch, x_enc_epoch, height=260, y_ref=1000.0)

	# Download (from the filtered view, without helper columns)
	csv_buf = io.StringIO()
	fdf.to_csv(csv_buf, index=False)
	st.download_button("Download CSV", data=csv_buf.getvalue(), file_name="run_params_comparison.csv", mime="text/csv")

	# Determine which timestamps to show in the viewer: prefer current grid
	# selection, then fall back to last_grid_selection, then sidebar-based
	# selection.
	runs_for_view: List[str] = []
	if selected_current:
		runs_for_view = list(dict.fromkeys(str(r) for r in selected_current))
	else:
		persisted = st.session_state.get("last_grid_selection", []) or []
		if persisted:
			runs_for_view = list(dict.fromkeys(str(r) for r in persisted))
		else:
			runs_for_view = sorted(list(selected_set))

	# Render the viewer panel at the bottom of the main page
	_viewer_panel(runs_for_view)


if __name__ == "__main__":
	_run_as_script()
