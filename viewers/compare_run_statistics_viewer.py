import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Tuple
import re as _re

# Add project root to sys.path to allow imports from sibling packages like 'database'
root_path = Path(__file__).resolve().parent.parent
if str(root_path) not in sys.path:
	sys.path.insert(0, str(root_path))

#import pandas as pd  # type: ignore
import streamlit as st  # type: ignore
from pandas import read_csv, DataFrame, concat

import plotly.express as px  # type: ignore
import plotly.graph_objects as go  # type: ignore

from simulate_trading import run_simulation_from_saved_scores, generate_simulations_from_directories, get_single_run_directory


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
	render_run_grid,
	discover_best_run_dir,
)


def _has_any_simulation_files(path: str) -> bool:
	return has_any_simulation_files(path)


def _discover_best_run_dir(base_dir: str, ts_name: str, run_list: List[Tuple[str, Dict[str, Any]]]) -> str | None:
	return discover_best_run_dir(base_dir, ts_name, run_list)


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
		# dname_0 might be "run_family_X/run_single_Y/repZ" or "ts/repZ"
		parent_rel = os.path.dirname(dname_0)
		candidate = os.path.normpath(os.path.join(base_dir, parent_rel))
		if os.path.isdir(candidate):
			ts_path = candidate

	# 1) Timestamp directory as ensemble
	if os.path.isdir(ts_path) and _has_any_simulation_files(ts_path):
		results.append((ts_path, "ensemble"))

	# 2) Subdirectories beneath timestamp directory
	if os.path.isdir(ts_path):
		try:
			for fname in sorted(os.listdir(ts_path)):
				sub_path = os.path.join(ts_path, fname)
				if not os.path.isdir(sub_path):
					continue
				if _has_any_simulation_files(sub_path):
					results.append((os.path.normpath(sub_path), fname))
		except Exception as e:
			print(f"[crsv] Error scanning subdirs of {ts_path}: {e}")

	# 3) Fallback: first run's explicit _dir
	if not results and dname_0:
		alt_path = os.path.normpath(os.path.join(base_dir, dname_0))
		if _has_any_simulation_files(alt_path):
			# Label derived from last component
			results.append((alt_path, os.path.basename(alt_path) or "_dir"))

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
	results.sort(key=lambda t: (t[0], t[1], t[2]))
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


import time
import json
import ast
from viewers.populate_statistics_viewer import build_run_epoch_series

