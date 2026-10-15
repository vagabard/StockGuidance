import os
import sys
import json
import torch
import numpy as np
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path
from typing import List, Dict, Any, Tuple

# Add project root to sys.path
root_path = Path(__file__).resolve().parent.parent
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from models import load_model_from_manifest
from viewers.compare_runs_shared import (
    scan_runs,
    group_runs_by_timestamp,
    build_run_families,
    sidebar_family_and_build_selector,
    load_preferences,
    render_run_grid,
    runs_to_dataframe_dicts,
    discover_best_run_dir,
    compute_common_params,
)
from training_instance_provider import create_provider

def _find_manifest(run_dir: str) -> str | None:
    """Look for any manifest file in the run directory."""
    # Preferred order: model_manifest.json, final_model_manifest.json, _model_manifest.json
    candidates = ["model_manifest.json", "final_model_manifest.json", "_model_manifest.json"]
    for cand in candidates:
        p = os.path.join(run_dir, cand)
        if os.path.exists(p):
            return p

    # Generic search
    try:
        for f in os.listdir(run_dir):
            if f.endswith("model_manifest.json"):
                return os.path.join(run_dir, f)
    except Exception:
        pass
    return None

def _load_model_safe(manifest_path: str):
    try:
        model, metadata, _ = load_model_from_manifest(manifest_path)
        return model, metadata
    except Exception as e:
        st.error(f"Error loading model from {manifest_path}: {e}")
        return None, None

def _get_model_internals(model):
    weights = []
    biases = []
    for name, param in model.named_parameters():
        if 'weight' in name:
            weights.append((name, param.detach().cpu().numpy().flatten()))
        elif 'bias' in name:
            biases.append((name, param.detach().cpu().numpy().flatten()))

    # Extract layer descriptions from model.net if it exists
    layer_info = []
    if hasattr(model, 'net') and isinstance(model.net, torch.nn.Sequential):
        for i, layer in enumerate(model.net):
            info = {
                "index": i,
                "type": type(layer).__name__,
                "details": str(layer)
            }
            # For linear-like layers, extract more specific connectivity info
            if hasattr(layer, 'in_features') and hasattr(layer, 'out_features'):
                info["connection"] = f"{layer.in_features} → {layer.out_features}"

            layer_info.append(info)

    return weights, biases, layer_info

