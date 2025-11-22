from __future__ import annotations

import os
import json
import time
from typing import List, Tuple

import numpy as np

from .compat import torch


def write_label_limits(run_dir: str, provider, train_ds) -> None:
    """Write per-date min/max label values for the training split."""
    try:
        label_limits_path = os.path.join(run_dir, "label_limits.txt")
        if os.path.exists(label_limits_path):
            return
        if hasattr(train_ds, "start_idx"):
            train_indices = list(range(int(train_ds.start_idx), int(train_ds.end_idx) + 1))
        else:
            train_indices = list(getattr(train_ds, "indices", []))

        rows: List[Tuple[int, float, float]] = []
        for i in train_indices:
            d = int(provider._num_prior + (int(i) - 1))
            if d < 0 or d >= int(provider._D):
                continue
            date_id = int(provider._dates[d])
            vec = provider._Y[d]
            if hasattr(np, "isfinite"):
                mask = np.isfinite(vec)
                if mask.any():
                    vmin = float(np.min(vec[mask]))
                    vmax = float(np.max(vec[mask]))
                else:
                    vmin = 0.0
                    vmax = 0.0
            else:
                vmin = float(np.min(vec))
                vmax = float(np.max(vec))
            rows.append((date_id, vmin, vmax))

        seen = set()
        unique_rows: List[Tuple[int, float, float]] = []
        for dt, lo, hi in rows:
            if dt not in seen:
                seen.add(dt)
                unique_rows.append((dt, lo, hi))
        unique_rows.sort(key=lambda t: t[0])

        with open(label_limits_path, mode="w", encoding="utf-8", newline="") as lf:
            lf.write("date_id,min_label,max_label\n")
            for dt, lo, hi in unique_rows:
                lf.write(f"{dt},{lo:.10f},{hi:.10f}\n")
    except Exception as e:
        print(f"Warning: failed to write label_limits.txt: {e}")


def write_run_params(run_dir: str, ts: str, args, resolved: dict, simulate_topk_resolved):
    """Write a text file describing the current run configuration."""
    try:
        params_path = os.path.join(run_dir, "runParameters.txt")
        with open(params_path, mode="w", encoding="utf-8") as pf:
            pf.write("Run Timestamp: " + ts + "\n")
            pf.write("Parameters:\n")
            for k, v in sorted(vars(args).items()):
                pf.write(f"  {k}: {v}\n")
            for rk, rv in resolved.items():
                pf.write(f"  {rk}: {rv}\n")
            pf.write(f"  resolved_simulate_top_k: {simulate_topk_resolved}\n")
    except Exception as e:
        print(f"Warning: failed to write runParameters.txt: {e}")


def save_model_and_manifest(args, model, optimizer, run_dir: str, suffix: str, metrics_file: str, test_metrics_path: str, input_size: int, output_size: int, mlp_layers: int, activation: str, prior_days: int):
    """Save model/optimizer state dicts and a human-readable manifest JSON."""
    model_state_path = os.path.join(run_dir, f"{suffix}_model_state.pt")
    optimizer_state_path = os.path.join(run_dir, f"{suffix}_optimizer_state.pt")
    manifest_path = os.path.join(run_dir, f"{suffix}_model_manifest.json")
    torch.save(model.state_dict(), model_state_path)
    try:
        torch.save(optimizer.state_dict(), optimizer_state_path)
    except Exception as opt_e:
        print(f"Warning: could not save optimizer state: {opt_e}")
    # NOTE: Several boolean feature flags are saved under multiple keys for
    # backward/forward compatibility with older manifests and loaders.
    #
    # "current_day_change" is the canonical key used by
    # StockRecommenderFromModel.get_current_day_change_used(). We also keep
    # "current_day_change_used" for older code that might read that field.
    #
    # "transaction_ratio" is the correctly-spelled key; older code wrote and
    # read "tranaction_ratio" (misspelled). We populate both so existing
    # manifests remain compatible while new code can rely on the proper name.
    manifest = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "arch": str(args.arch),
        "input_size": int(input_size),
        "output_size": int(output_size),
        "hidden_layers": int(args.hidden_layers),
        "mlp_layers": int(mlp_layers),
        "activation": (str(activation) if args.arch == "mlp" else None),
        "learning_rate": float(getattr(args, 'lr', 0.0) if isinstance(getattr(args, 'lr', None), (int, float)) else 0.0),
        "prior_days": int(prior_days),
        # Canonical feature flags (used by loaders)
        "current_day_change": bool(getattr(args, "change_in_price_today", False)),
        "volume_ratio": bool(getattr(args, "volume_ratio", False)),
        "vwap_ratio": bool(getattr(args, "vwap_ratio", False)),
        "transaction_ratio": bool(getattr(args, "tranaction_ratio", False)),
        # Legacy/alias keys for older code
        "current_day_change_used": bool(getattr(args, "change_in_price_today", False)),
        "day_change_centered": args.day_change_centered,
        "precision": str((args.precision or "auto").lower()),
        "state_dict_path": os.path.basename(model_state_path),
        "optimizer_state_path": os.path.basename(optimizer_state_path),
        "metrics_csv": os.path.basename(metrics_file),
        "test_metrics_csv": os.path.basename(test_metrics_path),
    }
    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2)
    print("Saved trained model and manifest:")
    print(f"  Weights: {model_state_path}")
    print(f"  Optimizer: {optimizer_state_path}")
    print(f"  Manifest: {manifest_path}")
    return manifest_path


__all__ = [
    "write_label_limits",
    "write_run_params",
    "save_model_and_manifest",
]