def _compute_run_statistics_for_path(run_path: str) -> Dict[str, Any]:
	"""Compute summary statistics for a single run directory.

	Prefer reading pre-calculated stats from run_summary_stats.json if available.
	"""
	summary_file = os.path.join(run_path, "run_summary_stats.json")
	if os.path.exists(summary_file):
		try:
			with open(summary_file, 'r') as f:
				summary = json.load(f)

			stats = {}
			# Flatten summary for the grid
			for key, val in summary.get("end_stats", {}).items():
				stats[f"end{key}"] = val

			for n_str, mstats in summary.get("max_stats", {}).items():
				# n_str is like "0" or "[1, 'Rank']" (string representation of key)
				# We need to clean it up for the grid columns
				clean_n = n_str.replace("[", "").replace("]", "").replace("'", "").replace(", ", "")
				stats[f"max{clean_n}"] = mstats["max_val"]
				stats[f"max_epoch{clean_n}"] = mstats["max_epoch"]
				stats[f"first5_max{clean_n}"] = mstats["first5_max"]
				stats[f"first5_epoch{clean_n}"] = mstats["first5_epoch"]

				# Add rank stats if available in JSON
				if "avg_rank" in mstats:
					stats[f"rank{clean_n}"] = mstats["avg_rank"]
			return stats
		except Exception as e:
			print(f"[crsv] Error reading summary file {summary_file}: {e}")

	# Fallback to manual computation if JSON missing or failed
	stats: Dict[str, Any] = {}

	# --- End-of-training values from stock_simulation_<set_size>.csv ---------
	#
	# Column compatibility matrix:
	#   - Legacy:      portfolio_top<N>
	#   - New single:  portfolio_value
	#   - Recent CSVs: "Portfolio return top<N>" or "Portfolio value top<N>"
	# Handle case/space/underscore variations robustly.
	end_files = _load_end_csv(run_path)
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
		n_to_col: Dict[Any, str] = {}
		n_to_rank_col: Dict[Any, str] = {}
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

		# portfolio_top<K> (Method)
		m = _re.match(r"^portfolio(?:return|value)top(\d+)(?:\((sum|norm|rank)\))?$", cn)
		if m:
			k_val = int(m.group(1))
			method_val = m.group(2).capitalize() if m.group(2) else ""
			n_to_col[(k_val, method_val)] = c
			continue

		# Rank Percent top<K> (Method)
		mr = _re.match(r"^rankpercenttop(\d+)(?:\((sum|norm|rank)\))?$", cn)
		if mr:
			k_val = int(mr.group(1))
			method_val = mr.group(2).capitalize() if mr.group(2) else ""
			n_to_rank_col[(k_val, method_val)] = c
			continue

		# Legacy portfolio_top<N>
		m = _re.match(r"^portfoliotop(\d+)$", cn)
		if m:
			n_to_col[(int(m.group(1)), "")] = c
			continue

		if not n_to_col and "portfolio_value" in df.columns:
			n_to_col[0] = "portfolio_value"

		for n, col in n_to_col.items():
			try:
				end_val = float(df[col].iloc[-1])
			except Exception as e:
				print(f"[crsv] Error extracting end value for {col} in {_set_size}: {e}")
				continue

			if isinstance(n, tuple):
				k, m = n
				stats[f"end{k}{m}"] = end_val
			else:
				stats[f"end{n}"] = end_val

	# --- Per‑epoch statistics from stock_simulation_<set_size>_epoch_*.csv ---
	epoch_files = _find_epoch_files(run_path)

	if not epoch_files:
		return stats

	# Instead of tying statistics to the single set_size encoded in the
	# filename, scan all portfolio-like columns in each epoch file so we
	# expose metrics for N = 1, 3, 5, ... as separate columns (max1, max3,
	# max5, etc.). When no N exists (single series), we use index 0.

	max_val: Dict[Any, float] = {}
	max_epoch: Dict[Any, int] = {}
	first5_best: Dict[Any, float] = {}
	first5_best_epoch: Dict[Any, int] = {}
	avg_ranks: Dict[Any, List[float]] = {}

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
		n_to_col: Dict[Any, str] = {}
		n_to_rank_col: Dict[Any, str] = {}
		def _norm(s: str) -> str:
			return s.lower().replace(" ", "").replace("_", "")
		for c in df.columns:
			if not isinstance(c, str):
				continue
			cn = _norm(c)
			if cn == "portfoliovalue":
				n_to_col[0] = c
				continue

			# portfolio_top<K> (Method)
			m = _re.match(r"^portfolio(?:return|value)top(\d+)(?:\((sum|norm|rank)\))?$", cn)
			if m:
				k_val = int(m.group(1))
				method_val = m.group(2).capitalize() if m.group(2) else ""
				n_to_col[(k_val, method_val)] = c
				continue

			# Rank Percent top<K> (Method)
			mr = _re.match(r"^rankpercenttop(\d+)(?:\((sum|norm|rank)\))?$", cn)
			if mr:
				k_val = int(mr.group(1))
				method_val = mr.group(2).capitalize() if mr.group(2) else ""
				n_to_rank_col[(k_val, method_val)] = c
				continue

			# Legacy portfolio_top<K>
			m = _re.match(r"^portfoliotop(\d+)$", cn)
			if m:
				n_to_col[(int(m.group(1)), "")] = c
				continue

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

		# Extract ranks for this epoch
		for n, col in n_to_rank_col.items():
			try:
				mean_rank = float(df[col].mean())
				avg_ranks.setdefault(n, []).append(mean_rank)
			except Exception:
				pass

	for n, value in max_val.items():
		if isinstance(n, tuple):
			k, m = n
			stats[f"max{k}{m}"] = value
		else:
			stats[f"max{n}"] = value
	for n, epoch in max_epoch.items():
		if isinstance(n, tuple):
			k, m = n
			stats[f"max_epoch{k}{m}"] = epoch
		else:
			stats[f"max_epoch{n}"] = epoch
	for n, value in first5_best.items():
		if isinstance(n, tuple):
			k, m = n
			stats[f"first5_max{k}{m}"] = value
		else:
			stats[f"first5_max{n}"] = value
	for n, epoch in first5_best_epoch.items():
		if isinstance(n, tuple):
			k, m = n
			stats[f"first5_epoch{k}{m}"] = epoch
		else:
			stats[f"first5_epoch{n}"] = epoch

	# Finalize rank stats (mean over all epochs/days processed)
	for n, ranks in avg_ranks.items():
		if ranks:
			avg_val = sum(ranks) / len(ranks)
			if isinstance(n, tuple):
				k, m = n
				stats[f"rank{k}{m}"] = avg_val
			else:
				stats[f"rank{n}"] = avg_val

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
			# Ensure _dir points to this variant's directory relative to base_dir
			try:
				row["_dir"] = os.path.relpath(run_path, base_dir)
			except Exception:
				pass
			# Provide a stable display id distinct per variant
			row["run_id"] = f"{ts_name} [{label}]"
			rows.append(row)

	return rows


