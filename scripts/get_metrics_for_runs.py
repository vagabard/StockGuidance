import os
import sys
import json
import random
import re
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

import pandas as pd
import numpy as np
import torch

# Add project root to sys.path to allow imports from other packages
root_path = Path(__file__).resolve().parent.parent
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from simulate_trading import (
    _get_cached_entries,
    get_connection
)
from StockRecommenderFromModel import StockRecommenderFromSavedScores
from trainer.state.single_run import SingleRun
from viewers.populate_statistics_viewer import build_run_epoch_series

def load_score_file_cached(score_path: str) -> torch.Tensor:
    """Load scores from a CSV file and return as a float32 tensor."""
    scores_np = np.loadtxt(score_path, delimiter=",")
    return torch.from_numpy(scores_np).to(torch.float32)

def get_daily_data_for_sim(dbinfo_file: str, date_ids: List[int]):
    """Fetch price and transaction data for simulation."""
    if not date_ids:
        return {}, {}

    min_date = min(date_ids)
    max_date = max(date_ids)

    all_ratios = {}
    transactions = {}

    try:
        conn = get_connection(dbinfo_file=dbinfo_file)
        cur = conn.cursor()

        sql = """
               SELECT date_id, ticker_id, open, close, transactions
               FROM raw_stock_data
               WHERE date_id >= %s AND date_id <= %s + 20
               """
        cur.execute(sql, (min_date, max_date))
        rows = cur.fetchall()

        for r_did, r_tid, r_open, r_close, r_trans in rows:
            d_id = int(r_did)
            t_id = int(r_tid)

            if d_id not in transactions:
                transactions[d_id] = {}
            if r_trans is not None:
                transactions[d_id][t_id] = float(r_trans)

            if r_open is not None and r_open > 0 and r_close is not None:
                if d_id not in all_ratios:
                    all_ratios[d_id] = {}
                all_ratios[d_id][t_id] = float(r_close) / float(r_open)

    except Exception as e:
        print(f"Failed to fetch simulation data: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

    return all_ratios, transactions

def get_metrics_for_runs(run_ids: list[str], base_dir: str, ensemble_mode: str, top_k_mode: str, metric_type: str, agg_mode: str, grouping_size: int):
    # Injected st.cache_data if streamlit is available, otherwise just a pass-through
    try:
        import streamlit as st
        decorator = st.cache_data
    except ImportError:
        def decorator(f): return f

    @decorator
    def _get_metrics_impl(run_ids_tuple: tuple, base_dir: str, ensemble_mode: str, top_k_mode: str, metric_type: str, agg_mode: str, grouping_size: int):
        run_ids = list(run_ids_tuple)
        all_metrics = {} # run_id -> dict of metrics OR list of dicts for individual reps
        available_metric_names = set()
        for rid in run_ids:
            run_dir = os.path.normpath(os.path.join(base_dir, rid))

            # Handle grouping_size=1 as individual mode
            current_ensemble_mode = ensemble_mode
            if grouping_size == 1:
                current_ensemble_mode = "individual"

            # Map top_k_mode string to key in by_topn
            # by_topn keys are 0 (ensemble) or (K, Method)
            k_val = int(top_k_mode.replace("top", ""))

            # Determine which internal method to use for the new random ensemble modes
            # e.g. "sum random" -> use_method="Sum", sampling="random"
            use_method = ""
            sampling = None
            is_sum_labeled = False
            if " " in current_ensemble_mode:
                parts = current_ensemble_mode.split(" ")
                use_method = parts[0].capitalize()
                sampling = " ".join(parts[1:])
                if parts[0].lower() == "sum":
                    is_sum_labeled = True
            elif current_ensemble_mode in ["sum", "norm"]:
                use_method = current_ensemble_mode.capitalize()
                if current_ensemble_mode == "sum":
                    is_sum_labeled = True

            target_keys = []
            # rep_target_keys are the keys we look for in INDIVIDUAL reps
            rep_target_keys = []
            if current_ensemble_mode == "individual":
                target_keys = [(k_val, ""), (k_val, None), (k_val, "Rank"), (k_val, "Sum"), (k_val, "Norm")]
                if k_val == 1:
                    target_keys.append(0)
            else:
                target_keys = [(k_val, use_method)]
                if k_val == 1 and not use_method:
                    target_keys.append(0)

                if sampling:
                    # For random ensemble modes, the reps themselves are individuals
                    # so we look for individual keys in them.
                    rep_target_keys = [(k_val, ""), (k_val, None), (k_val, "Rank"), (k_val, "Sum"), (k_val, "Norm")]
                    if k_val == 1:
                        rep_target_keys.append(0)
                else:
                    rep_target_keys = target_keys

            def extract_ewa_from_series(by_topn, by_topn_rank):
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
                        # Use consistent naming for metrics
                        m_suffix = f"_{current_ensemble_mode.replace(' ', '_')}" if current_ensemble_mode != "individual" else ""
                        if current_ensemble_mode == "individual":
                            if isinstance(key, tuple) and len(key) == 2 and key[1]:
                                m_suffix = f"_{key[1].lower()}"
                            elif key == 0:
                                 m_suffix = "_ensemble"

                        metric_name = f"{metric_type}_{top_k_mode}{m_suffix}_{agg_mode}"
                        stats[metric_name] = val_to_store
                return stats

            # Helper to process a list of reps and create ensembles
            def process_reps(rep_paths, sampling_mode, g_size):
                # Load series for each rep
                rep_data = []
                for rp in rep_paths:
                    s1, _, _, s4 = build_run_epoch_series(rp)
                    rep_data.append((s1, s4))

                if not rep_data:
                    return []

                num_reps = len(rep_data)
                ensembles = []

                if sampling_mode == "random":
                    for _ in range(num_reps):
                        if g_size == 0:
                            chosen_indices = list(range(num_reps))
                        else:
                            chosen_indices = random.sample(range(num_reps), min(g_size, num_reps))
                        ensembles.append(chosen_indices)

                elif sampling_mode == "partial random":
                    all_indices = list(range(num_reps))
                    current_pool = all_indices[:]
                    random.shuffle(current_pool)
                    target_g_size = g_size if g_size > 0 else num_reps

                    for _ in range(num_reps):
                        if len(current_pool) >= target_g_size:
                            chunk = current_pool[:target_g_size]
                            current_pool = current_pool[target_g_size:]
                        else:
                            needed = target_g_size - len(current_pool)
                            chunk = current_pool[:]
                            if needed > 0:
                                others = [i for i in all_indices if i not in chunk]
                                chunk += random.sample(others, min(needed, len(others)))
                            current_pool = all_indices[:]
                            random.shuffle(current_pool)
                        ensembles.append(chunk)
                else:
                    return []

                ensemble_metrics = []
                if is_sum_labeled:
                    first_rep_path = rep_paths[0]
                    rep_items = _get_cached_entries(first_rep_path)
                    epochs_and_dates = []
                    for fname, is_dir in rep_items:
                        if not is_dir and fname.startswith("stock_scores_epoch_") and fname.endswith(".csv"):
                            m = re.search(r"epoch_(.*?)_date_(\d+)", fname)
                            if m:
                                epochs_and_dates.append((m.group(1), int(m.group(2))))
                    if not epochs_and_dates:
                        return []

                    unique_epochs = sorted(list(set(e for e, d in epochs_and_dates)))
                    unique_dates = sorted(list(set(d for e, d in epochs_and_dates)))
                    dbinfo_file = "dbinfo.id"
                    all_ratios, _ = get_daily_data_for_sim(dbinfo_file, unique_dates)

                    sr_path = os.path.join(os.path.dirname(rep_paths[0]), "single_run_epoch.json")
                    if not os.path.exists(sr_path):
                        from simulate_trading import _find_single_run_path
                        sr_path = _find_single_run_path(os.path.dirname(rep_paths[0]))

                    if sr_path:
                        single_run = SingleRun.load(sr_path)
                        recommender = StockRecommenderFromSavedScores(single_run)
                        for indices in ensembles:
                            agg_by_topn = { (k_val, use_method): {} }
                            agg_by_topn_rank = { (k_val, use_method): {} }
                            for epoch_str in unique_epochs:
                                portfolio_value = 100.0
                                all_ranks = []
                                epoch_dates = sorted([d for e, d in epochs_and_dates if e == epoch_str])
                                for date_id in epoch_dates:
                                    combined_scores = None
                                    for idx in indices:
                                        score_file = os.path.join(rep_paths[idx], f"stock_scores_epoch_{epoch_str}_date_{date_id}_net_0.csv")
                                        if os.path.exists(score_file):
                                            scores = load_score_file_cached(score_file)
                                            if combined_scores is None:
                                                combined_scores = scores
                                            else:
                                                combined_scores += scores
                                    if combined_scores is None:
                                        continue
                                    top_tids, _ = recommender.get_recommendation_for_scores(combined_scores, k_val)
                                    ratios = all_ratios.get(date_id, {})
                                    if not ratios:
                                        continue
                                    valid_returns = [ratios[tid] for tid in top_tids if tid in ratios]
                                    if not valid_returns:
                                        continue
                                    mean_return = sum(valid_returns) / len(valid_returns)
                                    portfolio_value *= mean_return
                                    day_all_returns = sorted(list(ratios.values()))
                                    if day_all_returns:
                                        import bisect
                                        pos = bisect.bisect_left(day_all_returns, mean_return)
                                        rank_score = pos / len(day_all_returns)
                                        all_ranks.append(rank_score)
                                if all_ranks:
                                    agg_by_topn[(k_val, use_method)][int(epoch_str)] = portfolio_value
                                    agg_by_topn_rank[(k_val, use_method)][int(epoch_str)] = sum(all_ranks) / len(all_ranks)
                            stats = extract_ewa_from_series(agg_by_topn, agg_by_topn_rank)
                            if stats:
                                ensemble_metrics.append(stats)
                    return ensemble_metrics

                for indices in ensembles:
                    agg_by_topn = {}
                    agg_by_topn_rank = {}
                    for key in target_keys:
                        source_idx = 0 if metric_type == "portfolio_value" else 1
                        target_map = agg_by_topn if metric_type == "portfolio_value" else agg_by_topn_rank
                        all_epochs = set()
                        found_key_in_reps = None
                        for r_key in rep_target_keys:
                            for idx in indices:
                                if r_key in rep_data[idx][source_idx]:
                                    all_epochs.update(rep_data[idx][source_idx][r_key].keys())
                            if all_epochs:
                                found_key_in_reps = r_key
                                break
                        if not all_epochs:
                            continue
                        combined_epochs_map = {}
                        for epoch in all_epochs:
                            vals = []
                            for idx in indices:
                                if found_key_in_reps in rep_data[idx][source_idx] and epoch in rep_data[idx][source_idx][found_key_in_reps]:
                                    vals.append(rep_data[idx][source_idx][found_key_in_reps][epoch])
                            if vals:
                                combined_epochs_map[epoch] = sum(vals) / len(vals)
                        target_map[key] = combined_epochs_map
                    stats = extract_ewa_from_series(agg_by_topn, agg_by_topn_rank)
                    if stats:
                        ensemble_metrics.append(stats)
                return ensemble_metrics

            reps_found = []
            try:
                items = sorted(os.listdir(run_dir))
                for item in items:
                    item_path = os.path.join(run_dir, item)
                    if os.path.isdir(item_path) and (item.startswith("rep") or item.startswith("run_single")):
                        reps_found.append(item_path)
            except:
                pass

            if current_ensemble_mode == "individual":
                rep_metrics = []
                if reps_found:
                    for rp in reps_found:
                        rep_by_topn, _, _, rep_by_topn_rank = build_run_epoch_series(rp)
                        stats = extract_ewa_from_series(rep_by_topn, rep_by_topn_rank)
                        if stats:
                            rep_metrics.append(stats)
                            available_metric_names.update(stats.keys())
                else:
                    by_topn, _, _, by_topn_rank = build_run_epoch_series(run_dir)
                    stats = extract_ewa_from_series(by_topn, by_topn_rank)
                    if stats:
                        rep_metrics.append(stats)
                        available_metric_names.update(stats.keys())
                if rep_metrics:
                    all_metrics[rid] = rep_metrics
            elif sampling:
                if not reps_found:
                    reps_found = [run_dir]
                ensemble_results = process_reps(reps_found, sampling, grouping_size)
                if ensemble_results:
                    all_metrics[rid] = ensemble_results
                    for res in ensemble_results:
                        available_metric_names.update(res.keys())
            else:
                by_topn, _, _, by_topn_rank = build_run_epoch_series(run_dir)
                combined_stats = extract_ewa_from_series(by_topn, by_topn_rank)
                if combined_stats:
                    all_metrics[rid] = combined_stats
                    available_metric_names.update(combined_stats.keys())
        return all_metrics, sorted(list(available_metric_names))

    return _get_metrics_impl(tuple(run_ids), base_dir, ensemble_mode, top_k_mode, metric_type, agg_mode, grouping_size)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract metrics for runs independently.")
    parser.add_argument("--run-ids", type=str, required=True, help="Comma-separated run IDs")
    parser.add_argument("--base-dir", type=str, required=True, help="Base data directory")
    parser.add_argument("--ensemble-mode", type=str, default="norm", help="Ensemble mode (e.g., norm, sum, individual, sum random)")
    parser.add_argument("--top-k-mode", type=str, default="top5", help="Top K mode (e.g., top1, top5)")
    parser.add_argument("--metric-type", type=str, default="portfolio_value", help="Metric type")
    parser.add_argument("--agg-mode", type=str, default="ewa", help="Aggregation mode")
    parser.add_argument("--grouping-size", type=int, default=0, help="Grouping size for random ensembles")
    parser.add_argument("--output", type=str, help="Path to save results as JSON")

    args = parser.parse_args()
    run_ids = [rid.strip() for rid in args.run_ids.split(",")]

    print(f"Running get_metrics_for_runs for {len(run_ids)} runs...")
    metrics, available_names = get_metrics_for_runs(
        run_ids,
        args.base_dir,
        args.ensemble_mode,
        args.top_k_mode,
        args.metric_type,
        args.agg_mode,
        args.grouping_size
    )

    result = {
        "metrics": metrics,
        "available_metric_names": available_names
    }

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=4)
        print(f"Results saved to {args.output}")
    else:
        print(json.dumps(result, indent=2))