def _run_as_script():
    st.set_page_config(page_title="Examine Models", layout="wide")
    st.title("Examine Models")

    # Sidebar: base directory
    default_base = os.path.join(os.getcwd(), "data")
    win_default = r"C:\Projects\StockGuidance\data"
    base_dir = st.sidebar.text_input("Base data directory", value=(win_default if os.path.isdir(win_default) else default_base))

    runs = scan_runs(base_dir)
    if not runs:
        st.info("No runs found in the specified directory.")
        return

    ts_to_runs = group_runs_by_timestamp(runs)
    timestamp_names = sorted(ts_to_runs.keys(), reverse=True)
    family_to_ts, family_names = build_run_families(runs, require_multiple_param_sets=False)

    # ------------------------------------------------------------------
    # Navigation support from dashboard
    # ------------------------------------------------------------------
    fam_key = "emv_families"
    if fam_key in st.session_state:
        if "emv_families_init" not in st.session_state:
            st.session_state["emv_families_init"] = True

        # If the family selection changed, we want to re-trigger the auto-population of builds.
        # sidebar_family_and_build_selector checks families_init to decide whether to populate builds.
        last_fam_key = "emv_last_families_selection"
        if last_fam_key not in st.session_state:
            st.session_state[last_fam_key] = st.session_state[fam_key]
        elif st.session_state[last_fam_key] != st.session_state[fam_key]:
            st.session_state["emv_families_init"] = True
            st.session_state[last_fam_key] = st.session_state[fam_key]

    selected_runs = []
    def update_selected_runs(new_selected):
        nonlocal selected_runs
        selected_runs = new_selected

    # We capture the return values. If on_runs_updated is called, it updates nonlocal selected_runs.
    # But the return value 'res_runs' is the most definitive source for this render pass.
    res_runs, selected_families, _ = sidebar_family_and_build_selector(
        timestamp_names=timestamp_names,
        family_names=family_names,
        family_to_ts=family_to_ts,
        key_prefix="emv_",
        default_select_all_builds=False,
        on_runs_updated=update_selected_runs
    )

    # Prioritize returned value over callback-updated value for immediate use
    if res_runs:
        selected_runs = res_runs

    if not selected_families:
        st.info("Select one or more run families in the sidebar to begin.")
        return

    # Flatten the selected timestamps into a single list of logical runs for the grid
    display_rows = []
    # If no specific builds (timestamps) are selected in the sidebar, but families are,
    # show all builds for those families.
    effective_selected_runs = selected_runs
    if not effective_selected_runs and selected_families:
        effective_selected_runs = []
        for fam in selected_families:
            effective_selected_runs.extend(family_to_ts.get(fam, []))
        # De-duplicate
        effective_selected_runs = sorted(list(set(effective_selected_runs)), reverse=True)

    if not effective_selected_runs and selected_families:
        st.info("No runs found for the selected families.")
        return

    if not effective_selected_runs:
        # This case handles when no family and no specific builds are chosen.
        st.info("Tip: Select specific builds in the sidebar or use the sidebar pickers.")
        return

    for ts in effective_selected_runs:
        run_list = ts_to_runs.get(ts, [])
        if not run_list:
            continue

        # Show only the logical single run (ensemble) for this timestamp by default.
        # We prefer a directory that contains simulation files at the top level (ensemble).
        best_dir = discover_best_run_dir(base_dir, ts, run_list)

        # Map back to params
        if best_dir:
            rel_best = os.path.relpath(best_dir, base_dir)
            # Try to find exactly matching rep or just use first if it's the ensemble dir
            match = next((p for d, p in run_list if d == rel_best), None)
            if not match:
                match = compute_common_params([p for _, p in run_list])

            row = dict(match)
            row["_ts"] = ts
            row["_dir"] = rel_best
            display_rows.append(row)
        else:
            # Fallback: Collapse reps to common parameters
            run_params = [p for _, p in run_list]
            common_p = compute_common_params(run_params)
            row = dict(common_p)
            row["_ts"] = ts
            row.setdefault("_dir", run_list[0][0])
            display_rows.append(row)

    if not display_rows:
        st.info("No sub-runs found for the selected timestamps.")
        return

    df_dicts = runs_to_dataframe_dicts(display_rows, exclude_keys=[])
    df = pd.DataFrame(df_dicts)

    st.subheader(f"Available Single Runs ({len(df)})")
    prefs = load_preferences()
    selected_in_grid = render_run_grid(
        df=df,
        key_prefix="emv_grid",
        prefs=prefs,
        default_visible_cols=["run", "day_change_centered", "prior_days", "mlp_layers", "lr", "activation", "epochs", "epoch", "rank1", "rank5"]
    )

    if not selected_in_grid:
        st.warning("Please select at least one single run from the grid.")
        return

    # For the selected single runs, allow picking a specific rep
    selected_df = df[df['run'].isin(selected_in_grid)]

    # We'll examine one "run" at a time if multiple are selected, or provide a way to pick rep for each?
    # Usually "Examine Models" might want to look at one specific rep of one specific run.
    # If multiple are selected, we can show a selector for which one to examine.

    st.divider()
    st.subheader("Model Selection & Reps")

    # Let's group by the selected run to pick reps
    examine_configs = []
    for _, row in selected_df.iterrows():
        ts = row.get("_ts")
        run_id = row.get("run")
        run_list = ts_to_runs.get(ts, [])

        reps = [d for d, _ in run_list]
        # Sort reps to be consistent
        reps = sorted(reps)

        if len(reps) > 1:
            selected_rep = st.selectbox(f"Select rep for {run_id}", options=reps, index=0, key=f"rep_sel_{run_id}")
        else:
            selected_rep = reps[0] if reps else row.get("_dir")
            st.text(f"Run {run_id} has only one rep: {selected_rep}")

        # Get params for this specific rep
        rep_params = next((p for d, p in run_list if d == selected_rep), dict(row))
        examine_configs.append((selected_rep, rep_params))

    examine_button = st.button("Examine Models")

    if examine_button or "emv_examined_data" in st.session_state:
        if examine_button:
            # Collect data for selected reps
            examined_data = []

            for rel_path, params in examine_configs:
                run_dir = os.path.join(base_dir, rel_path)
                manifest_path = _find_manifest(run_dir)
                if manifest_path and os.path.exists(manifest_path):
                    model, metadata = _load_model_safe(manifest_path)
                    if model:
                        weights, biases, layers = _get_model_internals(model)
                        examined_data.append({
                            "name": rel_path,
                            "model": model,
                            "metadata": metadata,
                            "weights": weights,
                            "biases": biases,
                            "layers": layers,
                            "run_dir": run_dir
                        })
            st.session_state["emv_examined_data"] = examined_data

        examined_data = st.session_state.get("emv_examined_data", [])

        if not examined_data:
            st.error("No models found in the selected runs.")
            return

        # Section 1: Weight and Bias Histograms
        st.header("1. Model Architecture & Layer Connections")
        for data in examined_data:
            with st.expander(f"Architecture: {data['name']}", expanded=True):
                if data['layers']:
                    # Build a list of descriptions for each layer
                    for layer in data['layers']:
                        col1, col2, col3 = st.columns([1, 2, 4])
                        with col1:
                            st.markdown(f"**net.{layer['index']}**")
                        with col2:
                            st.markdown(f"`{layer['type']}`")
                        with col3:
                            desc = layer['details']
                            if 'connection' in layer:
                                desc = f"**{layer['connection']}**  \n({desc})"
                            st.write(desc)
                else:
                    st.info("No detailed layer information available for this model type.")

        st.header("2. Model Internals: Weights & Biases")
        for data in examined_data:
            with st.expander(f"Model: {data['name']}", expanded=False):
                st.subheader("Weights")
                # Weight histograms
                for name, val in data['weights']:
                    fig = px.histogram(x=val, title=f"Weight: {name}")
                    st.plotly_chart(fig, use_container_width=True)

                st.subheader("Biases")
                # Bias histograms
                for name, val in data['biases']:
                    fig = px.histogram(x=val, title=f"Bias: {name}")
                    st.plotly_chart(fig, use_container_width=True)

        # Section 2 & 3: Input/Output Statistics
        st.header("3. Input Statistics (Mean & Std Dev)")
        st.header("4. Output Statistics (Mean & Std Dev)")

        # We need a representative batch of data to compute I/O stats
        # Picking the first selected model's metadata to create a provider
        first_metadata = examined_data[0]['metadata']

        # We need to know which provider to use. Most use TrainingInstanceProvider
        # Let's try to infer parameters from metadata
        try:
            # We might need to pass common parameters to create_provider
            # Many parameters are in metadata
            provider_kwargs = {
                "dbinfo_file": first_metadata.get("dbinfo_file", "dbinfo.id"),
            }
            # Add other common params if they exist in manifest
            for key in ["volume_ratio", "vwap_ratio", "tranaction_ratio", "day_change_centered"]:
                if key in first_metadata:
                    provider_kwargs[key] = first_metadata[key]

            provider = create_provider(**provider_kwargs)
            if hasattr(provider, 'set_how_many_days_in_past_to_use'):
                provider.set_how_many_days_in_past_to_use(first_metadata.get("prior_days", 0))

            max_inst = provider.get_maximum_number_of_test_train_instances()
            # Use a sample of instances
            sample_size = min(max_inst, 500)
            indices = np.random.choice(max_inst, sample_size, replace=False)

            all_inputs = []
            all_outputs = []

            with st.spinner(f"Collecting stats from {sample_size} samples..."):
                for idx in indices:
                    feat, label = provider.get_feature_and_label_pair(int(idx))
                    if isinstance(feat, torch.Tensor):
                        feat = feat.cpu().numpy()
                    if isinstance(label, torch.Tensor):
                        label = label.cpu().numpy()
                    all_inputs.append(feat.flatten())
                    all_outputs.append(label.flatten())

            all_inputs = np.array(all_inputs) # (sample_size, input_dim)
            all_outputs = np.array(all_outputs) # (sample_size, output_dim)

            input_means = np.mean(all_inputs, axis=0)
            input_stds = np.std(all_inputs, axis=0)
            output_means = np.mean(all_outputs, axis=0)
            output_stds = np.std(all_outputs, axis=0)

            # Section 3 Plot
            st.subheader("Input Distribution")
            input_df = pd.DataFrame({
                "Feature Index": np.arange(len(input_means)),
                "Mean": input_means,
                "Std Dev": input_stds
            })
            fig_in = px.scatter(input_df, x="Mean", y="Std Dev", hover_data=["Feature Index"], title="Input Features Mean vs Std Dev")
            st.plotly_chart(fig_in, use_container_width=True)

            # Section 4 Plot
            st.subheader("Output Distribution")
            output_df = pd.DataFrame({
                "Output Index": np.arange(len(output_means)),
                "Mean": output_means,
                "Std Dev": output_stds
            })
            fig_out = px.scatter(output_df, x="Mean", y="Std Dev", hover_data=["Output Index"], title="Output Labels Mean vs Std Dev")
            st.plotly_chart(fig_out, use_container_width=True)

        except Exception as e:
            st.error(f"Error computing I/O statistics: {e}")
            st.exception(e)

if __name__ == "__main__":
    _run_as_script()
