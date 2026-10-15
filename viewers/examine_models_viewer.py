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
)
from training_instance_provider import create_provider

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
    return weights, biases

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
    if "emv_families" in st.session_state and "emv_families_init" not in st.session_state:
        st.session_state["emv_families_init"] = True

    selected_runs = []
    def update_selected_runs(new_selected):
        nonlocal selected_runs
        selected_runs = new_selected

    sidebar_family_and_build_selector(
        timestamp_names=timestamp_names,
        family_names=family_names,
        family_to_ts=family_to_ts,
        key_prefix="emv_",
        default_select_all_builds=False,
        on_runs_updated=update_selected_runs
    )

    if not selected_runs:
        st.warning("Please select at least one run from the sidebar.")
        return

    examine_button = st.button("Examine Models")

    if examine_button or "emv_examined_data" in st.session_state:
        if examine_button:
            # Collect data for all selected runs and their reps
            examined_data = []
            for ts in selected_runs:
                reps = ts_to_runs.get(ts, [])
                for rel_path, params in reps:
                    run_dir = os.path.join(base_dir, rel_path)
                    manifest_path = os.path.join(run_dir, "model_manifest.json")
                    if os.path.exists(manifest_path):
                        model, metadata = _load_model_safe(manifest_path)
                        if model:
                            weights, biases = _get_model_internals(model)
                            examined_data.append({
                                "name": rel_path,
                                "model": model,
                                "metadata": metadata,
                                "weights": weights,
                                "biases": biases,
                                "run_dir": run_dir
                            })
            st.session_state["emv_examined_data"] = examined_data

        examined_data = st.session_state.get("emv_examined_data", [])

        if not examined_data:
            st.error("No models found in the selected runs.")
            return

        # Section 1: Weight and Bias Histograms
        st.header("1. Model Internals: Weights & Biases")
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
        st.header("2. Input Statistics (Mean & Std Dev)")
        st.header("3. Output Statistics (Mean & Std Dev)")

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

            # Section 2 Plot
            st.subheader("Input Distribution")
            input_df = pd.DataFrame({
                "Feature Index": np.arange(len(input_means)),
                "Mean": input_means,
                "Std Dev": input_stds
            })
            fig_in = px.scatter(input_df, x="Mean", y="Std Dev", hover_data=["Feature Index"], title="Input Features Mean vs Std Dev")
            st.plotly_chart(fig_in, use_container_width=True)

            # Section 3 Plot
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
