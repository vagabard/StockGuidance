import os
import sys
import json
import traceback

import pandas as pd
import streamlit as st
import numpy as np
import torch
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

# Add project root to sys.path to allow imports from other packages
root_path = Path(__file__).resolve().parent.parent
if str(root_path) not in sys.path:
	sys.path.insert(0, str(root_path))

from scripts.aggregate_run_options import aggregate_run_options
from viewers.compare_runs_shared import (
	render_run_grid,
	load_preferences,
	save_preference,
	RunSelectorSidebar,
)
from simulate_trading import (
	generate_simulations_from_directories,
	get_single_run_directory,
	_find_score_file,
	_get_cached_entries,
	get_connection,
)
from StockRecommenderFromModel import StockRecommenderFromSavedScores
from trainer.state.single_run import SingleRun
from training_instance_provider import TrainingInstanceProvider
from trainer.datasets import IndexDataset
from viewers.compare_run_statistics_viewer import _compute_run_statistics_for_path
from scripts.get_metrics_for_runs import get_metrics_for_runs
from viewers.populate_runs_viewer import build_metrics_df_for_run
import plotly.express as px
import plotly.graph_objects as go


def run_selection_dashboard():
	st.set_page_config(page_title="Run Selection Dashboard", layout="wide")
	st.title("Run Selection Dashboard")

	# Use the shared RunSelectorSidebar
	selector = RunSelectorSidebar(key_prefix="rsd")
	matching_runs_ids = selector.render()

	if matching_runs_ids is not None:
		base_dir = st.session_state.get("rsd_base_dir")

		# Fetch detailed run data
		with st.spinner("Scanning runs for details..."):
			# Instead of scan_runs which finds individual reps, we want SingleRun objects.
			# We can find all single_run_*.json files.
			single_run_files = []
			for root, dirs, files in os.walk(base_dir):
				found_single_run = False
				for file in files:
					if file.startswith("single_run_") and file.endswith(".json"):
						single_run_files.append(os.path.join(root, file))
						found_single_run = True

				if found_single_run:
					# If there is a single_run object found at a given directory,
					# then subdirectories of that directory should not be checked.
					dirs[:] = []

		# Filter and load SingleRun data
		display_runs = []
		for file_path in single_run_files:
			try:
				with open(file_path, "r", encoding="utf-8") as f:
					data = json.load(f)

				family_data = data.get("family_run", {})
				single_data = data.get("single_run", {})

				ts_id = single_data.get("timestamp_id") or single_data.get("run_timestamp")

				# Filter by matching_runs_ids if criteria are selected
				if matching_runs_ids is not None and ts_id not in matching_runs_ids:
					continue

				# Prepare row for DataFrame
				# Merge data: single_run values should take precedence
				row = {}
				row.update(family_data)
				row.update(single_data)

				# Process hidden_layer_size: if it's a list, concatenate it into a single string
				if "hidden_layer_size" in row and isinstance(row["hidden_layer_size"], list):
					# Concatenate the list into a single string for better UI representation
					# E.g., [2048, 1024] -> "2048,1024"
					row["hidden_layer_size"] = ",".join(map(str, row["hidden_layer_size"]))

				# Check for score files to include in the grid/filtering
				# Now handled by aggregate_run_options, but we can also check here if needed for consistency
				# with the data frame we're building.
				has_score_files = False
				dir_path = os.path.dirname(file_path)
				try:
					for sub_root, sub_dirs, sub_files in os.walk(dir_path):
						for f_name in sub_files:
							if f_name.startswith("stock_scores_epoch_") and (f_name.endswith(".csv") or f_name.endswith(".pt")):
								has_score_files = True
								break
						if has_score_files:
							break
				except Exception:
					pass
				row["has_score_files"] = has_score_files

				# Use relative path of the directory as the "run" identifier for render_run_grid
				rel_path = os.path.relpath(os.path.dirname(file_path), base_dir)
				row["run"] = rel_path
				row["_file_path"] = file_path

				# Add some helper fields for navigation
				# Infer family from path if possible
				parts = Path(rel_path).parts
				if len(parts) > 1 and parts[0].startswith("run_family_"):
					row["_family"] = parts[0]
				else:
					row["_family"] = "None"
				row["_ts"] = ts_id

				# Add statistics if available
				try:
					stats = _compute_run_statistics_for_path(os.path.dirname(file_path))
					if stats:
						row.update(stats)
				except Exception:
					pass

				display_runs.append(row)
			except Exception as e:
				st.error(f"Error loading {file_path}: {e}")

		if not display_runs:
			st.info("No single run data found for the selected criteria.")
			return

		df = pd.DataFrame(display_runs)

		# Drop complex/unwanted columns for cleaner display
		cols_to_drop = [
			"file_structure", "single_runs_for_all_combinations", "dbinfo",
			"activation_list", "base_args", "base_args_list", "base_optional_features",
			"completed", "current_index", "day_change_centered_list", "dbinfo_file", "device",
			"epochs_list", "final_activation_list", "include_one_feature_list", "initial_run_id", "input_size", "limit",
			"log_interval", "lr_list", "mlp_layers_list", "optimizer_list",
			"output_size", "prior_days_list", "ticker_ids", "trading_interval",
			"bias_list", "layer_normalization_list", "learning_rate_adjustment_parameter_list"
		]
		df = df.drop(columns=[c for c in cols_to_drop if c in df.columns])

		# Display the grid
		st.subheader(f"Matching Single Runs ({len(df)})")

		prefs = load_preferences()
		selected_in_grid = render_run_grid(
			df=df,
			key_prefix="rsd",
			prefs=prefs,
			default_visible_cols=["run", "day_change_centered", "prior_days", "mlp_layers", "lr", "activation", "epochs", "epoch", "rank1", "rank5"]
		)

		if selected_in_grid:
			st.write(f"Selected {len(selected_in_grid)} runs in grid.")

			# Extract family and timestamp information for navigation
			selected_df = df[df['run'].isin(selected_in_grid)]

			selected_families = sorted(list(set(selected_df['_family'].dropna())))
			selected_timestamps = sorted(list(set(selected_df['_ts'].dropna())))

			col1, col2, col3, col4, col5 = st.columns([1, 1, 1, 1, 0.5])

			if col1.button("Basic Info", help="Open selected runs in Compare Runs Viewer"):
				st.session_state["crv_families"] = selected_families
				st.session_state["crv_builds"] = selected_timestamps
				try:
					st.switch_page("compare_runs")
				except Exception as e:
					st.error(f"Could not open Compare Runs Viewer: {e}. Please ensure it is registered as a page.")

			if col2.button("Statistics", help="Open selected runs in Compare Run Statistics Viewer"):
				st.session_state["crsv_families"] = selected_families
				st.session_state["crsv_builds"] = selected_timestamps
				try:
					st.switch_page("compare_statistics")
				except Exception as e:
					st.error(f"Could not open Statistics Viewer: {e}. Please ensure it is registered as a page.")

			if col3.button("Examine Models", help="Open selected runs in Examine Models Viewer"):
				st.session_state["emv_families"] = selected_families
				st.session_state["emv_builds"] = selected_timestamps
				try:
					st.switch_page("examine_models")
				except Exception as e:
					st.error(f"Could not open Examine Models Viewer: {e}. Please ensure it is registered as a page.")

			force_regen = col5.checkbox("force", help="Force the regeneration of all selected simulations", key="rsd_force_regen")
			if col4.button("Generate simulations from saved scores", help="For selected runs, look for saved scores and generate stock_simulation_*.csv files"):
				status_text = st.empty()
				# Clear cache to ensure simulations are picked up
				st.cache_data.clear()
				# Build absolute directories for each selected run
				paths_to_process = []
				try:
					for _, row in selected_df.iterrows():
						file_path = row.get("_file_path")
						if not file_path:
							continue
						# Directory containing the single_run_*.json
						run_dir = os.path.dirname(os.path.join(base_dir, os.path.relpath(file_path, base_dir)))
						# Normalize to the single run directory (handles nested selections safely)
						run_dir = get_single_run_directory(run_dir)
						if os.path.isdir(run_dir) and run_dir not in paths_to_process:
							paths_to_process.append(run_dir)
				except Exception as e:
					st.error(f"Error while preparing simulation directories: {e}")

				if not paths_to_process:
					st.warning("No valid run directories found for the current selection.")
				else:
					status_text.text(f"Generating simulations for {len(paths_to_process)} directories...")
					try:
						generate_simulations_from_directories(paths_to_process, top_k=[1, 3, 5, 10], force=force_regen)
						status_text.text("Done! Refreshing viewer...")
						st.rerun()
					except Exception as e:
						traceback.print_exception(e)

		# --- Graphing Section ---
		st.divider()
		st.header("Analyze Results")

		def make_hashable(val):
			if isinstance(val, (list, dict, set)):
				return str(val)
			return val

		# Determine available metrics and variables
		# For variables, we can use the columns in the dataframe except for internal ones
		all_cols = df.columns.tolist()
		internal_cols = ["run", "_file_path", "_family", "_ts", "run_id"]

		# For metrics, we need to scan the selected (or all matching) runs
		runs_to_analyze = selected_df if not selected_df.empty else df

		# Calculate which fields vary across the SELECTED runs
		# A field is included if it is NOT in skip_fields AND has > 1 unique value in runs_to_analyze
		fields_with_options = []
		# Fields we typically want to skip in the "Compare Parameters" section
		skip_fields = [
			"run_timestamp", "timestamp_id", "last_save_location",
			"activation_list", "base_args", "base_args_list", "base_optional_features",
			"completed", "current_index", "day_change_centered_list", "dbinfo_file", "device",
			"epochs_list", "final_activation_list", "include_one_feature_list", "initial_run_id", "input_size", "limit",
			"log_interval", "lr_list", "mlp_layers_list", "optimizer_list",
			"output_size", "prior_days_list", "ticker_ids", "trading_interval",
			"file_structure", "single_runs_for_all_combinations", "dbinfo",
			"_family", "has_score_files"
		]
		for field in df.columns:
			if field in skip_fields:
				continue

			if field in internal_cols or field.startswith("_"):
				continue

			# Check if this field exists in the dataframe and has more than one unique value
			if field in runs_to_analyze.columns:
				try:
					unique_vals = runs_to_analyze[field].nunique()
				except (TypeError, ValueError):
					# If column contains unhashable types (like lists), nunique fails.
					# We can try to count unique values by converting to string/tuple.
					unique_vals = len(runs_to_analyze[field].apply(make_hashable).unique())

				if unique_vals > 1:
					fields_with_options.append(field)

		variable_options = sorted(fields_with_options)

		# Persistence for graphing options
		if "rsd_graph_ensemble" not in st.session_state:
			st.session_state["rsd_graph_ensemble"] = load_preferences().get("rsd_graph_ensemble", "individual")
		if "rsd_graph_topk" not in st.session_state:
			st.session_state["rsd_graph_topk"] = load_preferences().get("rsd_graph_topk", "top5")
		if "rsd_graph_metric" not in st.session_state:
			st.session_state["rsd_graph_metric"] = load_preferences().get("rsd_graph_metric", "portfolio_value")
		if "rsd_graph_agg" not in st.session_state:
			st.session_state["rsd_graph_agg"] = load_preferences().get("rsd_graph_agg", "ewa")
		if "rsd_plot_type" not in st.session_state:
			st.session_state["rsd_plot_type"] = load_preferences().get("rsd_plot_type", "Scatter")
		if "rsd_selected_x_var" not in st.session_state:
			st.session_state["rsd_selected_x_var"] = load_preferences().get("rsd_selected_x_var", variable_options[0] if variable_options else None)
		if "rsd_log_x_axis" not in st.session_state:
			st.session_state["rsd_log_x_axis"] = load_preferences().get("rsd_log_x_axis", False)

		# get_metrics_for_runs is now imported from scripts.get_metrics_for_runs

		st.markdown("### Analyze Results")

		# New split combo boxes
		c1, c2, c3, c4, c5 = st.columns(5)

		# 1. Determine grouping size range
		# Collect run paths for all runs_to_analyze
		run_paths = [os.path.join(base_dir, rid) for rid in runs_to_analyze["run"].tolist()]
		run_rep_counts = []
		for rp in run_paths:
			try:
				reps = [d for d in os.listdir(rp) if os.path.isdir(os.path.join(rp, d)) and (d.startswith("rep") or d.startswith("run_single"))]
				if reps:
					run_rep_counts.append(len(reps))
				else:
					# Check if the directory itself is a single run (no reps subdirs)
					# We'll treat it as 1 rep if it has epoch files
					from viewers.populate_statistics_viewer import _find_epoch_files
					if _find_epoch_files(rp):
						run_rep_counts.append(1)
			except:
				pass

		min_reps = min(run_rep_counts) if run_rep_counts else 0

		if "rsd_grouping_size" not in st.session_state:
			st.session_state["rsd_grouping_size"] = load_preferences().get("rsd_grouping_size", 0)

		grouping_size = c1.selectbox(
			"Result Grouping Size",
			options=list(range(min_reps + 1)),
			index=st.session_state["rsd_grouping_size"] if st.session_state["rsd_grouping_size"] <= min_reps else 0,
			key="rsd_grouping_size_ui"
		)
		if grouping_size != st.session_state["rsd_grouping_size"]:
			st.session_state["rsd_grouping_size"] = grouping_size
			save_preference("rsd_grouping_size", grouping_size)
			st.rerun()

		ensemble_modes = ["individual", "norm", "sum"]
		if grouping_size != 1:
			ensemble_modes += [
				"sum random", "sum partial random",
				"rank random", "rank partial random",
				"norm random", "norm partial random"
			]

		ensemble_opt = c2.selectbox(
			"Ensemble Mode",
			options=ensemble_modes,
			key="rsd_graph_ensemble_ui",
			index=ensemble_modes.index(st.session_state["rsd_graph_ensemble"]) if st.session_state["rsd_graph_ensemble"] in ensemble_modes else 0,
			disabled=(grouping_size == 1)
		)
		if ensemble_opt != st.session_state["rsd_graph_ensemble"]:
			st.session_state["rsd_graph_ensemble"] = ensemble_opt
			save_preference("rsd_graph_ensemble", ensemble_opt)
			st.rerun()

		# Determine available TopK options from data? Or just provide common ones.
		topk_options = ["top1", "top3", "top5", "top10", "top20", "top50", "top100"]
		topk_opt = c3.selectbox("Top Stocks (K)", options=topk_options,
								key="rsd_graph_topk_ui",
								index=topk_options.index(st.session_state["rsd_graph_topk"]) if st.session_state["rsd_graph_topk"] in topk_options else 2)
		if topk_opt != st.session_state["rsd_graph_topk"]:
			st.session_state["rsd_graph_topk"] = topk_opt
			save_preference("rsd_graph_topk", topk_opt)
			st.rerun()

		metric_type_options = ["portfolio_value", "recommendation_distribution"]
		metric_type_opt = c4.selectbox("Metric Type", options=metric_type_options,
									  key="rsd_graph_metric_ui",
									  index=metric_type_options.index(st.session_state["rsd_graph_metric"]) if st.session_state["rsd_graph_metric"] in metric_type_options else 0)
		if metric_type_opt != st.session_state["rsd_graph_metric"]:
			st.session_state["rsd_graph_metric"] = metric_type_opt
			save_preference("rsd_graph_metric", metric_type_opt)
			st.rerun()

		agg_options = ["ewa"]
		agg_opt = c5.selectbox("Aggregation", options=agg_options,
							   key="rsd_graph_agg_ui",
							   index=agg_options.index(st.session_state["rsd_graph_agg"]) if st.session_state["rsd_graph_agg"] in agg_options else 0)
		if agg_opt != st.session_state["rsd_graph_agg"]:
			st.session_state["rsd_graph_agg"] = agg_opt
			save_preference("rsd_graph_agg", agg_opt)
			st.rerun()

		with st.spinner("Extracting metrics for graphing..."):
			# Try to get metrics. If they don't exist, we'll try to generate them.
			run_metrics, metric_options = get_metrics_for_runs(
				runs_to_analyze["run"].tolist(),
				base_dir,
				st.session_state["rsd_graph_ensemble"],
				st.session_state["rsd_graph_topk"],
				st.session_state["rsd_graph_metric"],
				st.session_state["rsd_graph_agg"],
				st.session_state["rsd_grouping_size"]
			)

		if not metric_options:
			# If no metrics found, maybe we need to generate simulations?
			st.info("No metrics found for the selected options and runs. Attempting to generate simulations...")
			generate_simulations_from_directories(
				[os.path.join(base_dir, rid) for rid in runs_to_analyze["run"].tolist()],
				top_k=[1, 3, 5, 10, 20, 50, 100],
				force=False
			)
			# Refresh metrics after generation
			run_metrics, metric_options = get_metrics_for_runs(
				runs_to_analyze["run"].tolist(),
				base_dir,
				st.session_state["rsd_graph_ensemble"],
				st.session_state["rsd_graph_topk"],
				st.session_state["rsd_graph_metric"],
				st.session_state["rsd_graph_agg"],
				st.session_state["rsd_grouping_size"]
			)

		if not metric_options:
			st.info("No metrics found for the selected options and runs.")
		else:
			col_x, col_s, col_p, col_l = st.columns([1, 1, 1, 0.5])
			selected_metric = metric_options[0] # Usually only one if we filtered correctly in get_metrics_for_runs
			if len(metric_options) > 1:
				 # Fallback if multiple metrics are generated (e.g. if we add more in future)
				 selected_metric = col_x.selectbox("Select specific metric", options=metric_options)

			if not variable_options:
				st.info("No varying variables found among selected runs to graph against.")
				return

			selected_x_var = col_x.selectbox(
				"Select variable to graph against (X-axis)",
				options=variable_options,
				index=variable_options.index(st.session_state["rsd_selected_x_var"]) if st.session_state["rsd_selected_x_var"] in variable_options else 0
			)
			if selected_x_var != st.session_state["rsd_selected_x_var"]:
				st.session_state["rsd_selected_x_var"] = selected_x_var
				save_preference("rsd_selected_x_var", selected_x_var)
				st.rerun()

			hold_steady_vars = col_s.multiselect("Variables to hold steady (separate graph for each combination)", options=[v for v in variable_options if v != selected_x_var])

			plot_type_options = ["Scatter", "Box"]
			plot_type = col_p.radio(
				"Plot Type",
				options=plot_type_options,
				index=plot_type_options.index(st.session_state["rsd_plot_type"]) if st.session_state["rsd_plot_type"] in plot_type_options else 0,
				horizontal=True
			)
			if plot_type != st.session_state["rsd_plot_type"]:
				st.session_state["rsd_plot_type"] = plot_type
				save_preference("rsd_plot_type", plot_type)
				st.rerun()

			log_x_axis = col_l.checkbox("Log X-axis", value=st.session_state["rsd_log_x_axis"])
			if log_x_axis != st.session_state["rsd_log_x_axis"]:
				st.session_state["rsd_log_x_axis"] = log_x_axis
				save_preference("rsd_log_x_axis", log_x_axis)
				st.rerun()

			# Prepare data for graphing
			graph_data = []
			missing_dirs = []
			for _, row in runs_to_analyze.iterrows():
				run_id = row["run"]
				if run_id in run_metrics:
					m_data = run_metrics[run_id]
					if isinstance(m_data, list):
						# Multiple reps
						run_dir = os.path.join(base_dir, run_id)
						try:
							reps_on_disk = [d for d in os.listdir(run_dir) if os.path.isdir(os.path.join(run_dir, d)) and (d.startswith("rep") or d.startswith("run_single"))]
						except:
							reps_on_disk = []

						if not m_data:
							# No metrics for any rep
							if reps_on_disk:
								for rep in reps_on_disk:
									missing_dirs.append(os.path.join(run_id, rep))
							else:
								missing_dirs.append(run_id)
						else:
							# Some reps have metrics, check if all reps on disk are present
							# m_data is a list of dicts. We don't have rep folder names in m_data directly from get_metrics_for_runs
							# but we can check the count.
							for rep_stats in m_data:
								if selected_metric in rep_stats:
									metric_val = rep_stats[selected_metric]
									data_point = {
										"metric": metric_val,
										"x_var": make_hashable(row.get(selected_x_var)),
										"run_id": run_id
									}
									for hsv in hold_steady_vars:
										data_point[hsv] = make_hashable(row.get(hsv))
									graph_data.append(data_point)

							# If individual mode, we'd like to know which reps are missing.
							# get_metrics_for_runs processes reps in sorted order.
							if len(m_data) < len(reps_on_disk) and st.session_state["rsd_graph_ensemble"] == "individual":
								missing_dirs.append(f"{run_id} ({len(reps_on_disk) - len(m_data)} reps missing)")
					else:
						# Single ensemble result
						if selected_metric in m_data:
							metric_val = m_data[selected_metric]
							data_point = {
								"metric": metric_val,
								"x_var": make_hashable(row.get(selected_x_var)),
								"run_id": run_id
							}
							for hsv in hold_steady_vars:
								data_point[hsv] = make_hashable(row.get(hsv))
							graph_data.append(data_point)
						else:
							missing_dirs.append(run_id)
				else:
					missing_dirs.append(run_id)

			if not graph_data:
				st.warning("No data points available for the selected metric and variables.")
				if missing_dirs:
					st.write("Missing directories:")
					for d in missing_dirs:
						st.write(f"- {d}")
			else:
				gdf = pd.DataFrame(graph_data)

				# --- Sanity Check Counts ---
				num_single_runs = len(runs_to_analyze)
				num_reps = 0
				for rp in runs_to_analyze["run"].tolist():
					run_dir = os.path.join(base_dir, rp)
					try:
						reps = [d for d in os.listdir(run_dir) if os.path.isdir(os.path.join(run_dir, d)) and (d.startswith("rep") or d.startswith("run_single"))]
						num_reps += len(reps) if reps else 1 # count the dir itself as 1 if no rep subdirs
					except:
						num_reps += 1

				if st.session_state["rsd_graph_ensemble"] == "individual":
					expected_points = num_reps
				else:
					expected_points = num_single_runs

				def make_hashable(val):
					if isinstance(val, (list, dict, set)):
						return str(val)
					return val

				# Grouping by hold_steady_vars
				if not hold_steady_vars:
					groups = [("All Data", gdf)]
				else:
					# Fill Nones/NaNs with "Not Set" for consistent grouping
					# And convert unhashable types (like lists) to strings
					gdf_grouped = gdf.copy()

					for hsv in hold_steady_vars:
						gdf_grouped[hsv] = gdf_grouped[hsv].fillna("Not Set")

					groups = []
					for name, group in gdf_grouped.groupby(hold_steady_vars):
						if isinstance(name, (tuple, list)):
							group_name = ", ".join([f"{hsv}={val}" for hsv, val in zip(hold_steady_vars, name)])
						else:
							group_name = f"{hold_steady_vars[0]}={name}"
						groups.append((group_name, group))

				for group_name, group in groups:
					st.subheader(group_name)

					# Check if x_var is numeric to decide between scatter and box/bar
					# Note: categorical x_var works fine with scatter if we want individual points
					# box plot is good if multiple runs have the same x_var value.

					# Attempt to convert to numeric if possible
					group_for_plot = group.copy()
					x_col = "x_var"

					# If all values in x_col can be converted to numeric, do so.
					# This handles cases where numbers are stored as strings.
					try:
						group_for_plot[x_col] = pd.to_numeric(group_for_plot[x_col], errors='raise')
					except (ValueError, TypeError):
						# Keep as is if it cannot be converted to numeric
						pass

					try:
						group_for_plot = group_for_plot.sort_values(x_col)
					except:
						pass

					# Detect if x_var is categorical
					is_categorical = not pd.api.types.is_numeric_dtype(group_for_plot[x_col])

					if plot_type == "Box":
						# Use box plot
						fig = px.box(group_for_plot, x=x_col, y="metric",
									 hover_data=["run_id"],
									 log_x=st.session_state["rsd_log_x_axis"] if not is_categorical else False,
									 points=False,
									 labels={x_col: selected_x_var, "metric": selected_metric})
					else:
						# Use scatter plot
						fig = px.scatter(group_for_plot, x=x_col, y="metric",
										 hover_data=["run_id"],
										 log_x=st.session_state["rsd_log_x_axis"] if not is_categorical else False,
										 labels={x_col: selected_x_var, "metric": selected_metric})

					if is_categorical:
						fig.update_xaxes(type='category')

					st.plotly_chart(fig, use_container_width=True)

					# Display sanity check info at the bottom of the graph
					actual_points = len(group)
					# If there's only one group, we use the total expected.
					# If multiple groups, it's harder to know 'expected' per group without more complex logic.
					# But the user asked for "at the bottom of the graph the number, and if they match".

					if not hold_steady_vars:
						match_str = "✅ Matches" if actual_points == expected_points else f"❌ Mismatch (Expected {expected_points})"
						st.info(f"Sanity Check: {num_single_runs} Single Runs, {num_reps} Reps. Data points: {actual_points}. {match_str}")
						if actual_points != expected_points and missing_dirs:
							st.write("Missing directories:")
							for d in missing_dirs:
								st.write(f"- {d}")
					else:
						st.info(f"Group points: {actual_points}")

				if hold_steady_vars:
					total_actual = len(gdf)
					match_str = "✅ Matches" if total_actual == expected_points else f"❌ Mismatch (Expected {expected_points})"
					st.info(f"Total Sanity Check: {num_single_runs} Single Runs, {num_reps} Reps. Total data points: {total_actual}. {match_str}")
					if total_actual != expected_points and missing_dirs:
						st.write("Missing directories:")
						for d in missing_dirs:
							st.write(f"- {d}")

if __name__ == "__main__":
	run_selection_dashboard()
