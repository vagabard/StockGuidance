import os
import sys
import json
import pandas as pd
import streamlit as st
from pathlib import Path

# Add project root to sys.path to allow imports from other packages
root_path = Path(__file__).resolve().parent.parent
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from scripts.aggregate_run_options import aggregate_run_options
from viewers.compare_runs_shared import (
    render_run_grid,
    load_preferences,
    save_preference,
)
from simulate_trading import generate_simulations_from_directories, get_single_run_directory
from viewers.compare_run_statistics_viewer import _compute_run_statistics_for_path
from viewers.populate_runs_viewer import build_metrics_df_for_run
import plotly.express as px
import plotly.graph_objects as go

def run_selection_dashboard():
    st.set_page_config(page_title="Run Selection Dashboard", layout="wide")
    st.title("Run Selection Dashboard")

    # Sidebar: Base directory
    default_base = r"C:\Projects\StockGuidance\data"
    if not os.path.isdir(default_base):
        default_base = os.path.join(os.getcwd(), "data")

    base_dir = st.sidebar.text_input("Base data directory", value=default_base)
    st.session_state["rsd_base_dir"] = base_dir

    if not os.path.exists(base_dir):
        st.error(f"Directory not found: {base_dir}")
        return

    # Load aggregated options
    with st.spinner("Aggregating run options..."):
        aggregated_options = aggregate_run_options(base_dir)

    if not aggregated_options:
        st.warning("No runs found with aggregateable options.")
        return

    # Sort fields for consistent UI
    fields = sorted(aggregated_options.keys())

    # 0. Family Run selection
    family_options = sorted(list(aggregated_options.get("_family", {}).keys()), reverse=True)
    if family_options:
        st.sidebar.subheader("Filter by Family Run")
        selected_families = st.sidebar.multiselect("Select Family Runs (Optional)", options=family_options, key="rsd_selected_families")
    else:
        selected_families = []

    # 0b. Filter by Score Files
    score_file_options = sorted(list(aggregated_options.get("has_score_files", {}).keys()))
    if score_file_options:
        st.sidebar.subheader("Filter by Score Files")
        # If both True and False are available, default to showing everything, but let user choose.
        # Convert to list to ensure we can use it in multiselect
        selected_score_files = st.sidebar.multiselect("Has Score Files?", options=score_file_options, key="filter_has_score_files")
    else:
        selected_score_files = []

    selected_criteria = {}

    # We'll skip some fields that might be too noisy or redundant
    skip_fields = [
        "run_timestamp", "timestamp_id", "last_save_location",
        "activation_list", "base_args", "base_args_list", "base_optional_features",
        "completed", "current_index", "day_change_centered_list", "dbinfo_file", "device",
        "final_activation_list", "include_one_feature_list", "initial_run_id", "input_size", "limit",
        "log_interval", "lr_list", "mlp_layers_list", "optimizer_list",
        "output_size", "prior_days_list", "ticker_ids", "trading_interval",
        "file_structure", "single_runs_for_all_combinations", "dbinfo",
        "_family", "has_score_files"
    ]

    extended_attributes = [
        "pin_GPU_memory",
        "raw_train_end_with_special_characters",
        "raw_train_start_with_special_characters",
        "train_percent",
        "train_validation_test_sets_end_date_id",
        "train_validation_test_sets_start_date_id",
        "val_percent"
    ]

    # Gather all unique run IDs to detect missing fields
    all_run_ids = set()
    for field_data in aggregated_options.values():
        for run_list in field_data.values():
            all_run_ids.update(run_list)

    # Filter all_run_ids by selected families if any
    if selected_families:
        filtered_run_ids = set()
        for family in selected_families:
            filtered_run_ids.update(aggregated_options.get("_family", {}).get(family, []))
        all_run_ids &= filtered_run_ids

    # 1. Pre-process ALL fields to include "Not Set" and handle skip_fields/extended_attributes
    for field in list(aggregated_options.keys()):
        field_options_dict = aggregated_options[field]
        runs_with_field = set()
        for run_list in field_options_dict.values():
            runs_with_field.update(run_list)

        missing_runs = all_run_ids - runs_with_field
        if missing_runs:
            if "Not Set" not in aggregated_options[field]:
                aggregated_options[field]["Not Set"] = list(missing_runs)

    # We need to know which fields are being displayed to collect their selections
    active_fields = [f for f in fields if f not in skip_fields]

    # Calculate matching runs for currently selected values
    field_matching_runs = {}
    for field in active_fields:
        key = f"filter_{field}"
        selected = st.session_state.get(key, [])
        if selected:
            matches = set()
            for val in selected:
                matches.update(aggregated_options[field].get(val, []))
            field_matching_runs[field] = matches

    def get_final_dynamic_options(field):
        # Intersection of matches from all OTHER fields
        others_matches = all_run_ids.copy()
        for f, matches in field_matching_runs.items():
            if f != field:
                others_matches &= matches

        available = []
        for val, runs in aggregated_options[field].items():
            if any(r in others_matches for r in runs):
                available.append(val)

        # Always include currently selected values to avoid Streamlit errors
        # If the user has something selected that is no longer valid given other filters,
        # we still show it in the list so it doesn't just disappear and throw an error.
        current_selection = st.session_state.get(f"filter_{field}", [])
        for sel in current_selection:
            if sel not in available:
                available.append(sel)

        available = sorted(list(set(available)), key=lambda x: str(x))
        if "Not Set" in available:
            available.remove("Not Set")
            available.insert(0, "Not Set")
        return available

    st.sidebar.subheader("Filter Criteria")
    selected_criteria = {}

    for field in fields:
        if field in skip_fields or field in extended_attributes:
            continue

        options = sorted(list(aggregated_options[field].keys()), key=lambda x: str(x))
        if len(options) <= 1:
            continue

        dynamic_options = get_final_dynamic_options(field)

        selected = st.sidebar.multiselect(f"Select {field}", options=dynamic_options, key=f"filter_{field}")
        if selected:
            selected_criteria[field] = selected

    # Extended Attributes section
    with st.sidebar.expander("Extended Attributes"):
        for field in fields:
            if field not in extended_attributes:
                continue

            options = sorted(list(aggregated_options[field].keys()), key=lambda x: str(x))
            if len(options) <= 1:
                continue

            dynamic_options = get_final_dynamic_options(field)

            selected = st.multiselect(f"Select {field}", options=dynamic_options, key=f"filter_{field}")
            if selected:
                selected_criteria[field] = selected

    # Button to trigger grid population
    show_runs = st.button("Show Single Runs")

    # We want to keep the grid visible if it was already triggered,
    # but Streamlit reruns everything. However, the requirement says
    # "this grid should not be populated until a button show single runs is pushed".
    # We can use session state to keep track if we should show it.
    if "show_grid" not in st.session_state:
        st.session_state.show_grid = False

    # If selections change, we might want to hide the grid until button is pushed again.
    # But usually users expect the grid to update if it's already shown.
    # However, the requirement says "this grid should not be populated until a button show single runs is pushed".
    # This could mean every time you want to see the UPDATED grid, you push the button.

    # Let's check if criteria changed since last button push.
    if "last_criteria" not in st.session_state:
        st.session_state.last_criteria = {}

    if show_runs:
        st.session_state.show_grid = True
        st.session_state.last_criteria = selected_criteria.copy()
        st.session_state.last_selected_families = list(selected_families)

    # If criteria changed and we are showing the grid, we could either hide it or keep showing old one.
    # Given the wording, "should not be populated until a button... is pushed",
    # if criteria changed, the CURRENT grid might be invalid.
    if st.session_state.show_grid and (st.session_state.last_criteria != selected_criteria or st.session_state.get("last_selected_families") != list(selected_families)):
         st.warning("Criteria changed. Push 'Show Single Runs' to update the grid.")
         # Optional: st.session_state.show_grid = False # If we want to strictly follow "should not be populated"

    if st.session_state.show_grid:
        # Filter timestamp_ids
        # We use last_criteria to filter, so it matches what was there when button was pushed
        criteria_to_use = st.session_state.last_criteria
        matching_runs_ids = None

        # Apply Family selection filter FIRST from when button was pushed
        last_selected_families = st.session_state.get("last_selected_families", [])
        if last_selected_families:
            matching_runs_ids = set()
            for family in last_selected_families:
                matching_runs_ids.update(aggregated_options.get("_family", {}).get(family, []))

        for field, values in criteria_to_use.items():
            current_field_matches = set()
            for val in values:
                current_field_matches.update(aggregated_options[field][val])

            if matching_runs_ids is None:
                matching_runs_ids = current_field_matches
            else:
                matching_runs_ids = matching_runs_ids.intersection(current_field_matches)

        if matching_runs_ids is not None and not matching_runs_ids:
            st.info("No runs match all selected criteria.")
            return

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
            "final_activation_list", "include_one_feature_list", "initial_run_id", "input_size", "limit",
            "log_interval", "lr_list", "mlp_layers_list", "optimizer_list",
            "output_size", "prior_days_list", "ticker_ids", "trading_interval"
        ]
        df = df.drop(columns=[c for c in cols_to_drop if c in df.columns])

        # Display the grid
        st.subheader(f"Matching Single Runs ({len(df)})")

        prefs = load_preferences()
        selected_in_grid = render_run_grid(
            df=df,
            key_prefix="rsd",
            prefs=prefs,
            default_visible_cols=["run", "day_change_centered", "prior_days", "mlp_layers", "lr", "activation", "rank1", "rank5"]
        )

        if selected_in_grid:
            st.write(f"Selected {len(selected_in_grid)} runs in grid.")

            # Extract family and timestamp information for navigation
            selected_df = df[df['run'].isin(selected_in_grid)]

            selected_families = sorted(list(set(selected_df['_family'].dropna())))
            selected_timestamps = sorted(list(set(selected_df['_ts'].dropna())))

            col1, col2, col3 = st.columns(3)

            if col1.button("Basic Info", help="Open selected runs in Compare Runs Viewer"):
                st.session_state["crv_families"] = selected_families
                st.session_state["crv_builds"] = selected_timestamps
                try:
                    st.switch_page("pages/compare_runs.py")
                except Exception as e:
                    st.error(f"Could not open Compare Runs Viewer: {e}. Please ensure it is registered as a page.")

            if col2.button("Statistics", help="Open selected runs in Compare Run Statistics Viewer"):
                st.session_state["crsv_families"] = selected_families
                st.session_state["crsv_builds"] = selected_timestamps
                try:
                    st.switch_page("pages/compare_statistics.py")
                except Exception as e:
                    st.error(f"Could not open Statistics Viewer: {e}. Please ensure it is registered as a page.")

            if col3.button("Generate simulations from saved scores", help="For selected runs, look for saved scores and generate stock_simulation_*.csv files"):
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
                        generate_simulations_from_directories(paths_to_process, top_k=[1, 3, 5, 10], force=False)
                        status_text.text("Done! Refreshing viewer...")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error during simulation generation: {e}")

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
        for field in fields:
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

        @st.cache_data
        def get_metrics_for_runs(run_ids: list[str], base_dir: str, ensemble_mode: str, topk_mode: str, metric_type: str, agg_mode: str):
            all_metrics = {} # run_id -> dict of metrics OR list of dicts for individual reps
            available_metric_names = set()
            for rid in run_ids:
                run_dir = os.path.join(base_dir, rid)

                from viewers.populate_statistics_viewer import build_run_epoch_series

                # Map topk_mode string to key in by_topn
                # by_topn keys are 0 (ensemble) or (K, Method)
                k_val = int(topk_mode.replace("top", ""))
                target_keys = []
                if ensemble_mode == "individual":
                    target_keys = [(k_val, ""), (k_val, None), (k_val, "Rank"), (k_val, "Sum"), (k_val, "Norm")]
                    if k_val == 1:
                        target_keys.append(0)
                elif ensemble_mode == "norm":
                    target_keys = [(k_val, "Norm")]
                elif ensemble_mode == "sum":
                    target_keys = [(k_val, "Sum")]

                def extract_ewa(by_topn, by_topn_rank):
                    stats = {}
                    source_map = by_topn if metric_type == "portfolio_value" else by_topn_rank
                    for key in target_keys:
                        epochs_map = source_map.get(key)
                        if not epochs_map:
                            continue
                        sorted_epochs = sorted(epochs_map.keys())
                        if not sorted_epochs:
                            continue

                        val_to_store = None
                        if agg_mode == "ewa":
                            ewa = 0.0
                            for count, epoch in enumerate(sorted_epochs, 1):
                                val = epochs_map[epoch]
                                ewa = (ewa * (count - 1) + val) / count
                            val_to_store = ewa

                        if val_to_store is not None:
                            m_suffix = f"_{ensemble_mode}" if ensemble_mode != "individual" else ""
                            # If individual, also check if this key represents Sum, Norm or Rank (e.g. from ensemble fallback)
                            # to avoid confusion, but primarily we are looking for the plain (k, "") key.
                            if ensemble_mode == "individual":
                                if isinstance(key, tuple) and len(key) == 2 and key[1]:
                                    m_suffix = f"_{key[1].lower()}"
                                elif key == 0:
                                     m_suffix = "_ensemble"

                            metric_name = f"{metric_type}_{topk_mode}{m_suffix}_{agg_mode}"
                            stats[metric_name] = val_to_store
                    return stats

                if ensemble_mode == "individual":
                    rep_metrics = []
                    # Check for rep subdirectories
                    try:
                        # Normalize path for comparison
                        norm_run_dir = os.path.normpath(run_dir)

                        items = sorted(os.listdir(run_dir))
                        # Optimization: if we have MANY items, this might be slow.
                        # But typically it's < 100 reps.
                        for item in items:
                            item_path = os.path.join(run_dir, item)
                            if os.path.isdir(item_path) and (item.startswith("rep") or item.startswith("run_single")):
                                # If it's a run_single_* within another run_single_*, something is wrong,
                                # but usually it's reps.
                                rep_by_topn, _, _, rep_by_topn_rank = build_run_epoch_series(item_path)
                                stats = extract_ewa(rep_by_topn, rep_by_topn_rank)
                                if stats:
                                    rep_metrics.append(stats)
                                    available_metric_names.update(stats.keys())

                        # ALSO check for ensemble files in the run_dir itself ONLY if no reps were found.
                        # In some family runs, the "single run" directory contains the ENSEMBLE of its reps.
                        # If reps exist, we usually only want the individual rep metrics for "individual" mode.
                        if not rep_metrics:
                            by_topn, _, _, by_topn_rank = build_run_epoch_series(run_dir)
                            stats = extract_ewa(by_topn, by_topn_rank)
                            if stats:
                                 # Use a different metric name for the ensemble point in individual view
                                 # so it can be distinguished if needed, but for now just include it.
                                 rep_metrics.append(stats)
                                 available_metric_names.update(stats.keys())
                    except Exception as e:
                        # Don't show error in sidebar, it's too intrusive. Just log it to st.error if needed or skip.
                        pass

                    if rep_metrics:
                        all_metrics[rid] = rep_metrics
                else:
                    # Ensemble modes: look at the run_dir directly for ensemble files
                    by_topn, _, _, by_topn_rank = build_run_epoch_series(run_dir)
                    combined_stats = extract_ewa(by_topn, by_topn_rank)
                    if combined_stats:
                        all_metrics[rid] = combined_stats
                        available_metric_names.update(combined_stats.keys())

            return all_metrics, sorted(list(available_metric_names))

        st.markdown("### Analyze Results")

        # New split combo boxes
        c1, c2, c3, c4 = st.columns(4)

        ensemble_opt = c1.selectbox("Ensemble Mode", options=["individual", "norm", "sum"],
                                    key="rsd_graph_ensemble_ui",
                                    index=["individual", "norm", "sum"].index(st.session_state["rsd_graph_ensemble"]))
        if ensemble_opt != st.session_state["rsd_graph_ensemble"]:
            st.session_state["rsd_graph_ensemble"] = ensemble_opt
            save_preference("rsd_graph_ensemble", ensemble_opt)
            st.rerun()

        # Determine available TopK options from data? Or just provide common ones.
        topk_options = ["top1", "top3", "top5", "top10", "top20", "top50", "top100"]
        topk_opt = c2.selectbox("Top Stocks (K)", options=topk_options,
                                key="rsd_graph_topk_ui",
                                index=topk_options.index(st.session_state["rsd_graph_topk"]) if st.session_state["rsd_graph_topk"] in topk_options else 2)
        if topk_opt != st.session_state["rsd_graph_topk"]:
            st.session_state["rsd_graph_topk"] = topk_opt
            save_preference("rsd_graph_topk", topk_opt)
            st.rerun()

        metric_type_options = ["portfolio_value", "recommendation_distribution"]
        metric_type_opt = c3.selectbox("Metric Type", options=metric_type_options,
                                      key="rsd_graph_metric_ui",
                                      index=metric_type_options.index(st.session_state["rsd_graph_metric"]) if st.session_state["rsd_graph_metric"] in metric_type_options else 0)
        if metric_type_opt != st.session_state["rsd_graph_metric"]:
            st.session_state["rsd_graph_metric"] = metric_type_opt
            save_preference("rsd_graph_metric", metric_type_opt)
            st.rerun()

        agg_options = ["ewa"]
        agg_opt = c4.selectbox("Aggregation", options=agg_options,
                               key="rsd_graph_agg_ui",
                               index=agg_options.index(st.session_state["rsd_graph_agg"]) if st.session_state["rsd_graph_agg"] in agg_options else 0)
        if agg_opt != st.session_state["rsd_graph_agg"]:
            st.session_state["rsd_graph_agg"] = agg_opt
            save_preference("rsd_graph_agg", agg_opt)
            st.rerun()

        with st.spinner("Extracting metrics for graphing..."):
            run_metrics, metric_options = get_metrics_for_runs(
                runs_to_analyze["run"].tolist(),
                base_dir,
                st.session_state["rsd_graph_ensemble"],
                st.session_state["rsd_graph_topk"],
                st.session_state["rsd_graph_metric"],
                st.session_state["rsd_graph_agg"]
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

                    # Sort by X variable for better visualization
                    # Handle unhashable types (like lists) in x_var by converting to string if needed
                    group_for_plot = group.copy()
                    x_col = "x_var"

                    try:
                        group_for_plot = group_for_plot.sort_values(x_col)
                    except:
                        pass

                    if plot_type == "Box":
                        # Use box plot
                        fig = px.box(group_for_plot, x=x_col, y="metric",
                                     hover_data=["run_id"],
                                     log_x=st.session_state["rsd_log_x_axis"],
                                     points=False,
                                     labels={x_col: selected_x_var, "metric": selected_metric})
                    else:
                        # Use scatter plot
                        fig = px.scatter(group_for_plot, x=x_col, y="metric",
                                         hover_data=["run_id"],
                                         log_x=st.session_state["rsd_log_x_axis"],
                                         labels={x_col: selected_x_var, "metric": selected_metric})

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