@st.cache_data(show_spinner=False)
def _cached_build_statistics_rows(
	base_dir: str, ts_to_runs: Dict[str, List[Tuple[str, Dict[str, Any]]]]
) -> List[Dict[str, Any]]:
	return _build_statistics_rows(base_dir, ts_to_runs)


def _run_generate_stats(selected_ts, all_stat_rows, base_dir, root_path, force=False):
	progress_bar = st.progress(0)
	status_text = st.empty()

	# Collect all run directories (including reps) that belong to selected timestamps
	paths_to_process = []
	for r in all_stat_rows:
		if r.get("_ts") in selected_ts:
			dname = r.get("_dir")
			if dname:
				p = os.path.normpath(os.path.join(base_dir, dname))
				if os.path.isdir(p) and p not in paths_to_process:
					paths_to_process.append(p)
					# Also include parent
					parent_p = os.path.dirname(p)
					if parent_p and parent_p != base_dir and parent_p not in paths_to_process:
						paths_to_process.append(parent_p)

	import subprocess
	script_path = os.path.join(root_path, "scripts", "generate_simulation_stats.py")

	total = len(paths_to_process)
	for i, p in enumerate(paths_to_process):
		status_text.text(f"Pre-calculating {os.path.basename(p)} ({i + 1}/{total})...")
		try:
			cmd = [sys.executable, script_path, p, "--summary-only"]
			if force:
				cmd.append("--force")
			# Call our new script
			subprocess.run(cmd, check=True)
		except Exception as e:
			st.error(f"Error pre-calculating {p}: {e}")
		progress_bar.progress((i + 1) / total)

	status_text.text("Done! Refreshing...")
	st.rerun()


