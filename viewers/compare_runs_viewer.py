import os
import sys
from pathlib import Path

# Add project root to sys.path to allow imports from sibling packages like 'database'
root_path = Path(__file__).resolve().parent.parent
if str(root_path) not in sys.path:
	sys.path.insert(0, str(root_path))

import streamlit as st
import pandas as pd
import numpy as np
import io
import json
import re
import traceback
from math import ceil, sqrt
from typing import Dict, List, Any, Tuple

from simulate_trading import run_simulation_from_saved_scores, generate_simulations_from_directories, get_single_run_directory

import plotly.express as px
import plotly.graph_objects as go


from viewers.compare_runs_shared import (
	build_run_families,
	compute_selected_timestamps,
	group_runs_by_timestamp,
	render_common_params,
	scan_runs,
	sidebar_family_and_build_selector,
	has_any_simulation_files,
	load_preferences,
	save_preference,
	render_run_grid,
	discover_best_run_dir,
	RunSelectorSidebar,
)
from database.db_connect import get_connection



@st.cache_data
def _fetch_market_deltas(min_date_id: int, max_date_id: int) -> Dict[int, List[float]]:
	"""Fetch per-date market returns using daily_delta_stock_data.change_in_price_over_day.

	Returns
	-------
	Dict[int, List[float]]
		Mapping of ``date_id -> list of change_in_price_over_day`` values.
	"""
	import time
	start_t = time.time()
	print(f"[_fetch_market_deltas] fetching {min_date_id} to {max_date_id}")
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
		params = (min_date_id, max_date_id)
		cur.execute(query, params)
		rows = cur.fetchall()
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
		print(f"[_fetch_market_deltas] took {time.time() - start_t:.2f}s")
		return results
	except Exception as e:
		print(f"[_fetch_market_deltas] Error fetching market data: {e}")
		traceback.print_exc()
		return {}
	finally:
		try:
			if conn:
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
		conn = get_connection(dbinfo_file="dbinfo.id")
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

		cur.execute(query  )  #, params)
		rows = cur.fetchall()

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

	# 1) Get preferences
	prefs = load_preferences()

	st.set_page_config(page_title="Training Runs Viewer", layout="wide")

	# Use the shared RunSelectorSidebar
	selector = RunSelectorSidebar(key_prefix="crv")
	selected_runs = selector.render()

	if selected_runs is None:
		st.info("Select filters in the sidebar and click 'Show Runs' to compare.")
		return

	base_dir = st.session_state["crv_base_dir"]
	runs = scan_runs(base_dir)

	# Build a mapping from timestamp to run info for the selected runs
	ts_to_runs = group_runs_by_timestamp(runs)

	# Derive "run families" based on the first subdirectory level for the WHOLE dataset
	# because the sidebar selector might still need them, or the user might want to see
	# the context of families even if only some runs are selected.
	family_to_ts, family_names = build_run_families(
		runs,
		require_multiple_param_sets=False,
	)
	timestamp_names: List[str] = sorted(ts_to_runs.keys(), reverse=True)

	# Filter ts_to_runs to only include selected_runs for the main display
	filtered_ts_to_runs = {ts: ts_to_runs[ts] for ts in selected_runs if ts in ts_to_runs}
	ts_to_runs = filtered_ts_to_runs
	# Update timestamp_names to only include those in the filtered set for the rest of the UI
	timestamp_names = sorted(ts_to_runs.keys(), reverse=True)

	st.title("Compare Training Runs")
	st.caption("Scan a data folder of timestamped run directories and compare run parameters.")

	# ------------------------------------------------------------------
	# Navigation support from dashboard
	# ------------------------------------------------------------------
	fam_key = "crv_families"
	if fam_key in st.session_state:
		if "crv_families_init" not in st.session_state:
			st.session_state["crv_families_init"] = True

		# If the family selection changed, we want to re-trigger the auto-population of builds.
		last_fam_key = "crv_last_families_selection"
		if last_fam_key not in st.session_state:
			st.session_state[last_fam_key] = st.session_state[fam_key]
		elif st.session_state[last_fam_key] != st.session_state[fam_key]:
			st.session_state["crv_families_init"] = True
			st.session_state[last_fam_key] = st.session_state[fam_key]

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
	# Sidebar selection: families + builds (shared across viewers)
	# ------------------------------------------------------------------
	st.sidebar.button("Refresh runs list")
	st.sidebar.subheader("Select families and builds")

	def update_list_of_runs_selected(selected_runs: List[str]) -> None:
		# Keep last sidebar selection for other panels in this viewer.
		st.session_state["crv_runs_selected"] = list(selected_runs)

	res_runs, selected_families, _available_builds = sidebar_family_and_build_selector(
		timestamp_names=timestamp_names,
		family_names=family_names,
		family_to_ts=family_to_ts,
		key_prefix="crv",
		default_select_all_builds=False,
		empty_builds_means_all=False,
		on_runs_updated=update_list_of_runs_selected,
	)

	selected_builds = res_runs

	# Do not populate the main window until at least one family is selected.
	if not selected_families:
		st.info("Select one or more run families in the sidebar to begin.")
		return

	selected_set = set(selected_builds)

	# Allow empty selection: we'll still render the grid to let users pick rows there
	if not selected_set:
		st.info("Tip: Select rows in the grid below or use the sidebar pickers.")

	# Prepare selected timestamp "run" entries for the comparison table.
	# When "display extending info" is checked, we show every individual rep.
	# Otherwise, we collapse each logical timestamp's underlying reps to their
	# common parameters.
	show_extended_key = "display_extending_info"
	if show_extended_key not in st.session_state:
		st.session_state[show_extended_key] = prefs.get(show_extended_key, False)
	show_extended = st.session_state[show_extended_key]

	display_rows: List[Dict[str, Any]] = []
	for ts_name in timestamp_names:
		if ts_name not in selected_set:
			continue

		run_list = ts_to_runs.get(ts_name, [])
		if not run_list:
			continue

		if show_extended:
			# Show each rep as its own row
			for rel_dir, params in run_list:
				row = dict(params)
				row["_ts"] = ts_name
				row["_dir"] = rel_dir
				display_rows.append(row)
		else:
			# Show only the logical single run (ensemble) for this timestamp.
			# We prefer a directory that contains simulation files at the top level (ensemble).
			best_dir = discover_best_run_dir(base_dir, ts_name, run_list)

			# Map back to params
			if best_dir:
				rel_best = os.path.relpath(best_dir, base_dir)
				# Try to find exactly matching rep or just use first if it's the ensemble dir
				match = next((p for d, p in run_list if d == rel_best), None)
				if not match:
					match = _compute_common_params([p for _, p in run_list])

				row = dict(match)
				row["_ts"] = ts_name
				row["_dir"] = rel_best
				display_rows.append(row)
			else:
				# Fallback: Collapse reps to common parameters
				run_params = [p for _, p in run_list]
				common_p = _compute_common_params(run_params)
				row = dict(common_p)
				row["_ts"] = ts_name
				row.setdefault("_dir", run_list[0][0])
				display_rows.append(row)

	if not display_rows:
		st.info("No runs selected or available for the current criteria.")
		return

	# Compute common vs varying across all displayed rows
	common = _compute_common_params(display_rows)
	common_keys = set(common.keys())

	render_common_params(common, title="Common parameters across runs", expanded=False)

	st.subheader("Run parameters (differences)")

	exclude = ["_dir", *common_keys]
	rows = _to_dataframe_dicts(display_rows, exclude)
	df = pd.DataFrame(rows)

	# Standard grid rendering
	selected_runs_from_grid = render_run_grid(
		df=df,
		key_prefix="crv",
		prefs=prefs,
		default_visible_cols=["run", "run_timestamp", "epochs", "epoch"] + [c for c in df.columns if c not in ["run", "run_timestamp", "epochs", "epoch", "_variant", "_ts", "run_id", "activation", "final_activation", "optimizer", "optimizer_list"]],
		return_all_if_none=False
	)

	col_sim_1, col_sim_2 = st.columns([1, 0.5])
	with col_sim_1:
		force_sim = col_sim_2.checkbox("force", help="Force the regeneration of all selected simulations", key="crv_force_sim")
		if st.button("Generate simulations from saved scores", help="For selected runs, look for saved scores and generate stock_simulation_*.csv files"):
			if not selected_runs_from_grid:
				st.warning("Please select at least one run in the table above.")
			else:
				status_text = st.empty()

				# We need the full paths for selected runs.
				# selected_runs_from_grid are IDs from the grid (usually timestamp names or _dir).
				paths_to_process = []
				for rid in selected_runs_from_grid:
					# Find the original row to get the full _dir
					match = next((r for r in display_rows if (r.get("_ts") or r.get("_dir")) == rid), None)
					if match and match.get("_dir"):
						p = os.path.normpath(os.path.join(base_dir, match["_dir"]))
						if os.path.isdir(p):
							# Ensure we only process single run (ensemble) directories
							p = get_single_run_directory(p)
							if p not in paths_to_process:
								paths_to_process.append(p)
					else:
						# Fallback to direct join if not found in display_rows
						p = os.path.normpath(os.path.join(base_dir, rid))
						if os.path.isdir(p):
							p = get_single_run_directory(p)
							if p not in paths_to_process:
								paths_to_process.append(p)

				if not paths_to_process:
					st.warning(f"No directories found for selection: {selected_runs_from_grid}")
				else:
					status_text.text(f"Generating simulations for {len(paths_to_process)} directories...")
					try:
						generate_simulations_from_directories(paths_to_process, top_k=[1, 3, 5, 10], force=force_sim)
					except Exception as e:
						st.error(f"Error during simulation generation: {e}")
						traceback.print_exc()

					status_text.text("Done! Refreshing viewer...")
					st.rerun()

	selected_runs = selected_runs_from_grid

	# Map selected_runs back to the full parameter dictionaries
	chosen_params = []
	for rid in selected_runs:
		for r in display_rows:
			if (r.get("run") == rid or r.get("run_id") == rid or r.get("_ts") == rid or r.get("_dir") == rid):
				chosen_params.append(r)
				break
	if not chosen_params:
		# Fallback to all if nothing selected
		chosen_params = display_rows

	# --- Selection change detection --------------------------------------
	#
	# Streamlit reruns the script on every interaction. To notice when the
	# grid selection actually changes, we keep the last seen selection in
	# session_state and compare it with the current one.
	ss_key_runs = "crv_last_grid_runs"
	prev_runs = list(st.session_state.get(ss_key_runs, []))  # type: ignore[index]
	curr_runs = [str(r) for r in selected_runs]

	if curr_runs != prev_runs:
		st.session_state[ss_key_runs] = curr_runs
		# Whenever the grid selection changes, update the set of runs that the
		# metrics viewer at the bottom should focus on.
		st.session_state["last_grid_selection"] = curr_runs
		# Reset graphs populated state so we wait for the next button click
		st.session_state["crv_graphs_populated"] = False

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
			if k.startswith("_") or k in ["activation_list", "final_activation_list"]:
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

	def _viewer_panel(selected_runs: List[str]):
		"""Render the metrics and stock simulation viewer.

		The selection coming into this panel is the list of identifiers from the
		central grid (either timestamps or rep paths).
		"""

		st.header("Metrics viewer")
		if not selected_runs:
			st.info("No runs selected for viewing yet. Use the sidebar or the grid above to pick some runs.")
			return

		# Collect params for the selected runs.
		chosen_params: List[Dict[str, Any]] = []
		for run_id in selected_runs:
			# Look for exact match in display_rows (which contains either collapsed TS or individual reps)
			match = None
			for r in display_rows:
				if (r.get("_ts") or r.get("_dir")) == run_id:
					match = r
					break

			if match:
				# If we're looking at a collapsed TS, expand to its reps
				# to preserve the original multi-rep chart behavior.
				ts_name = match.get("_ts")
				is_collapsed = (match.get("_dir") == ts_to_runs.get(ts_name, [("", {})])[0][0]) if ts_name in ts_to_runs else False
				# Actually, the simple way to know if it's collapsed is checking display_rows vs ts_to_runs.
				# But wait, if show_extended is OFF, display_rows has one entry per TS.
				# If show_extended is ON, display_rows has one entry per rep.

				if not show_extended:
					# It's a collapsed TS entry. Expand to all reps.
					run_list = ts_to_runs.get(str(ts_name), [])
					for _name, p in run_list:
						chosen_params.append(p)
				else:
					# It's an individual rep entry. Just add it.
					chosen_params.append(match)
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
		with st.expander("Metrics to show", expanded=True):
			# Global toggle for logarithmic Y-axis across all charts in the viewer.
			# Persisted via session_state so it survives tab switches and reruns.
			if "viewer_log_y" not in st.session_state:
				st.session_state["viewer_log_y"] = prefs.get("viewer_log_y", False)
			log_y = st.checkbox(
				"Logarithmic Y-axis",
				value=st.session_state["viewer_log_y"],
				key="viewer_log_y",
				on_change=lambda: save_preference("viewer_log_y", st.session_state["viewer_log_y"]),
			)

			# Number of initial points to ignore from the beginning of each run's series.
			# Implemented as a small dropdown (0..20) so it's quick to adjust and consistent
			# across all charts. Persisted in session_state.
			if "viewer_ignore_head" not in st.session_state:
				st.session_state["viewer_ignore_head"] = prefs.get("viewer_ignore_head", 0)
			ignore_head_val = int(st.session_state["viewer_ignore_head"] or 0)
			ignore_head_val = max(0, min(20, ignore_head_val))
			ignore_head = st.selectbox(
				"Ignore first N points",
				options=list(range(0, 21)),
				index=ignore_head_val,
				help="Number of leading data points to drop from each run before plotting.",
				key="viewer_ignore_head",
				#width=100,  # selectbox doesn't take width
				on_change=lambda: save_preference("viewer_ignore_head", st.session_state["viewer_ignore_head"]),
			)

			# Layout toggle: either one chart per run (original behaviour) or
			# a single combined chart that overlays all selected runs to make
			# comparisons easier.
			if "viewer_single_chart" not in st.session_state:
				st.session_state["viewer_single_chart"] = prefs.get("viewer_single_chart", False)
			single_chart = st.checkbox(
				"Single chart (overlay all selected runs)",
				value=st.session_state["viewer_single_chart"],
				key="viewer_single_chart",
				help=(
					"When enabled, show all selected runs together in a single chart. "
					"Each run/metric pair will appear as a separate line in the legend."
				),
				on_change=lambda: save_preference("viewer_single_chart", st.session_state["viewer_single_chart"]),
			)

			# Sum rep errors toggle: when multiple reps exist for a given run configuration,
			# this option aggregates their error values at each point and graphs the sum
			# rather than individual lines. This makes it easier to compare different
			# hyperparameters when repetitions are used.
			if "viewer_sum_rep_errors" not in st.session_state:
				st.session_state["viewer_sum_rep_errors"] = prefs.get("viewer_sum_rep_errors", False)
			sum_rep_errors = st.checkbox(
				"Sum rep errors",
				value=st.session_state["viewer_sum_rep_errors"],
				key="viewer_sum_rep_errors",
				help="Sum metrics containing 'mse', 'loss', or 'error' across repetitions for each run.",
				on_change=lambda: save_preference("viewer_sum_rep_errors", st.session_state["viewer_sum_rep_errors"]),
			)

			if "viewer_linear_interp" not in st.session_state:
				st.session_state["viewer_linear_interp"] = prefs.get("viewer_linear_interp", False)
			linear_interp = st.checkbox(
				"Linear interpolation",
				value=st.session_state["viewer_linear_interp"],
				key="viewer_linear_interp",
				help="Display linear interpolation of the graphs based on the final 10% of displayed points.",
				on_change=lambda: save_preference("viewer_linear_interp", st.session_state["viewer_linear_interp"]),
			)

			if not all_metric_cols:
				st.warning("No metric CSVs found for selected runs. Ensure each run contains a *_training_metrics.csv file.")
			# Initialize missing keys from preferences
			for col in all_metric_cols:
				if col == x_axis:
					continue
				key = f"metric_check_{col}"
				if key not in st.session_state:
					# Default current_learning_rate to False so it doesn't squash the MSE curves by default
					if col == "current_learning_rate":
						st.session_state[key] = prefs.get(key, False)
					else:
						st.session_state[key] = prefs.get(key, True)

			# Render checkboxes in 4 columns
			if all_metric_cols:
				cols = st.columns(4)
				for i, col in enumerate([c for c in all_metric_cols if c != x_axis]):
					with cols[i % 4]:
						key = f"metric_check_{col}"
						if key not in st.session_state:
							st.session_state[key] = prefs.get(key, True)
						st.checkbox(
							col,
							key=key,
							value=st.session_state[key],
							on_change=lambda k=key: save_preference(k, st.session_state[k]),
						)

			selected_metrics = [c for c in all_metric_cols if c != x_axis and st.session_state.get(f"metric_check_{c}", False)]

		if not selected_metrics:
			st.info("No metrics selected. Check one or more metric types above to display graphs.")
			return

		# ------------------------------------------------------------------
		# Run comparison (replicate summary)
		# ------------------------------------------------------------------
		with st.expander("Metric table", expanded=True):
			if not all_metric_cols:
				st.info("No metrics available for comparison yet.")
			else:
				import numpy as np
				import pandas as pd

				comp_metric_candidates = [c for c in all_metric_cols if c != x_axis]
				default_metric = None
				for key in ["mse", "loss", "error", "err", "rank"]:
					for cand in comp_metric_candidates:
						if key in str(cand).lower():
							default_metric = cand
							break
						if default_metric:
							break
				if default_metric is None and comp_metric_candidates:
					default_metric = comp_metric_candidates[0]

				comp_col, summary_col, direction_col = st.columns([2, 2, 2])
				with comp_col:
					comparison_metric = st.selectbox(
						"Metric",
						options=comp_metric_candidates,
						index=comp_metric_candidates.index(default_metric) if default_metric in comp_metric_candidates else 0,
						help="Metric to summarise per replicate and compare across argument sets.",
					)
				with summary_col:
					summary_mode = st.selectbox(
						"Summary",
						options=["final", "mean_tail", "min", "max"],
						format_func=lambda v: {
							"final": "Final value",
							"mean_tail": "Mean of last N",
							"min": "Minimum",
							"max": "Maximum",
						}.get(v, v),
						help="How to summarise each replicate's metric series.",
					)
				with direction_col:
					default_lower_better = any(k in str(comparison_metric).lower() for k in ["mse", "loss", "error", "err"]) if comparison_metric else True
					lower_is_better = st.checkbox(
						"Lower is better",
						value=default_lower_better,
						help="Toggle when higher values indicate better performance.",
					)

				tail_n = 5
				if summary_mode == "mean_tail":
					tail_n = st.selectbox(
						"Tail window (N)",
						options=list(range(3, 31)),
						index=2,
						help="Number of most recent points to average for each replicate.",
					)

				def _summarise_series(series) -> float | None:
					if series is None or series.empty:
						return None
					try:
						if summary_mode == "final":
							return float(series.iloc[-1])
						if summary_mode == "min":
							return float(series.min())
						if summary_mode == "max":
							return float(series.max())
						return float(series.tail(int(tail_n)).mean())
					except Exception:
						return None

				def _bootstrap_diff(a_vals: List[float], b_vals: List[float], n_boot: int = 1000) -> Tuple[float | None, Tuple[float | None, float | None]]:
					if not a_vals or not b_vals:
						return None, (None, None)
					rng = np.random.default_rng(0)
					a = np.asarray(a_vals, dtype=float)
					b = np.asarray(b_vals, dtype=float)
					diffs = np.empty(n_boot, dtype=float)
					for i in range(n_boot):
						a_s = rng.choice(a, size=len(a), replace=True)
						b_s = rng.choice(b, size=len(b), replace=True)
						diffs[i] = float(np.mean(a_s) - np.mean(b_s))
					ci_low, ci_high = np.percentile(diffs, [2.5, 97.5])
					if lower_is_better:
						confidence = float(np.mean(diffs > 0))
					else:
						confidence = float(np.mean(diffs < 0))
					return confidence, (float(ci_low), float(ci_high))

			run_summaries: List[Dict[str, Any]] = []
			for run_id in selected_runs:
				rep_entries: List[Tuple[str, Dict[str, Any]]] = []
				if run_id in ts_to_runs:
					rep_entries = list(ts_to_runs.get(run_id, []))
				else:
					for _ts_name, run_list in ts_to_runs.items():
						for rel_dir, params in run_list:
							if rel_dir == run_id:
								rep_entries = [(rel_dir, params)]
								break
						if rep_entries:
							break

				if not rep_entries:
					continue

				param_list = [p for _, p in rep_entries]
				if len(param_list) > 1:
					label_params = dict(_compute_common_params(param_list)) or dict(param_list[0])
				else:
					label_params = dict(param_list[0])
				label_params.setdefault("_ts", label_params.get("_ts") or run_id)
				label_params.setdefault("_dir", rep_entries[0][0])
				label = _build_title(label_params, diff_keys)

				rep_values: List[float] = []
				missing_metric = 0
				for rel_dir, _params in rep_entries:
					dfm = _load_metrics_df(str(rel_dir))
					if dfm is None or comparison_metric not in dfm.columns:
						missing_metric += 1
						continue
					series = dfm[comparison_metric].dropna()
					try:
						trim_n = int(ignore_head or 0)
					except Exception:
						trim_n = 0
					if trim_n > 0 and len(series) > trim_n:
						series = series.iloc[trim_n:]
					val = _summarise_series(series)
					if val is not None:
						rep_values.append(val)

				if not rep_values:
					continue

				mean_val = float(np.mean(rep_values))
				std_val = float(np.std(rep_values, ddof=1)) if len(rep_values) > 1 else 0.0
				run_summaries.append({
					"run": label,
					"run_id": run_id,
					"rep_count": len(rep_values),
					"mean": mean_val,
					"std": std_val,
					"values": rep_values,
					"missing": missing_metric,
				})

			if not run_summaries:
				st.info("No replicate metrics found for the current selection.")
			else:
				best_idx = int(np.argmin([r["mean"] for r in run_summaries]) if lower_is_better else np.argmax([r["mean"] for r in run_summaries]))
				best_run = run_summaries[best_idx]
				best_mean = best_run["mean"]

				rows = []
				for idx, r in enumerate(run_summaries):
					delta = float(r["mean"] - best_mean)
					rel_delta = None
					if best_mean != 0:
						rel_delta = float(delta / abs(best_mean))
					confidence, (ci_low, ci_high) = (None, (None, None))
					if idx != best_idx:
						confidence, (ci_low, ci_high) = _bootstrap_diff(r["values"], best_run["values"])
					else:
						confidence, (ci_low, ci_high) = (1.0, (0.0, 0.0))
					rows.append({
						"run": r["run"],
						"reps": r["rep_count"],
						"mean": r["mean"],
						"std": r["std"],
						"delta_vs_best": delta,
						"delta_pct": rel_delta,
						"diff_ci_95": None if ci_low is None else f"[{ci_low:.4g}, {ci_high:.4g}]",
						"confidence_best_better": confidence,
						"missing_reps": r["missing"],
					})

				comp_df = pd.DataFrame(rows)
				st.dataframe(
					comp_df,
					width='stretch',
				)
				st.caption(
					"Confidence is estimated via bootstrap on replicate means. "
					"Higher values indicate more certainty that the best run outperforms the comparator."
				)

		# Render charts in n x n grid
		n = len(metric_dfs)
		if sum_rep_errors:
			import pandas as pd # Ensure pandas is available for grouping and merging

			# Group by logical run (timestamp) and sum their metrics
			grouped_dfs: Dict[str, List[Tuple[Dict[str, Any], pd.DataFrame]]] = {}
			for p, dfm in metric_dfs:
				if dfm is not None:
					ts = p.get("_ts") or p.get("_dir") or "unknown"
					grouped_dfs.setdefault(str(ts), []).append((p, dfm))

			aggregated_metric_dfs: List[Tuple[Dict[str, Any], pd.DataFrame]] = []
			for ts, group in grouped_dfs.items():
				if not group:
					continue
				# Use the first rep's params as a template
				base_p = dict(group[0][0])
				base_p["_is_summed_reps"] = True
				base_p["_rep_count"] = len(group)

				# Summing logic: we need a unified X axis (usually 'epoch')
				if x_axis is None:
					# If no X axis, we can't easily align/sum without it. Just use the first one as fallback
					aggregated_metric_dfs.append(group[0])
					continue

				import pandas as pd # local import to ensure it's available

				# Merge all dataframes in the group on the x_axis
				merged = group[0][1].copy()
				for i in range(1, len(group)):
					other_df = group[i][1]
					# Outer join to handle varying lengths/epochs
					merged = pd.merge(merged, other_df, on=x_axis, how="outer", suffixes=("", f"_rep{i+1}"))

				# Identify which columns to sum
				sum_cols = [c for c in selected_metrics if any(sub in c.lower() for sub in ["mse", "loss", "error"])]
				final_df = pd.DataFrame({x_axis: merged[x_axis]})
				for col in selected_metrics:
					if col in sum_cols:
						# Sum all versions of this column
						rep_cols = [col] + [f"{col}_rep{i+1}" for i in range(1, len(group)) if f"{col}_rep{i+1}" in merged.columns]
						final_df[col] = merged[rep_cols].sum(axis=1)
					else:
						# For non-error metrics (like LR or top10), take the mean
						rep_cols = [col] + [f"{col}_rep{i+1}" for i in range(1, len(group)) if f"{col}_rep{i+1}" in merged.columns]
						final_df[col] = merged[rep_cols].mean(axis=1)

				aggregated_metric_dfs.append((base_p, final_df.sort_values(by=x_axis)))

			metric_dfs_to_plot = aggregated_metric_dfs
		else:
			metric_dfs_to_plot = metric_dfs

		n = len(metric_dfs_to_plot)
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

			for p, dfm in metric_dfs_to_plot:
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

					if linear_interp:
						try:
							# Linear interpolation: y = mx + c
							# Based on displayed points (respects ignore_head)
							valid_pts = [(x, y) for x, y in zip(local_x, y_vals) if x is not None and y is not None]
							if len(valid_pts) >= 2:
								import numpy as np
								# Use only the final 10% of points for the fit
								fit_pts = valid_pts[-max(2, len(valid_pts) // 10):]
								xs_fit = np.array([p[0] for p in fit_pts])
								ys_fit = np.array([p[1] for p in fit_pts])
								# Fit a first-degree polynomial (linear)
								coeffs = np.polyfit(xs_fit, ys_fit, 1)
								poly = np.poly1d(coeffs)
								# Generate interpolation values across the WHOLE displayed x range
								xs_arr = np.array([p[0] for p in valid_pts])
								y_interp = poly(xs_arr).tolist()
								series.append((local_x, y_interp, f"{label} (linear interp)"))
						except Exception as e:
							print(f"[compare_runs_viewer] Linear interp failed for {label}: {e}")

			if not series:
				st.caption("No data available to render a combined chart with the current selection.")
				return

			# Prefer Plotly for rich interactive comparison.
			try:
				import plotly.graph_objects as go  # type: ignore
				from plotly.subplots import make_subplots # type: ignore

				# Check if we should use a secondary Y-axis (e.g. for learning rate)
				# We always use the dual-axis Plotly chart to keep a consistent graph type
				use_secondary_y = True

				if use_secondary_y:
					fig = make_subplots(specs=[[{"secondary_y": True}]])
				else:
					fig = go.Figure()

				for xs, ys, label in series:
					is_lr = label.endswith(": current_learning_rate")
					if use_secondary_y:
						fig.add_trace(
							go.Scatter(x=xs, y=ys, mode="lines", name=label),
							secondary_y=is_lr
						)
					else:
						fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name=label))

				fig.update_layout(
					height=360,
					margin=dict(l=10, r=10, t=10, b=10),
					legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
					xaxis_title=x_axis or "index",
				)

				fig.update_yaxes(title_text="MSE / Metrics", secondary_y=False)
				if use_secondary_y:
					fig.update_yaxes(title_text="Learning Rate", secondary_y=True)

				if log_y:
					fig.update_yaxes(type="log", secondary_y=False)
					if use_secondary_y:
						# Keep LR linear usually, but could be log too.
						# Let's keep it consistent with the user's choice for now.
						fig.update_yaxes(type="log", secondary_y=True)
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
				from plotly.subplots import make_subplots # type: ignore

				# Check if we should use a secondary Y-axis (e.g. for learning rate)
				# We always use the dual-axis Plotly chart to keep a consistent graph type
				use_secondary_y = True

				if use_secondary_y:
					fig = make_subplots(specs=[[{"secondary_y": True}]])
				else:
					fig = go.Figure()

				for col in [c for c in plot_df.columns if c != x_enc]:
					is_lr = (col == "current_learning_rate")
					if use_secondary_y:
						fig.add_trace(
							go.Scatter(x=plot_df[x_enc], y=plot_df[col], mode="lines", name=str(col)),
							secondary_y=is_lr
						)
					else:
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

				fig.update_yaxes(title_text="Metrics", secondary_y=False)
				if use_secondary_y:
					fig.update_yaxes(title_text="Learning Rate", secondary_y=True)

				if log_y:
					# Use logarithmic scale on Y axis; non-positive values will be ignored by Plotly.
					fig.update_yaxes(type="log", secondary_y=False)
					if use_secondary_y:
						fig.update_yaxes(type="log", secondary_y=True)
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
			for idx, (p, dfm) in enumerate(metric_dfs_to_plot):
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

					if linear_interp:
						try:
							import numpy as np
							new_cols = {}
							for col in [m for m in selected_metrics if m in plot_df.columns]:
								# Linear interpolation: y = mx + c
								valid_pts = plot_df[[x_enc, col]].dropna()
								if len(valid_pts) >= 2:
									# Use only the final 10% of points for the fit
									fit_pts = valid_pts.tail(max(2, len(valid_pts) // 10))
									xs_fit = fit_pts[x_enc].values
									ys_fit = fit_pts[col].values
									coeffs = np.polyfit(xs_fit, ys_fit, 1)
									poly = np.poly1d(coeffs)
									# Generate interpolation values across the WHOLE displayed range
									new_cols[f"{col} (linear interp)"] = poly(plot_df[x_enc].values)
							if new_cols:
								plot_df = pd.concat([plot_df, pd.DataFrame(new_cols, index=plot_df.index)], axis=1)
						except Exception as e:
							print(f"[compare_runs_viewer] Linear interp failed in grid charts: {e}")

					# Check for secondary Y axis support
					# We always use the dual-axis chart type (via fallback to _plot_lines) to keep it consistent
					use_secondary_y = True

					# Melt to long for Altair
					try:
						# If we have secondary Y, we always use the fallback to _plot_lines (Plotly)
						# to keep the graph type consistent with the dual-axis Plotly implementation
						if use_secondary_y:
							_plot_lines(plot_df, x_enc, height=260)
							continue

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

			for name, params in runs:
				dname = params.get("_dir")
				if not dname:
					continue

				parts = re.split(r'[\\/]', str(dname))
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

				# Derive the timestamp directory relative to base_dir.
				# In the new structure, we might have run_family_.../run_single_.../repN
				# We want the directory that contains the reps (e.g. run_single_...)
				ts_rel = os.path.sep.join(parts[:-1])
				if not ts_rel:
					continue
				ts_path = os.path.join(base_dir, ts_rel)

				# Also check the parent of THAT directory if it's not a family directory
				# but might contain ensemble results (legacy or different layout)
				search_paths = [ts_path]
				if len(parts) > 2:
					parent_rel = os.path.sep.join(parts[:-2])
					parent_path = os.path.join(base_dir, parent_rel)
					if os.path.isdir(parent_path):
						search_paths.append(parent_path)

				best_csv_path = None
				best_epoch = None
				try:
					for spath in search_paths:
						for fname in os.listdir(spath):
							# Match the current naming convention
							#   stock_simulation_*_epoch_<epoch>.csv
							# OR ensemble naming:
							#   stock_simulation_ensemble_*_epoch_<epoch>.csv
							m = re.match(r"stock_simulation_(?:ensemble_)?.*_epoch_(\d+)\.csv$", fname)
							if not m:
								continue
							epoch_idx = int(m.group(1))
							if best_epoch is None or epoch_idx > best_epoch:
								best_epoch = epoch_idx
								best_csv_path = os.path.join(spath, fname)
				except FileNotFoundError:
					best_csv_path = None

				if not best_csv_path or not os.path.exists(best_csv_path):
					continue

				try:
					dfs_ens = pd.read_csv(best_csv_path)
				except Exception as e:
					print(f"[stock_sim] failed to read ensemble csv {best_csv_path}: {e}")
					dfs_ens = None

				ensemble_per_ts[ts_name] = dfs_ens

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
				# logical identifier, but if it's already a timestamp-like name, keep it
				ens_params["_dir"] = ts_name
				ens_params["_is_ensemble"] = True
				ens_params["_timestamp"] = ts_name
				# Make it obvious in the title that this line is the
				# ensemble/aggregate across reps.
				ens_params.setdefault("rep", "ensemble")

				# Inject the _family if it exists in the original params
				if "_family" in p:
					ens_params["_family"] = p["_family"]

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
							m = re.match(r"stock_simulation_(?:ensemble_)?.*_epoch_(\d+)\.csv$", fname)
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

							# Collect numeric columns from this rep so they are available
							# even if the ensemble file didn't have them.
							rep_cols = [
								c for c in rep_dfs.columns
								if c not in ["feature_date_id", "open_date_id", "close_date_id"]
								and (rep_dfs[c].dtype.kind in "if")
							]
							all_top_cols = list(sorted(set(all_top_cols + rep_cols)))
						except Exception:
							rep_dfs = None

					# Use run_params so per-rep parameters (e.g. rep id) are
					# reflected in titles and downstream logic.
					# We explicitly inject 'rep' from the directory name if missing.
					p_copy = dict(run_params)
					parts = re.split(r'[\\/]', str(p_copy.get("_dir", "")))
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
						m = re.match(r"stock_simulation_(?:ensemble_)?.*_epoch_(\d+)\.csv$", fname)
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

		# Initialize missing keys from preferences
		for col in sim_cols:
			key = f"stock_sim_col_{col}"
			if key not in st.session_state:
				st.session_state[key] = prefs.get(key, True)

		# Ensemble method visibility controls (Sum, Norm, Rank)
		method_suffix_map = {
			"Sum": " (Sum)",
			"Norm": " (Norm)",
			"Rank": " (Rank)",
		}
		available_methods = [
			m for m, suffix in method_suffix_map.items() if any(c.endswith(suffix) for c in sim_cols)
		]
		if available_methods:
			for method in available_methods:
				key = f"stock_sim_method_{method.lower()}"
				if key not in st.session_state:
					st.session_state[key] = prefs.get(key, True)

			st.markdown("**Ensemble methods to show**")
			method_cols = st.columns(len(available_methods))
			for idx, method in enumerate(available_methods):
				key = f"stock_sim_method_{method.lower()}"
				with method_cols[idx]:
					st.checkbox(
						method,
						key=key,
						value=st.session_state[key],
						on_change=lambda k=key: save_preference(k, st.session_state[k]),
					)

		if f"stock_sim_show_cdf" not in st.session_state:
			st.session_state["stock_sim_show_cdf"] = prefs.get("stock_sim_show_cdf", True)

		st.markdown("**Stock simulation columns to show**")
		if st.button(
			"Select all",
			key="stock_sim_select_all_btn",
			help="Check all stock simulation column boxes and show CDF overlays.",
		):
			for col in sim_cols:
				key = f"stock_sim_col_{col}"
				st.session_state[key] = True
				save_preference(key, True)
			st.session_state["stock_sim_show_cdf"] = True
			save_preference("stock_sim_show_cdf", True)

		# Render individual checkboxes in a compact multi-column layout.
		if sim_cols:
			cols_ui = st.columns(4)
			for idx, col in enumerate(sim_cols):
				with cols_ui[idx % 4]:
					key = f"stock_sim_col_{col}"
					if key not in st.session_state:
						st.session_state[key] = prefs.get(key, True)
					st.checkbox(
						col,
						key=key,
						value=st.session_state[key],
						on_change=lambda k=key: save_preference(k, st.session_state[k]),
					)

		def _method_enabled_for_col(col_name: str) -> bool:
			for method_name, suffix in method_suffix_map.items():
				if col_name.endswith(suffix):
					key = f"stock_sim_method_{method_name.lower()}"
					return st.session_state.get(key, True)
			return True

		# Final list of columns that are actually enabled for plotting.
		selected_sim_cols = [
			c
			for c in sim_cols
			if st.session_state.get(f"stock_sim_col_{c}", False) and _method_enabled_for_col(c)
		]

		# Bin width for histograms of day-over-day change ratios in the
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
				"For example, 0.01 groups changes in 1% increments."
			),
		)

		# Independent control for whether CDF overlays are shown on the
		# per-run stock simulation charts. This ensures the CDF lines are
		# also user-controllable, as requested.
		if "stock_sim_show_cdf" not in st.session_state:
			st.session_state["stock_sim_show_cdf"] = prefs.get("stock_sim_show_cdf", True)

		show_cdf = st.checkbox(
			"Show CDF overlays",
			key="stock_sim_show_cdf",
			value=st.session_state["stock_sim_show_cdf"],
			help=(
				"When enabled, overlay CDF percentile curves alongside portfolio values "
				"for each stock simulation run."
			),
			on_change=lambda: save_preference("stock_sim_show_cdf", st.session_state["stock_sim_show_cdf"]),
		)

		# Independent control for combining all *_top curves on a single chart.
		if "stock_sim_single_chart" not in st.session_state:
			st.session_state["stock_sim_single_chart"] = prefs.get("stock_sim_single_chart", False)

		sim_single_chart = st.checkbox(
			"Display on single chart",
			key="stock_sim_single_chart",
			value=st.session_state["stock_sim_single_chart"],
			help="When enabled, overlay *_top curves from all runs on one chart.",
			on_change=lambda: save_preference("stock_sim_single_chart", st.session_state["stock_sim_single_chart"]),
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

			if selected_sim_cols:
				cols_here = [c for c in cols_here if c in selected_sim_cols]

			if not cols_here:
				if selected_sim_cols:
					st.caption(
						"None of the enabled stock simulation columns are present in this run's "
						"stock_simulation_*.csv file."
					)
				else:
					st.caption("All stock simulation columns are currently hidden.")
				continue

			# Calculate CDFs dynamically for all portfolio/percent columns
			# Filter numeric cols to exclude any existing CDF/percentile columns for the primary axis
			primary_cols = [c for c in cols_here if "percentile" not in c and "CDF" not in c]

			# Split into portfolio-value vs daily-return-percent vs rank-percent series to avoid
			# hiding small percent/rank traces against ~1000-value portfolio curves.
			percent_cols = [c for c in primary_cols if isinstance(c, str) and c.lower().startswith("return percent")]
			rank_cols = [c for c in primary_cols if isinstance(c, str) and c.lower().startswith("rank percent")]
			value_cols = [c for c in primary_cols if c not in percent_cols and c not in rank_cols]

			# Build plot_df with just primary cols first
			cols_needed = primary_cols[:]

			if preferred_sim_x in dfs.columns:
				plot_df = dfs[[preferred_sim_x] + cols_needed].copy()
				x_enc = preferred_sim_x
			else:
				plot_df = dfs[cols_needed].copy()
				plot_df.insert(0, "index", range(len(plot_df)))
				x_enc = "index"

			# Ensure percent_cols and rank_cols are numeric (coerce errors to NaN so they still plot)
			try:
				for _c in percent_cols + rank_cols:
					if _c in plot_df.columns:
						plot_df[_c] = pd.to_numeric(plot_df[_c], errors="coerce")
			except Exception:
				pass

			# Dynamic CDF calculation (optional)
			cdf_cols = []
			date_col = "open_date_id"

			if "stock_sim_show_cdf" not in st.session_state:
				st.session_state["stock_sim_show_cdf"] = prefs.get("stock_sim_show_cdf", True)

			if show_cdf and date_col in dfs.columns and not dfs.empty and value_cols:
				try:
					dates = dfs[date_col].values
					valid_dates = [int(d) for d in dates if d > 0]
					min_d = min(valid_dates) if valid_dates else None
					max_d = max(valid_dates) if valid_dates else None
					if min_d is not None and max_d is not None:
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
							market_deltas: Dict[int, List[float]] = {}
						else:
							market_deltas = _fetch_market_deltas(min_d, max_d) if (min_d is not None and max_d is not None) else {}

						ret_override = None
						if "top1_daily_return" in dfs.columns:
							try:
								ret_override = dfs["top1_daily_return"].astype(float).values
							except Exception:
								ret_override = None

						for col in value_cols:
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
				except Exception as e:
					print(f"[stock_sim] Error computing CDFs: {e}")

			has_cdf = len(cdf_cols) > 0

			try:
				import plotly.graph_objects as go
				from plotly.subplots import make_subplots

				# Always use dual-axis Plotly for consistent appearance
				fig = make_subplots(specs=[[{"secondary_y": True}]])

				# Primary: Portfolio Values
				for col in value_cols:
					y_vals = pd.to_numeric(plot_df[col], errors="coerce")
					fig.add_trace(
						go.Scatter(x=plot_df[x_enc], y=y_vals, mode="lines", name=col),
						secondary_y=False
					)

				# Secondary: Percent series (if any)
				for col in percent_cols:
					y_vals = pd.to_numeric(plot_df[col], errors="coerce")
					fig.add_trace(
						go.Scatter(x=plot_df[x_enc], y=y_vals, mode="lines", name=col),
						secondary_y=True
					)

				# Secondary: Rank series (if any)
				for col in rank_cols:
					y_vals = pd.to_numeric(plot_df[col], errors="coerce")
					fig.add_trace(
						go.Scatter(x=plot_df[x_enc], y=y_vals, mode="lines", name=col),
						secondary_y=True
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
				if has_cdf or percent_cols or rank_cols:
					fig.add_hline(y=0.5, line=dict(color="gray", dash="dash", width=1), secondary_y=True)

				fig.update_layout(
					height=350 if has_cdf else 260,
					margin=dict(l=10, r=10, t=10, b=10),
					legend=dict(orientation="h"),
				)
				fig.update_yaxes(title_text="Portfolio Value", secondary_y=False)

				if has_cdf or rank_cols:
					fig.update_yaxes(title_text="Percent / Rank / Percentile", range=[-0.05, 1.05], secondary_y=True)
				elif percent_cols:
					fig.update_yaxes(title_text="Percent", secondary_y=True)
				else:
					# Even if no secondary data, keep the axis for consistent rendering style
					fig.update_yaxes(title_text="", secondary_y=True)

				st.plotly_chart(fig, width="stretch")

			except Exception as e:
				print(f"[stock_sim] Plotly dual-axis error: {e}")
				_plot_lines(plot_df[[x_enc] + primary_cols], x_enc, height=260, y_ref=1000.0)

				# ------------------------------------------------------------------
				# Histogram of day-over-day change ratios for this run.
				#
				# We group the simulation columns by their "topN" suffix
				# (top1, top3, top5) and stack them in the histogram.
				# ------------------------------------------------------------------
			try:
				import numpy as _np  # type: ignore

				# Group columns by top1, top3, top5
				groups = ["top1", "top3", "top5"]
				group_values: dict[str, list[float]] = {g: [] for g in groups}
				other_values: list[float] = []

				for col in cols_here:
					try:
						# Coerce to numeric, dropping any non-numeric strings
						s_numeric = pd.to_numeric(dfs[col], errors="coerce")
						series = s_numeric.to_numpy(dtype=float)
					except Exception:
						continue

					if series.size < 2:
						continue
					prev = series[:-1]
					curr = series[1:]
					with _np.errstate(divide="ignore", invalid="ignore"):
						ratio = curr / prev - 1.0
					# Drop NaN/inf values that may arise from zero or invalid
					# previous values.
					ratio = ratio[_np.isfinite(ratio)]
					if ratio.size:
						# Assign to group
						col_lower = str(col).lower()
						assigned = False
						for g in groups:
							if g in col_lower:
								group_values[g].extend(ratio.tolist())
								assigned = True
								break
						if not assigned:
							other_values.extend(ratio.tolist())

				all_vals = []
				for g in groups:
					all_vals.extend(group_values[g])
				all_vals.extend(other_values)

				if not all_vals:
					continue

				vals = _np.asarray(all_vals, dtype=float)
				max_val = float(_np.max(vals))
				min_val = float(_np.min(vals))

				if hist_bin_width <= 0.0:
					continue

				# Build bin edges using the configured window size.
				neg_bins = int(ceil(abs(min(0.0, min_val)) / float(hist_bin_width)))
				pos_bins = int(ceil(max(0.0, max_val) / float(hist_bin_width)))

				edges = _np.arange(-neg_bins * float(hist_bin_width), (pos_bins + 1) * float(hist_bin_width), float(hist_bin_width))
				bin_centres = (edges[:-1] + edges[1:]) / 2.0

				# Prefer Plotly for the histogram; fall back to Matplotlib if
				# Plotly is not available.
				try:
					import plotly.graph_objects as _go  # type: ignore
					from plotly.subplots import make_subplots as _make_subplots

					# Detect large gaps to handle outliers ("broken axis" effect)
					# Calculate total counts across all groups to find empty spaces
					total_counts = _np.zeros(len(bin_centres))
					group_counts = {}
					for g in groups:
						g_vals = _np.asarray(group_values[g], dtype=float)
						if g_vals.size > 0:
							counts, _ = _np.histogram(g_vals, bins=edges)
							group_counts[g] = counts
							total_counts += counts

					if other_values:
						o_vals = _np.asarray(other_values, dtype=float)
						counts, _ = _np.histogram(o_vals, bins=edges)
						group_counts["other"] = counts
						total_counts += counts

					# Find indices where counts > 0
					nz = _np.where(total_counts > 0)[0]
					gap_split_idx = None
					if nz.size > 1:
						gaps = _np.diff(nz)
						if gaps.size > 0:
							# Find all large gaps (> 50 bins)
							large_gaps = _np.where(gaps > 50)[0]
							if large_gaps.size > 0:
								# If there are large gaps, we want to isolate the "Main" cluster.
								# We define clusters separated by large gaps.
								cluster_starts = _np.concatenate(([0], large_gaps + 1))
								cluster_ends = _np.concatenate((large_gaps, [len(nz) - 1]))

								# Identify the "Main" cluster: the one with the most points,
								# prioritizing clusters near 0.
								best_cluster_idx = 0
								best_score = -1.0
								for i in range(len(cluster_starts)):
									c_nz_indices = nz[cluster_starts[i] : cluster_ends[i] + 1]
									c_bin_centres = bin_centres[c_nz_indices]
									c_counts = total_counts[c_nz_indices]
									c_total_points = _np.sum(c_counts)

									# Score: favor more points and proximity to 0
									# dist_to_zero is 0 if 0 is within [min, max] of cluster
									c_min, c_max = _np.min(c_bin_centres), _np.max(c_bin_centres)
									dist_to_zero = 0.0
									if c_min > 0: dist_to_zero = c_min
									elif c_max < 0: dist_to_zero = abs(c_max)

									score = c_total_points / (1.0 + dist_to_zero)
									if score > best_score:
										best_score = score
										best_cluster_idx = i

								# Now we have the main cluster.
								# If it's not the only cluster, we need to split.
								# For now, we still support 2 subplots.
								# If the main cluster is the first one, we split after it.
								# If it's not the first, we might want to split before it?
								# Usually, outliers are to the right.
								if len(cluster_starts) > 1:
									if best_cluster_idx == 0:
										# Split after the first cluster
										gap_split_idx = nz[large_gaps[0]]
									elif best_cluster_idx == len(cluster_starts) - 1:
										# Split before the last cluster
										gap_split_idx = nz[large_gaps[-1]]
									else:
										# Main cluster is in the middle? Split at the largest gap.
										gap_split_idx = nz[_np.argmax(gaps)]

					# Fallback to original logic if we couldn't decide or for extra safety
					if gap_split_idx is None and nz.size > 1:
						gaps = _np.diff(nz)
						if gaps.size > 0:
							max_gap_val = _np.max(gaps)
							if max_gap_val > 50 and max_gap_val > (len(bin_centres) * 0.4):
								gap_split_idx = nz[_np.argmax(gaps)]

					if gap_split_idx is not None:
						# Heuristic to name subplots: which side has more points?
						left_count = _np.sum(total_counts[:gap_split_idx+1])
						right_count = _np.sum(total_counts[gap_split_idx+1:])
						if left_count >= right_count:
							titles = ("Main Distribution", "Outliers")
						else:
							titles = ("Outliers", "Main Distribution")

						fig_hist = _make_subplots(
							rows=1, cols=2,
							shared_yaxes=True,
							horizontal_spacing=0.05,
							subplot_titles=titles
						)
						masks = [
							_np.arange(len(bin_centres)) <= gap_split_idx,
							_np.arange(len(bin_centres)) > gap_split_idx
						]
					else:
						fig_hist = _go.Figure()
						masks = [_np.ones(len(bin_centres), dtype=bool)]

					# Colors for top1, top3, top5
					colors = {"top1": "#636EFA", "top3": "#EF553B", "top5": "#00CC96", "other": "#AB63FA"}

					for col_idx, mask in enumerate(masks):
						if not _np.any(mask): continue

						# If we have subplots, we need to add traces to both?
						# No, only if the data exists in that range.
						for g_name in list(groups) + (["other"] if other_values else []):
							counts = group_counts.get(g_name)
							if counts is not None and _np.any(counts[mask] > 0):
								trace = _go.Bar(
									x=bin_centres[mask],
									y=counts[mask],
									name=g_name,
									marker_color=colors.get(g_name),
									legendgroup=g_name,
									showlegend=(col_idx == 0) # Only show in legend once
								)
								if gap_split_idx is not None:
									fig_hist.add_trace(trace, row=1, col=col_idx + 1)
								else:
									fig_hist.add_trace(trace)

					fig_hist.update_layout(
						height=280,
						margin=dict(l=10, r=10, t=30, b=10),
						xaxis_title="change ratio (day-over-day)" if gap_split_idx is None else None,
						yaxis_title="frequency",
						barmode='stack',
						legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
					)
					if gap_split_idx is not None:
						fig_hist.update_xaxes(title_text="Ratio", row=1, col=1)
						fig_hist.update_xaxes(title_text="Outliers", row=1, col=2)

					# Use explicit width with a unique key to avoid duplicate-element errors
					st.plotly_chart(fig_hist, width="stretch", key=f"stock_sim_hist_{title}")
				except Exception as e:
					print(f"[stock_sim] Plotly histogram error: {e}")
					try:
						import matplotlib.pyplot as _plt  # type: ignore

						# Find indices where counts > 0 for gap detection (reuse logic if possible or recalculate)
						# Recalculate total_counts for fallback
						tc_plt = _np.zeros(len(bin_centres))
						g_counts_plt = {}
						for g in groups:
							gv = _np.asarray(group_values[g], dtype=float)
							if gv.size > 0:
								c, _ = _np.histogram(gv, bins=edges)
								g_counts_plt[g] = c
								tc_plt += c
						if other_values:
							ov = _np.asarray(other_values, dtype=float)
							c, _ = _np.histogram(ov, bins=edges)
							g_counts_plt["other"] = c
							tc_plt += c

						nz_plt = _np.where(tc_plt > 0)[0]
						gap_split_idx_plt = None
						if nz_plt.size > 1:
							gaps_plt = _np.diff(nz_plt)
							if gaps_plt.size > 0:
								large_gaps_plt = _np.where(gaps_plt > 50)[0]
								if large_gaps_plt.size > 0:
									cluster_starts_plt = _np.concatenate(([0], large_gaps_plt + 1))
									cluster_ends_plt = _np.concatenate((large_gaps_plt, [len(nz_plt) - 1]))
									best_cluster_idx_plt = 0
									best_score_plt = -1.0
									for i in range(len(cluster_starts_plt)):
										c_nz_indices = nz_plt[cluster_starts_plt[i] : cluster_ends_plt[i] + 1]
										c_bin_centres = bin_centres[c_nz_indices]
										c_counts = tc_plt[c_nz_indices]
										c_total_points = _np.sum(c_counts)
										c_min, c_max = _np.min(c_bin_centres), _np.max(c_bin_centres)
										dist_to_zero = 0.0
										if c_min > 0: dist_to_zero = c_min
										elif c_max < 0: dist_to_zero = abs(c_max)
										score = c_total_points / (1.0 + dist_to_zero)
										if score > best_score_plt:
											best_score_plt = score
											best_cluster_idx_plt = i
									if len(cluster_starts_plt) > 1:
										if best_cluster_idx_plt == 0:
											gap_split_idx_plt = nz_plt[large_gaps_plt[0]]
										elif best_cluster_idx_plt == len(cluster_starts_plt) - 1:
											gap_split_idx_plt = nz_plt[large_gaps_plt[-1]]
										else:
											gap_split_idx_plt = nz_plt[_np.argmax(gaps_plt)]

						if gap_split_idx_plt is None and nz_plt.size > 1:
							gaps_plt = _np.diff(nz_plt)
							if gaps_plt.size > 0:
								max_gap_plt = _np.max(gaps_plt)
								if max_gap_plt > 50 and max_gap_plt > (len(bin_centres) * 0.4):
									gap_split_idx_plt = nz_plt[_np.argmax(gaps_plt)]

						if gap_split_idx_plt is not None:
							fig_hist, (ax1, ax2) = _plt.subplots(1, 2, figsize=(9.0, 4.0), sharey=True)
							_plt.subplots_adjust(wspace=0.1)
							axes = [ax1, ax2]
							masks_plt = [
								_np.arange(len(bin_centres)) <= gap_split_idx_plt,
								_np.arange(len(bin_centres)) > gap_split_idx_plt
							]
						else:
							fig_hist, ax_hist = _plt.subplots(figsize=(7.0, 4.0))
							axes = [ax_hist]
							masks_plt = [_np.ones(len(bin_centres), dtype=bool)]

						colors_plt = {"top1": "tab:blue", "top3": "tab:red", "top5": "tab:green", "other": "tab:purple"}

						for ax, mask in zip(axes, masks_plt):
							if not _np.any(mask): continue
							bottom = _np.zeros(_np.sum(mask))
							bc_masked = bin_centres[mask]

							for g_name in list(groups) + (["other"] if other_values else []):
								counts = g_counts_plt.get(g_name)
								if counts is not None and _np.any(counts[mask] > 0):
									c_masked = counts[mask]
									ax.bar(bc_masked, c_masked, width=float(hist_bin_width) * 0.9,
										   bottom=bottom, label=g_name, color=colors_plt.get(g_name))
									bottom += c_masked

							ax.set_xlabel("ratio")
							ax.grid(True, alpha=0.3)

						axes[0].set_ylabel("frequency")
						if len(axes) > 1:
							axes[0].set_title("Main" if _np.sum(tc_plt[masks_plt[0]]) >= _np.sum(tc_plt[masks_plt[1]]) else "Outliers")
							axes[1].set_title("Outliers" if _np.sum(tc_plt[masks_plt[0]]) >= _np.sum(tc_plt[masks_plt[1]]) else "Main")

						axes[0].legend()
						st.pyplot(fig_hist, clear_figure=True)
					except Exception as e_plt:
						print(f"[stock_sim] Matplotlib histogram error: {e_plt}")
						# Fallback to streamlit native chart
						try:
							st.caption("Plotly/Matplotlib missing; using native Streamlit chart.")
							import pandas as _pd

							chart_data = {}
							for g in groups:
								if group_values[g]:
									counts, _ = _np.histogram(group_values[g], bins=edges)
									chart_data[g] = counts
							if other_values:
								counts, _ = _np.histogram(other_values, bins=edges)
								chart_data["other"] = counts

							labels = [f"{x:.3f}" for x in bin_centres]
							hist_df = _pd.DataFrame(chart_data, index=labels)
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

	# Download (from the (standardized) grid DataFrame, without helper columns)
	csv_buf = io.StringIO()
	df.to_csv(csv_buf, index=False)
	st.download_button("Download CSV", data=csv_buf.getvalue(), file_name="run_params_comparison.csv", mime="text/csv")

	# Determine which timestamps to show in the viewer: ONLY use explicit grid selection.
	# We no longer fall back to all sidebar runs or persistent state from previous runs
	# if nothing is currently selected, as requested.
	runs_for_view: List[str] = list(dict.fromkeys(str(r) for r in selected_runs_from_grid))

	# Provide a button under the grid to explicitly populate the graphs
	if st.button("Populate Graphs", key="crv_populate_graphs"):
		st.session_state["crv_graphs_populated"] = True

	# Render the viewer panel at the bottom of the main page (only after button press)
	if st.session_state.get("crv_graphs_populated"):
		_viewer_panel(runs_for_view)
	else:
		st.info("Click 'Populate Graphs' under the grid to render charts.")


if __name__ == "__main__":
	_run_as_script()