def _run_generate_simulations(selected_ts, all_stat_rows, base_dir, selected_run_ids, force=False):
	progress_bar = st.progress(0)
	status_text = st.empty()

	ts_to_process = set(selected_ts)
	paths_to_process = []
	for r in all_stat_rows:
		if r.get("_ts") in ts_to_process:
			if r.get("run_id") in selected_run_ids or r.get("_ts") in selected_run_ids:
				dname = r.get("_dir")
				if dname:
					p = os.path.normpath(os.path.join(base_dir, dname))
					if os.path.isdir(p):
						# Only single run (ensemble) directories should be used.
						p = get_single_run_directory(p)
						if p not in paths_to_process:
							paths_to_process.append(p)

	if not paths_to_process:
		st.warning("No directories found for selected timestamps.")
		return

	status_text.text(f"Generating simulations for {len(paths_to_process)} directories...")
	try:
		print("\n\nPATHS TO PROCESS ARE ", paths_to_process)
		generate_simulations_from_directories(paths_to_process, top_k=[1, 3, 5, 10], force=force)
	except Exception as e:
		st.error(f"Error during simulation generation: {e}")
		traceback.print_exc()

	status_text.text("Done! Refreshing statistics...")
	st.rerun()


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
	# Navigation support from dashboard
	# ------------------------------------------------------------------
	# If we have pre-selected families/builds from session state, we use them
	# but don't force a reset if the user has already interacted with sidebar.
	if "crsv_families" in st.session_state and "crsv_families_init" not in st.session_state:
		# First time arriving from dashboard
		st.session_state["crsv_families_init"] = True
		# sidebar_family_and_build_selector uses key_prefix_families/builds
		# The dashboard sets crsv_families and crsv_builds.
		# But wait, sidebar_family_and_build_selector uses st.session_state[f"{key_prefix}_families"]
		# So if the dashboard sets it, the selector should pick it up as default.

	# ------------------------------------------------------------------
	# Sidebar selection: families + builds (shared across viewers)
	# ------------------------------------------------------------------
	st.sidebar.button("Refresh runs list")
	st.sidebar.subheader("Select families and builds")

	def update_list_of_runs_selected(selected_runs: List[str]) -> None:
		st.session_state["crsv_runs_selected"] = list(selected_runs)

	# One important detail: sidebar_family_and_build_selector will default
	# to 'all builds' if no builds are selected in st.session_state and a family is selected.
	# If we want it to ONLY show dashboard-selected builds, we must ensure
	# it doesn't fall back to all.
	# We can't easily change sidebar_family_and_build_selector's behavior without affecting others.

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

	# If we arrived from dashboard, we might have had specific builds.
	# But sidebar_family_and_build_selector logic:
	# if (not selected_builds) and selected_families: selected_builds = list(available_builds)
	# This is likely the culprit.

	# Match the behavior of the other runs viewer: do not populate the grid
	# or draw charts until at least one run family has been selected.
	if not selected_families:
		st.info("Select one or more run families in the sidebar to begin.")
		return

	# If we arrived from the dashboard, we want to respect the specific builds selected
	# even if the sidebar selector defaults to 'all' for selected families.
	if "crsv_builds" in st.session_state and "crsv_builds_applied" not in st.session_state:
		# Check if the currently selected_builds is actually the full list for these families
		# which happens if sidebar_family_and_build_selector found no previous selection in state.
		# Dashboard sets crsv_builds, but the selector looks for crsv_builds. Wait, dashboard sets crsv_builds.
		# Key prefix is 'crsv', so selector looks for st.session_state['crsv_builds'].
		# If it's already there, sidebar_family_and_build_selector should have used it!
		pass

	selected_builds_set = set(selected_builds)

	# Prepare rows for the grid
	# Instructions: "the run grid... should look the same and use the same underlying code."
	# If "display extending info" is checked, show all variants. Otherwise show collapsed/ensemble.
	show_extended_key = "display_extending_info"
	if show_extended_key not in st.session_state:
		st.session_state[show_extended_key] = prefs.get(show_extended_key, False)
	show_extended = st.session_state[show_extended_key]

	# --- Performance profiling ---
	start_time = time.perf_counter()

	all_stat_rows = _cached_build_statistics_rows(base_dir, ts_to_runs)

	load_time = time.perf_counter() - start_time
	st.sidebar.write(f"Data load time: {load_time:.2f}s")

	display_rows: List[Dict[str, Any]] = []
	if show_extended:
		display_rows = all_stat_rows
	else:
		# Filter to only ensemble variants if they exist, else use whatever is there for that TS
		# This ensures the grid is primarily populated with logical "single runs" (ensembles).
		ts_groups: Dict[str, List[Dict[str, Any]]] = {}
		for row in all_stat_rows:
			ts_groups.setdefault(row["_ts"], []).append(row)

		for ts_name, group in ts_groups.items():
			ensemble_rows = [r for r in group if r.get("_variant") == "ensemble"]
			if ensemble_rows:
				display_rows.extend(ensemble_rows)
			else:
				# If no ensemble, use the first variant found (which might be a single rep if it's not a standard ensemble)
				display_rows.append(group[0])

	# Filter by selected builds from sidebar
	if selected_builds_set:
		display_rows = [r for r in display_rows if r.get("_ts") in selected_builds_set]

	if not display_rows:
		st.info("No runs found for the current selection.")
		return

	common = compute_common_params(display_rows)
	common_keys = sorted(common.keys())

	# Shared common-params display
	render_common_params(common, title="Common parameters across runs", expanded=False)

	# Construct DataFrame
	exclude_keys = ["_dir"] + common_keys
	table_rows = runs_to_dataframe_dicts(display_rows, exclude_keys)
	df = DataFrame(table_rows)

	st.subheader("Run selection and statistics")

	# Standard grid rendering
	selected_run_ids = render_run_grid(
		df=df,
		key_prefix="crsv",
		prefs=prefs,
		default_visible_cols=["run"] + [c for c in df.columns if c not in ["run", "_variant", "_ts", "run_id", "activation", "final_activation", "optimizer", "optimizer_list"]],
		return_all_if_none=False
	)

	# Map selected_run_ids back to timestamps for charts
	selected_ts: List[str] = []
	for rid in selected_run_ids:
		# rid could be "TS [variant]" or just TS or relative path.
		# In RSD, rid is a relative path like "run_family_.../run_single_.../single_run_0.json"
		# In CRSV, display_rows are from _cached_build_statistics_rows.
		# Let's check how rid matches display_rows.
		for r in display_rows:
			if (r.get("run") == rid or r.get("run_id") == rid or r.get("_ts") == rid or r.get("_dir") == rid):
				if r["_ts"] not in selected_ts:
					selected_ts.append(r["_ts"])
				break

	if not selected_ts and selected_run_ids:
		 # fallback if matching fails, should not happen if grid is consistent
		 selected_ts = list(set(selected_run_ids))

	# Persist last selection
	st.session_state["crsv_last_selection"] = selected_ts

	# --- New Section: Multi-run averages (Epoch vs Avg Epoch Return) ---
	# We now look for rep_averages.csv in each selected run directory and aggregate them.
	aggregated_data = []

	# Map selected timestamps to their corresponding directories
	selected_dirs_full = []
	for r in all_stat_rows:
		if r.get("_ts") in selected_ts:
			dname = r.get("_dir")
			if dname:
				# If _dir is a rep directory, we want the parent which is the run_single directory
				# Actually, in this project structure, it seems:
				# data/run_family_.../run_single_.../repN
				# AND rep_averages.csv is in run_single_...
				# So we try the directory itself and its parent.
				full_p = os.path.normpath(os.path.join(base_dir, dname))
				if full_p not in selected_dirs_full:
					selected_dirs_full.append(full_p)

				parent_p = os.path.dirname(full_p)
				if parent_p and parent_p != base_dir and parent_p not in selected_dirs_full:
					selected_dirs_full.append(parent_p)

	for run_dir in selected_dirs_full:
		rep_avg_csv = os.path.join(run_dir, "rep_averages.csv")
		if os.path.exists(rep_avg_csv):
			try:
				run_mdf = read_csv(rep_avg_csv)
				# Ensure run_directory column is present for plotting color
				if "run_directory" not in run_mdf.columns:
					run_mdf["run_directory"] = os.path.basename(run_dir)
				aggregated_data.append(run_mdf)
			except Exception as e:
				st.error(f"Error loading {rep_avg_csv}: {e}")

	if aggregated_data:
		try:
			mdf = concat(aggregated_data, ignore_index=True)
			if not mdf.empty:
				st.subheader("Multi-run averages: Epoch vs Avg Epoch Return")

				# Plotly graph
				fig = px.line(
					mdf,
					x="epoch",
					y="epoch_avg_return",
					color="run_directory",
					markers=True,
					title="Epoch vs Average Epoch Return",
					labels={"epoch_avg_return": "Avg Epoch Return", "epoch": "Epoch", "run_directory": "Run"}
				)
				fig.update_layout(height=400)
				st.plotly_chart(fig, width='stretch')
		except Exception as e:
			st.error(f"Error aggregating rep averages: {e}")

	# Provide a separate view for rep rows if not showing them in the main grid
	if not st.session_state[show_extended_key] and "_variant" in df.columns:
		rep_df = df[df["_variant"].astype(str) != "ensemble"].copy()
		if not rep_df.empty:
			with st.expander("Show rep rows (read-only)", expanded=False):
				st.dataframe(rep_df.reset_index(drop=True), width='stretch')

	# Download button for the statistics table
	csv_buf = df.to_csv(index=False)
	col_dl, col_gen = st.columns([1, 1])
	with col_dl:
		st.download_button(
			"Download statistics CSV",
			data=csv_buf,
			file_name="run_statistics_comparison.csv",
			mime="text/csv",
		)
	with col_gen:
		col_gen_1, col_gen_2 = st.columns([1, 1])
		with col_gen_1:
			if st.button("Generate statistics (Pre-calculate)", help="Run the generation script to pre-calculate statistics for selected runs, which makes the viewer much faster."):
				if not selected_ts:
					st.warning("Please select at least one run.")
				else:
					_run_generate_stats(selected_ts, all_stat_rows, base_dir, root_path, force=False)

		with col_gen_2:
			if st.button("Force Generate statistics", help="Redo all calculations even if they already exist."):
				if not selected_ts:
					st.warning("Please select at least one run.")
				else:
					_run_generate_stats(selected_ts, all_stat_rows, base_dir, root_path, force=True)

		col_sim_1, col_sim_2 = st.columns([1, 1])
		with col_sim_1:
			if st.button("Generate simulations from saved scores", help="For selected runs, look for saved scores and generate stock_simulation_*.csv files"):
				if not selected_ts:
					st.warning("Please select at least one run in the table above.")
				else:
					_run_generate_simulations(selected_ts, all_stat_rows, base_dir, selected_run_ids, force=False)

		with col_sim_2:
			if st.button("Force Generate simulations", help="For selected runs, look for saved scores and generate stock_simulation_*.csv files. Forces regeneration even if files exist."):
				if not selected_ts:
					st.warning("Please select at least one run in the table above.")
				else:
					_run_generate_simulations(selected_ts, all_stat_rows, base_dir, selected_run_ids, force=True)

	# ------------------------------------------------------------------
	# Charts: for each selected timestamp we build two plots:
	#   1. final portfolio value vs epoch
	#   2. best first‑five‑days value vs epoch
	# ------------------------------------------------------------------

	st.subheader("Per‑run epoch statistics")

	# Checkboxes for graph visibility
	st.markdown("##### Graph visibility")
	col1, col2, col3, col4 = st.columns(4)

	if "crsv_show_final" not in st.session_state:
		st.session_state["crsv_show_final"] = prefs.get("crsv_show_final", True)
	if "crsv_show_first5" not in st.session_state:
		st.session_state["crsv_show_first5"] = prefs.get("crsv_show_first5", True)
	if "crsv_show_avgcdf" not in st.session_state:
		st.session_state["crsv_show_avgcdf"] = prefs.get("crsv_show_avgcdf", True)
	if "crsv_show_reps" not in st.session_state:
		st.session_state["crsv_show_reps"] = prefs.get("crsv_show_reps", True)

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
	with col4:
		show_reps = st.checkbox(
			"Show reps",
			value=st.session_state["crsv_show_reps"],
			key="crsv_show_reps",
			help="When unchecked, only the ensemble graph is shown for each run configuration.",
			on_change=lambda: save_preference("crsv_show_reps", st.session_state["crsv_show_reps"]),
		)

	# Toggle for grouping mode
	grouping_key = "crsv_chart_grouping"

	# Ensemble method visibility controls
	st.markdown("##### Ensemble methods to show")
	meth_col1, meth_col2, meth_col3, _ = st.columns(4)
	with meth_col1:
		st.checkbox("Sum", value=st.session_state.get("crsv_method_sum", True), key="crsv_method_sum", on_change=lambda: save_preference("crsv_method_sum", st.session_state["crsv_method_sum"]))
	with meth_col2:
		st.checkbox("Norm", value=st.session_state.get("crsv_method_norm", True), key="crsv_method_norm", on_change=lambda: save_preference("crsv_method_norm", st.session_state["crsv_method_norm"]))
	with meth_col3:
		st.checkbox("Rank", value=st.session_state.get("crsv_method_rank", True), key="crsv_method_rank", on_change=lambda: save_preference("crsv_method_rank", st.session_state["crsv_method_rank"]))

	if st.button("Populate Graphs", key="crsv_populate_graphs_main"):
		st.session_state["crsv_graphs_populated"] = True

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

	if not st.session_state.get("crsv_graphs_populated"):
		st.info("Click 'Populate Graphs' to view charts.")
		return

	# Collectors used for the "Across runs" grouping mode
	acc_final: Dict[Any, Dict[str, Dict[int, float]]] = {}
	acc_first5: Dict[Any, Dict[str, Dict[int, float]]] = {}
	acc_avgcdf: Dict[Any, Dict[str, Dict[int, float]]] = {}

	for ts_name in selected_ts:
		run_list = ts_to_runs.get(ts_name)
		if not run_list:
			continue

		# Determine timestamp directory path (may be nested under run_family)
		ts_path = os.path.join(base_dir, ts_name)
		if run_list:
			dname = run_list[0][1].get("_dir")
			if dname:
				# dname could be "run_family_X/run_single_Y/repZ"
				# parent_rel would be "run_family_X/run_single_Y"
				parent_rel = os.path.dirname(dname)
				candidate = os.path.join(base_dir, parent_rel)
				if os.path.isdir(candidate):
					ts_path = candidate

		run_paths: List[Tuple[str, str]] = []  # (label, path)

		# Ensemble at timestamp level
		if os.path.isdir(ts_path) and _find_epoch_files(ts_path):
			run_paths.append(("ensemble", ts_path))

		# Rep sub-runs
		if show_reps:
			for _run_name, params in run_list:
				dname = params.get("_dir")
				if not dname:
					continue
				parts = re.split(r'[\\/]', str(dname))
				if len(parts) < 2:
					continue
				rep_name = parts[-1]
				# In new structure, ts_part might be run_single_... which matches the end of ts_name or id
				# We trust dname is relative to base_dir and points to the rep.
				rep_path = str(os.path.join(base_dir, dname))
				if not os.path.isdir(rep_path):
					continue
				if not _find_epoch_files(rep_path):
					continue
				if any(existing_path == rep_path for _label, existing_path in run_paths):
					continue
				run_paths.append((rep_name, rep_path))

		if not run_paths and not show_reps:
			# If show_reps is off and there's no ensemble, fall back to the first rep
			# so the user sees *something* for their selection.
			for _run_name, params in run_list:
				dname = params.get("_dir")
				if not dname: continue
				rep_path = str(os.path.join(base_dir, dname))
				if os.path.isdir(rep_path) and _find_epoch_files(rep_path):
					run_paths.append((os.path.basename(rep_path), rep_path))
					break

		if not run_paths:
			continue

		if grouping_mode == "Per run (overlay N lines)":
			st.markdown(f"### Run {ts_name}")

		for label, run_path in run_paths:
			# Build data only (no plotting) using the helper for profiling and reuse
			by_topn, by_topn_first5, by_topn_avgcdf, _ = build_run_epoch_series(run_path)

			# Placeholders if nothing collected
			used_placeholder = False
			if not by_topn and not by_topn_first5 and not by_topn_avgcdf:
				used_placeholder = True
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

					# Determine which columns to show based on ensemble method and top_k selections
					ensemble_methods_enabled = []
					for m in ["Sum", "Norm", "Rank"]:
						if st.session_state.get(f"crsv_method_{m.lower()}"):
							ensemble_methods_enabled.append(m)

					# Top-k selection (numeric)
					# Use all if nothing selected in a theoretical top_k filter

					displayed_any = False
					for n, epoch_map in sorted(by_topn.items()):
						# If n is a tuple (k, method), check if method is enabled
						col_label = None
						if isinstance(n, tuple):
							k_val, method_val = n
							if method_val and method_val not in ensemble_methods_enabled:
								continue
							col_label = f"final_top{k_val} ({method_val})" if method_val else f"final_top{k_val}"
						else:
							col_label = f"final_top{n}" if n != 0 else "final_value"

						plot_df[col_label] = [epoch_map.get(e) for e in all_epochs]
						displayed_any = True

					# Badge for placeholder data
					if used_placeholder:
						st.markdown("**Final value by epoch** — `FAKE`")
					elif not displayed_any:
						st.markdown("**Final value by epoch** — `Hidden by filters`")
					else:
						st.markdown("**Final value by epoch**")

					if displayed_any:
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

					ensemble_methods_enabled = []
					for m in ["Sum", "Norm", "Rank"]:
						if st.session_state.get(f"crsv_method_{m.lower()}"):
							ensemble_methods_enabled.append(m)

					displayed_any = False
					for n, epoch_map in sorted(by_topn_first5.items()):
						col_label = None
						if isinstance(n, tuple):
							k_val, method_val = n
							if method_val and method_val not in ensemble_methods_enabled:
								continue
							col_label = f"first5_best_top{k_val} ({method_val})" if method_val else f"first5_best_top{k_val}"
						else:
							col_label = f"first5_best_top{n}" if n != 0 else "first5_best_value"

						plot_df[col_label] = [epoch_map.get(e) for e in all_epochs]
						displayed_any = True

					if used_placeholder:
						st.markdown("**Best first‑5‑day value by epoch** — `FAKE`")
					elif not displayed_any:
						st.markdown("**Best first‑5‑day value by epoch** — `Hidden by filters`")
					else:
						st.markdown("**Best first‑5‑day value by epoch**")

					if displayed_any:
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

					ensemble_methods_enabled = []
					for m in ["Sum", "Norm", "Rank"]:
						if st.session_state.get(f"crsv_method_{m.lower()}"):
							ensemble_methods_enabled.append(m)

					displayed_any = False
					for n, epoch_map in sorted(by_topn_avgcdf.items()):
						col_label = None
						if isinstance(n, tuple):
							k_val, method_val = n
							if method_val and method_val not in ensemble_methods_enabled:
								continue
							col_label = f"avg_cdf_top{k_val} ({method_val})" if method_val else f"avg_cdf_top{k_val}"
						else:
							col_label = f"avg_cdf_top{n}" if n != 0 else "avg_cdf_value"

						plot_df[col_label] = [epoch_map.get(e) for e in all_epochs]
						displayed_any = True

					if used_placeholder:
						st.markdown("**Average CDF by epoch** — `FAKE`")
					elif not displayed_any:
						st.markdown("**Average CDF by epoch** — `Hidden by filters`")
					else:
						st.markdown("**Average CDF by epoch**")

					if displayed_any:
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
		# Determine which methods are enabled
		ensemble_methods_enabled = []
		for m in ["Sum", "Norm", "Rank"]:
			if st.session_state.get(f"crsv_method_{m.lower()}"):
				ensemble_methods_enabled.append(m)

		# Final
		if show_final:
			for n in sorted(acc_final.keys(), key=lambda x: (x if isinstance(x, int) else x[0], "" if isinstance(x, int) else x[1])):
				if isinstance(n, tuple):
					k_val, method_val = n
					if method_val and method_val not in ensemble_methods_enabled:
						continue
					title_suffix = f"top{k_val} ({method_val})" if method_val else f"top{k_val}"
				else:
					title_suffix = f"top{n}" if n != 0 else "total"

				series_dict = acc_final[n]
				# Union of all epochs across runs
				all_epochs = sorted({e for m in series_dict.values() for e in m.keys()})
				plot_df = DataFrame({"epoch": all_epochs})
				for series_label, epoch_map in sorted(series_dict.items()):
					plot_df[series_label] = [epoch_map.get(e) for e in all_epochs]

				st.markdown(f"**Final value by epoch — {title_suffix}**")
				plot_lines(plot_df, "epoch", height=300, y_ref=1000.0, log_y=log_y, key=f"crsv_across_final__{n}")

		# First5
		if show_first5:
			for n in sorted(acc_first5.keys(), key=lambda x: (x if isinstance(x, int) else x[0], "" if isinstance(x, int) else x[1])):
				if isinstance(n, tuple):
					k_val, method_val = n
					if method_val and method_val not in ensemble_methods_enabled:
						continue
					title_suffix = f"top{k_val} ({method_val})" if method_val else f"top{k_val}"
				else:
					title_suffix = f"top{n}" if n != 0 else "total"

				series_dict = acc_first5[n]
				all_epochs = sorted({e for m in series_dict.values() for e in m.keys()})
				plot_df = DataFrame({"epoch": all_epochs})
				for series_label, epoch_map in sorted(series_dict.items()):
					plot_df[series_label] = [epoch_map.get(e) for e in all_epochs]

				st.markdown(f"**Best first‑5‑day by epoch — {title_suffix}**")
				plot_lines(plot_df, "epoch", height=300, y_ref=1000.0, log_y=log_y, key=f"crsv_across_first5__{n}")

		# Avg CDF
		if show_avgcdf:
			for n in sorted(acc_avgcdf.keys(), key=lambda x: (x if isinstance(x, int) else x[0], "" if isinstance(x, int) else x[1])):
				if isinstance(n, tuple):
					k_val, method_val = n
					if method_val and method_val not in ensemble_methods_enabled:
						continue
					title_suffix = f"top{k_val} ({method_val})" if method_val else f"top{k_val}"
				else:
					title_suffix = f"top{n}" if n != 0 else "total"

				series_dict = acc_avgcdf[n]
				all_epochs = sorted({e for m in series_dict.values() for e in m.keys()})
				plot_df = DataFrame({"epoch": all_epochs})
				for series_label, epoch_map in sorted(series_dict.items()):
					plot_df[series_label] = [epoch_map.get(e) for e in all_epochs]

				st.markdown(f"**Average CDF by epoch — {title_suffix}**")
				plot_lines(plot_df, "epoch", height=300, y_ref=1000.0, log_y=log_y, key=f"crsv_across_avgcdf__{n}")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
	_run_as_script()
