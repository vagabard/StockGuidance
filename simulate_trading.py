import argparse
import os
import random
import re
import threading
import time
import traceback
from queue import Queue
from typing import Tuple, Dict, Optional, List, Union, Any

import line_profiler
import numpy as np
import torch
from torch import Tensor

from StockRecommenderFromModel import StockRecommenderFromModel, StockRecommenderFromSavedScores, StockRecommender
from database.db_connect import get_connection
from get_ticker_position_on_date import get_ticker_positions
from load_raw_stock_data_rows import fetch_open_close_for_date_tickers
from models.utils import resolve_manifest_path  # type: ignore
from trainer.datasets import IndexDataset
from trainer.state.single_run import SingleRun
from trainer.util.argparse_utils import parse_int_list
from training_instance_provider import TrainingInstanceProvider

# Global cache for pre-fetched stock data to avoid redundant DB queries across epochs/runs
# Key: (dbinfo_file, min_date, max_date)
# Value: (all_ratios_by_date, transactions_by_date, price_data_by_date)
GLOBAL_DATA_CACHE: Dict[Tuple[str, int, int], Tuple[Dict, Dict, Dict]] = {}

# Global cache for score files to avoid repeated glob calls
# Key: score_dir
# Value: List of (name, is_dir) tuples
GLOBAL_SCORE_FILE_CACHE: Dict[str, List[Tuple[str, bool]]] = {}

def _get_cached_entries(score_dir: str, force_refresh: bool = False) -> List[Tuple[str, bool]]:
	"""Get list of (name, is_dir) in a directory, using a cache to avoid repeated OS calls."""
	if score_dir not in GLOBAL_SCORE_FILE_CACHE or force_refresh:
		try:
			entries = []
			with os.scandir(score_dir) as it:
				for entry in it:
					entries.append((entry.name, entry.is_dir()))
			GLOBAL_SCORE_FILE_CACHE[score_dir] = entries
		except Exception as e:
			print(f"Error scanning {score_dir}: {e}")
			GLOBAL_SCORE_FILE_CACHE[score_dir] = []
	return GLOBAL_SCORE_FILE_CACHE[score_dir]

# Queue for background score processing
# Elements: (score_path, raw_scores_list, norm_scores_list, rank_scores_list)
SCORE_PROCESSING_QUEUE: Queue = Queue()


def _score_processing_worker():
	"""Worker thread to process scores from the queue."""
	while True:
		item = SCORE_PROCESSING_QUEUE.get()
		if item is None:
			SCORE_PROCESSING_QUEUE.task_done()
			break

		score_path, raw_scores_list, norm_scores_list, rank_scores_list = item
		try:
			scores = _load_scores_from_file(score_path)
			raw_scores_list.append(scores)

			norm_scores = _normalize_scores(scores)
			norm_scores_list.append(norm_scores)

			ranks = _rank_scores(scores)
			rank_scores_list.append(ranks)
		except Exception as e:
			print(f"Error processing scores from {score_path}: {e}")
			traceback.print_exc()
		finally:
			SCORE_PROCESSING_QUEUE.task_done()


# Start the background thread
worker_thread = threading.Thread(target=_score_processing_worker, daemon=True)
worker_thread.start()


def perform_analysis_prefetch_scores(
	dates_to_handle: IndexDataset,
	dbinfo_file: str = "dbinfo.id",
) -> Tuple[Dict[int, Dict[int, float]], Dict[int, Dict[int, float]], Dict[int, Dict[int, Tuple[float, float]]]]:
	"""Pre-fetch all data for all dates to avoid DB roundtrips in the loop.
	Uses GLOBAL_DATA_CACHE to reuse data across multiple calls with same parameters.
	"""
	all_ratios_by_date: Dict[int, Dict[int, float]] = {}
	transactions_by_date: Dict[int, Dict[int, float]] = {}
	price_data_by_date: Dict[int, Dict[int, Tuple[float, float]]] = {}

	# Fetch all relevant dates from dates_to_handle
	all_feature_dates = []
	for _, _, feature_date_id in dates_to_handle.get_next_feature_label_date():
		all_feature_dates.append(int(feature_date_id))

	if not all_feature_dates:
		return {}, {}, {}

	# We need data for feature_date_id (transactions) and trade_date_id (prices)
	provider = dates_to_handle.provider
	all_dates_provider = provider.cumulative_dates_for_train_validation_and_test_sets.tolist()

	# Range covers both feature dates and trade dates
	min_date = min(all_feature_dates)

	trade_dates = []
	for f_date in all_feature_dates:
		try:
			idx = all_dates_provider.index(f_date)
			if idx + 1 < len(all_dates_provider):
				trade_dates.append(all_dates_provider[idx + 1])
		except ValueError:
			continue

	max_date = max(trade_dates) if trade_dates else max(all_feature_dates)

	# Check cache
	cache_key = (dbinfo_file, min_date, max_date)
	if cache_key in GLOBAL_DATA_CACHE:
		return GLOBAL_DATA_CACHE[cache_key]

	try:
		conn = get_connection(dbinfo_file=dbinfo_file)
		cur = conn.cursor()

		sql_data = """
				   SELECT date_id, ticker_id, open, close, transactions
				   FROM raw_stock_data
				   WHERE date_id BETWEEN %s AND %s \
				   """
		cur.execute(sql_data, (min_date, max_date))
		rows = cur.fetchall()

		for r_did, r_tid, r_open, r_close, r_trans in rows:
			d_id = int(r_did)
			t_id = int(r_tid)

			# Store transactions for all dates in range
			if d_id not in transactions_by_date:
				transactions_by_date[d_id] = {}
			if r_trans is not None:
				transactions_by_date[d_id][t_id] = float(r_trans)

			# Store price/ratio data if open > 0
			if r_open is not None and r_open > 0 and r_close is not None:
				r_o = float(r_open)
				r_c = float(r_close)

				if d_id not in all_ratios_by_date:
					all_ratios_by_date[d_id] = {}
				all_ratios_by_date[d_id][t_id] = r_c / r_o

				if d_id not in price_data_by_date:
					price_data_by_date[d_id] = {}
				price_data_by_date[d_id][t_id] = (r_o, r_c)

		print(f"DEBUG: [perform_analysis_prefetch_scores] Pre-fetched data for {len(transactions_by_date)} dates.")

		# Save to cache
		GLOBAL_DATA_CACHE[cache_key] = (all_ratios_by_date, transactions_by_date, price_data_by_date)

	except Exception as e:
		print(f"Warning: Failed to pre-fetch data: {e}")
	finally:
		if 'conn' in locals() and conn:
			conn.close()

	return all_ratios_by_date, transactions_by_date, price_data_by_date


def calculate_scores(
	stock_recommenders: List[StockRecommender],
	dates_to_handle: IndexDataset,
	epoch_name: str = "",
	dbinfo_file: str = "dbinfo.id",
) -> None:
	"""Step 1: Calculate and save recommendation scores for all dates and networks."""
	total_recommenders = len(stock_recommenders)
	with torch.no_grad():
		for r_idx, stock_recommender in enumerate(stock_recommenders):
			print(f"DEBUG: [calculate_scores] Processing recommender {r_idx+1}/{total_recommenders} ({stock_recommender.get_output_directory()})")
			feature_label_date_gen = dates_to_handle.get_next_feature_label_date()
			num_days = len(dates_to_handle)
			for day_idx, (feat_vec, _, feature_date_id) in enumerate(feature_label_date_gen):
				if day_idx % 100 == 0:
					print(f"DEBUG: [calculate_scores] Day {day_idx}/{num_days} (date_id {feature_date_id}): generating buy recommendations...")
				# We don't need top_k or other analysis here, just trigger score generation and saving
				stock_recommender.getBuyRecommendations(
					feat_vec,
					top_k=1,
					current_date_id=feature_date_id,
					dbinfo_file=dbinfo_file,
					save_scores=True,
					epoch_name=epoch_name,
					clear_gpu_cache=True,
				)


@line_profiler.profile
def perform_analysis(
	stock_recommenders: List[StockRecommender],
	top_k: List[int],
	dates_to_handle: IndexDataset,
	dbinfo_file: str = "dbinfo.id",
	out_filename_suffix: Optional[str] = None,
	only_ensemble: bool = False,
	force: bool = False,
) -> str:
	"""Step 2: Use saved scores to generate recommendations and update portfolio."""
	out_paths = []
	epoch_name = str(out_filename_suffix).replace("_epoch_", "") if out_filename_suffix else ""

	# Pre-fetch all data for all dates to avoid DB roundtrips in the loop
	all_ratios_by_date, transactions_by_date, price_data_by_date = perform_analysis_prefetch_scores(
		dates_to_handle, dbinfo_file
	)

	num_days = len(dates_to_handle)
	stock_simulation_size = f"{num_days}days"
	num_networks = len(stock_recommenders)

	# If multiple recommenders, the first one is usually the parent directory recommender.
	# We want to generate:
	# 1. Ensemble simulation in the parent directory (if num_networks > 1)
	# 2. Individual simulations in each rep directory

	# Determine which recommenders to process and where to save them.
	# Mapping: output_path -> list of recommenders for ensemble (usually just 1 for reps, many for ensemble)
	tasks = []

	if num_networks > 1:
		parent_recommender = stock_recommenders[0]
		target_dir = parent_recommender.get_output_directory()
		if not is_family_directory(target_dir):
			# Ensemble task: all reps (recommenders 1..N) aggregated into parent dir (recommender 0)
			ensemble_reps = stock_recommenders[1:]
			out_name = f"stock_simulation_ensemble_{stock_simulation_size}{out_filename_suffix or ''}.csv"
			out_path = os.path.join(target_dir, out_name)
			tasks.append((out_path, ensemble_reps, parent_recommender)) # target_path, source_reps, main_recommender

			# Individual rep tasks: each rep (recommenders 1..N) in its own dir
			# In an ensemble, reps are added for each net_X.
			# We only want to generate one simulation per rep directory.
			seen_dirs = set()
			for rep_rec in ensemble_reps:
				rep_dir = rep_rec.get_output_directory()
				if rep_dir in seen_dirs:
					continue
				seen_dirs.add(rep_dir)
				out_name = f"stock_simulation_{stock_simulation_size}{out_filename_suffix or ''}.csv"
				out_path = os.path.join(rep_dir, out_name)
				tasks.append((out_path, [rep_rec], rep_rec))
		else:
			# It's a family directory, don't ensemble the sub-runs here.
			# Just process each individually (if requested via not only_ensemble)
			for rec in stock_recommenders:
				out_name = f"stock_simulation_{stock_simulation_size}{out_filename_suffix or ''}.csv"
				out_path = os.path.join(rec.get_output_directory(), out_name)
				tasks.append((out_path, [rec], rec))
	else:
		# Single recommender task
		rec = stock_recommenders[0]
		out_name = f"stock_simulation_{stock_simulation_size}{out_filename_suffix or ''}.csv"
		out_path = os.path.join(rec.get_output_directory(), out_name)
		tasks.append((out_path, [rec], rec))

	# The ensemble file in the parent directory is the primary output we want to return
	final_out_path = tasks[0][0] if tasks else ""
	for out_path, source_reps, main_recommender in tasks:
		if os.path.exists(out_path) and not force:
			continue

		# Portfolio initial values
		portfolio_values_by_k_method: Dict[Tuple[str, int], float] = {} # (method, k) -> value

		# Write header
		with open(out_path, "w", encoding="utf-8") as f:
			cols = ["feature_date_id", "open_date_id", "close_date_id", "topNRecommends"]
			num_sources = len(source_reps)
			method_names = ["Sum", "Norm", "Rank"] if num_sources > 1 else [""]
			for method_name in method_names:
				suffix = f" ({method_name})" if method_name else ""
				for k in top_k:
					cols += [f"Return Percent top{k}{suffix}", f"Rank Percent top{k}{suffix}", f"Portfolio return top{k}{suffix}"]
			f.write(",".join(cols) + "\n")

			provider = dates_to_handle.provider
			all_dates = provider.cumulative_dates_for_train_validation_and_test_sets.tolist()

			feature_label_date_gen = dates_to_handle.get_next_feature_label_date()
			num_days_to_process = len(dates_to_handle)

			for day_idx, (_, _, feature_date_id) in enumerate(feature_label_date_gen):
				try:
					date_idx = all_dates.index(int(feature_date_id))
				except ValueError:
					continue

				trade_idx = date_idx + 1
				if trade_idx >= len(all_dates):
					continue
				trade_date_id = int(all_dates[trade_idx])

				# Load saved scores for all source networks
				raw_scores_list, norm_scores_list, rank_scores_list = _load_epoch_scores(
					source_reps, int(feature_date_id), epoch_name
				)

				if not raw_scores_list:
					continue

				# Methods to simulate
				methods = [
					("Sum", torch.stack(raw_scores_list).sum(dim=0)),
					("Norm", torch.stack(norm_scores_list).sum(dim=0)),
					("Rank", torch.stack(rank_scores_list).mean(dim=0))
				]

				# If it's not an ensemble, just use the first method
				if num_sources == 1:
					methods = [("", methods[0][1])]

				# Results for all methods
				daily_method_results = {} # method_name -> {k -> (ret, rank, port_val)}

				for method_name, agg_scores in methods:
					max_k = max(top_k)
					precalc_trans = transactions_by_date.get(int(feature_date_id))

					# Use main_recommender for logic (ticker pool etc)
					_scores, top_scoring_ticker_ids_all = main_recommender.getBuyRecommendations(
						feature_vector=None,
						top_k=max_k,
						index_base=1,
						current_date_id=feature_date_id,
						dbinfo_file=dbinfo_file,
						clear_gpu_cache=True,
						precalculated_scores=agg_scores,
						precalculated_transactions=precalc_trans,
					)

					# Fetch actual open/close prices to calculate real returns
					needed_tickers_list = [int(tid) for tid in top_scoring_ticker_ids_all[:max_k]]

					day_price_data = price_data_by_date.get(trade_date_id, {})
					if not day_price_data:
						price_data_map = fetch_open_close_for_date_tickers(
							dbinfo_file=dbinfo_file,
							date_id=trade_date_id,
							ticker_ids=needed_tickers_list,
						)
					else:
						price_data_map = {tid: day_price_data[tid] for tid in needed_tickers_list if tid in day_price_data}

					method_results_k = {}
					for k in top_k:
						chosen = top_scoring_ticker_ids_all[:k]
						valid_ratios: List[float] = []
						for tid in chosen:
							prices = price_data_map.get(int(tid))
							if prices is not None:
								open_p, close_p = prices
								if open_p > 0 and not np.isnan(open_p) and not np.isnan(close_p):
									valid_ratios.append(close_p / open_p)

						day_ratio_avg = 1.0
						ret = 0.0
						rank = 0.0
						if valid_ratios:
							day_ratio_avg = float(np.mean(valid_ratios))
							ret = day_ratio_avg - 1.0

							# Calculate percentile rank
							# The requirement is to rank against all stocks from the recommendation file (the pool)
							pool_ticker_ids = main_recommender.ticker_ids
							date_ratios = all_ratios_by_date.get(trade_date_id, {})

							# Filter distribution to ONLY include those in the recommendation pool
							if pool_ticker_ids is not None:
								pool_set = set(pool_ticker_ids)
								ticker_to_ratio = {tid: ratio for tid, ratio in date_ratios.items() if tid in pool_set}
							else:
								ticker_to_ratio = date_ratios

							if not ticker_to_ratio:
								positions_map = {tid: 0.0 for tid in chosen}
							else:
								all_ratios_vals = np.fromiter(ticker_to_ratio.values(),dtype=np.float32)
								all_ratios_sorted = np.sort(all_ratios_vals)
								total_count = len(all_ratios_sorted)
								positions_map = {}

								stable_order = sorted(chosen)
								ordered_ratios = list()
								used_tickers = list()
								for tid in stable_order:
									if tid in ticker_to_ratio:
										ordered_ratios.append(ticker_to_ratio[tid])
										used_tickers.append(tid)
									else:
										positions_map[tid] = 0.0
										print(f"Warning: Ticker {tid} not found in the recommendation pool.")
										# "count the number of stocks ... with a true ... next day change less than that"
								lower_count = np.searchsorted(all_ratios_sorted, ordered_ratios, side='left')
								for idx, tid in enumerate(used_tickers):
									positions_map[tid] = float(lower_count[idx]) / total_count




							if positions_map:
								rank = float(np.mean(list(positions_map.values())))

						# Update portfolio value
						port_key = (method_name, k)
						if port_key not in portfolio_values_by_k_method:
							portfolio_values_by_k_method[port_key] = 1000.0
						portfolio_values_by_k_method[port_key] *= day_ratio_avg

						method_results_k[k] = (ret, rank, portfolio_values_by_k_method[port_key])

					daily_method_results[method_name] = (method_results_k, "|".join(str(int(tid)) for tid in top_scoring_ticker_ids_all[:max_k]))

				# Write row to CSV
				primary_method = methods[0][0]
				row_vals = [
					str(feature_date_id),
					str(trade_date_id),
					str(trade_date_id),
					daily_method_results[primary_method][1]
				]
				for method_name, _ in methods:
					res_k, _ = daily_method_results[method_name]
					for k in top_k:
						ret, rnk, port = res_k[k]
						row_vals += [f"{ret:.6f}", f"{rnk:.6f}", f"{port:.6f}"]
				f.write(",".join(row_vals) + "\n")

		out_paths.append(out_path)
	return final_out_path


def _find_score_file(score_dir: str, feature_date_id: int, epoch_name: str) -> Optional[str]:
	"""Find the score file for a specific date and epoch in the given directory."""
	files = _get_cached_entries(score_dir)

	# pattern was: stock_scores_epoch_{epoch_name}_date_{feature_date_id}_net_*.csv
	prefix = f"stock_scores_epoch_{epoch_name}_date_{feature_date_id}_net_"
	suffix = ".csv"

	found_files = [
		os.path.join(score_dir, f) for f, is_dir in files
		if not is_dir and f.startswith(prefix) and f.endswith(suffix)
	]

	if not found_files:
		print(f"Warning: No score file found for date {feature_date_id} in {score_dir} (prefix: {prefix})")
		return None

	if len(found_files) > 1:
		print(f"Warning: Multiple score files found for date {feature_date_id} in {score_dir}: {found_files}. Using the first one.")

	return found_files[0]


def _load_scores_from_file(score_path: str) -> torch.Tensor:
	"""Load scores from a CSV file and return as a float32 tensor."""
	scores_np = np.loadtxt(score_path, delimiter=",")
	return torch.from_numpy(scores_np).to(torch.float32)


def _normalize_scores(scores: torch.Tensor) -> torch.Tensor:
	"""Normalize scores by their sum if the sum is positive."""
	s_sum = scores.sum()
	if s_sum > 0:
		return scores / s_sum
	return scores


def _rank_scores(scores: torch.Tensor) -> torch.Tensor:
	"""Calculate ranks for scores, handling NaNs and infinities."""
	temp_scores = scores.clone()
	temp_scores = torch.nan_to_num(temp_scores, nan=-1e9, posinf=-1e9, neginf=-1e9)
	sorted_indices = torch.argsort(temp_scores)
	ranks = torch.zeros_like(temp_scores)
	ranks[sorted_indices] = torch.arange(len(temp_scores), dtype=torch.float32)
	return ranks

def _load_epoch_scores(
	stock_recommenders: List[StockRecommender],
	feature_date_id: int,
	epoch_name: str,
) -> Tuple[List[torch.Tensor], List[torch.Tensor], List[torch.Tensor]]:
	"""Load and normalize scores for all networks for a given date and epoch."""
	raw_scores_list: List[torch.Tensor] = []
	norm_scores_list: List[torch.Tensor] = []
	rank_scores_list: List[torch.Tensor] = []

	num_networks = len(stock_recommenders)

	for net_idx in range(num_networks):
		curr_recommender = stock_recommenders[net_idx]
		score_dir = curr_recommender.get_output_directory()

		score_path = _find_score_file(score_dir, feature_date_id, epoch_name)
		if score_path is None:
			continue

		load_and_process_scores_from_files(norm_scores_list, rank_scores_list, raw_scores_list, score_path)

	# Wait for all scores in this epoch to be processed
	SCORE_PROCESSING_QUEUE.join()

	return raw_scores_list, norm_scores_list, rank_scores_list


def load_and_process_scores_from_files(norm_scores_list: list[Tensor], rank_scores_list: list[Tensor],
									   raw_scores_list: list[Tensor], score_path: str):
	"""Add a score processing task to the queue."""
	SCORE_PROCESSING_QUEUE.put((score_path, raw_scores_list, norm_scores_list, rank_scores_list))


def simulate(
	stock_recommenders: List[StockRecommender],
	top_k: List[int],
	dates_to_handle: IndexDataset,
	dbinfo_file: str = "dbinfo.id",
	out_filename_suffix: Optional[str] = None,
	only_ensemble: bool = False,
	force: bool = False,
) -> str:
	"""Run a trading simulation for a single model or an ensemble of models."""
	epoch_name = str(out_filename_suffix).replace("_epoch_", "") if out_filename_suffix else ""

	print(f"Step 1: Calculating scores for epoch {epoch_name}...")
	calculate_scores(stock_recommenders, dates_to_handle, epoch_name=epoch_name, dbinfo_file=dbinfo_file)

	print(f"Step 2: Performing analysis for epoch {epoch_name}...")
	return perform_analysis(stock_recommenders, top_k, dates_to_handle, dbinfo_file, out_filename_suffix, only_ensemble=only_ensemble, force=force)


def parse_args(argv: Optional[List[str]] = None):
	p = argparse.ArgumentParser(description="Simulate daily trading using a trained model.")
	p.add_argument("model", help="Path to model directory or model_manifest.json")
	# p.add_argument("--percent", type=float, default=10.0, help="Percentage of dates to simulate (use last N percent)")
	p.add_argument("--date-range", type=str, default=None, help="date range in the form database date_id-database date_id")
	p.add_argument(
		"--top-k",
		type=str,
		default="1,3,5,10",
		help=(
			"How many top tickers to buy each day. Accepts a single int (e.g., 10) "
			"or a comma-separated list (e.g., 3,5,10). When multiple are provided, the output CSV includes one column per K."
		),
	)

	# Data loading / DB options (standardized)
	p.add_argument("--dbinfo-file", default="dbinfo.id", help="Path to DB info file (default: dbinfo.id)")
	p.add_argument("--limit", type=int, default=None, help="Optional LIMIT for debugging")

	p.add_argument(
		"--prior-only", action="store_true", help="Build features from strictly prior days [d-1..d-N] if possible"
	)
	p.add_argument(
		"--device",
		default="auto",
		type=str,
		help=(
			"Device to run inference on: 'auto' (default, uses 'cuda' if available else 'cpu'), "
			"'cpu', or a specific CUDA device like 'cuda:0'."
		),
	)
	p.add_argument(
		"--debug-sim-output",
		action="store_true",
		help="Include debug columns: top3 ticker ids, close_ratio for top1, and raw close for top1",
	)
	p.add_argument(
		"--ensemble-normalize",
		action="store_true",
		help=(
			"If set, enable score normalization at the StockRecommenderFromModel level "
			"(mirrors run_recommender_from_model_dir's --ensemble-normalize flag)."
		),
	)
	p.add_argument(
		"--from-saved-scores",
		action="store_true",
		help="Generate simulation results using existing stock_scores_*.csv files instead of running models",
	)
	p.add_argument(
		"--only-ensemble",
		action="store_true",
		help="Only process ensemble simulations, skipping individual reps.",
	)
	p.add_argument(
		"--force",
		action="store_true",
		help="Force recalculation even if simulation results already exist.",
	)
	return p.parse_args(argv)

def simulate_using_models_passed(models_passed: List[StockRecommender], args: argparse.Namespace, top_k_parsed=None,
								 dates_to_handle: IndexDataset = None) -> None:
	"""Run simulations for the passed models."""

	start_time = time.perf_counter()
	if not models_passed:
		print("No models passed for simulation.")
		return

	print(f"Starting simulations for {models_passed[0].get_output_directory()}...")
	try:
		out_path = simulate(
			models_passed,
			top_k=top_k_parsed,
			dates_to_handle=dates_to_handle,
			dbinfo_file=args.dbinfo_file,
			only_ensemble=args.only_ensemble,
			force=args.force,
		)
		print(f"Simulation complete. Results written to: {out_path}")

	finally:
		elapsed = time.perf_counter() - start_time
		print(f"Completed simulations in {elapsed:.2f} seconds")

def is_rep_directory(path: str, force_refresh: bool = False) -> bool:
	"""Check if the given path is a rep directory (contains net_X score files)."""
	if not os.path.isdir(path):
		return False
	entries = _get_cached_entries(path, force_refresh=force_refresh)
	for fname, is_dir in entries:
		if not is_dir and fname.startswith("stock_scores_epoch_") and fname.endswith(".csv") and "_net_" in fname:
			return True
	return False


def get_single_run_directory(path: str) -> str:
	"""Return the single run (ensemble) directory for a given path.
	If the path is a rep directory, returns its parent. Otherwise returns the path itself.
	"""
	if is_rep_directory(path):
		return os.path.dirname(path)
	return path


def is_family_directory(path: str, force_refresh: bool = False) -> bool:
	"""Check if the given path is a family directory."""
	entries = _get_cached_entries(path, force_refresh=force_refresh)
	for fname, is_dir in entries:
		if not is_dir and fname == "family_state.json":
			return True
	return False


def is_ensemble_directory_given(path: str, force_refresh: bool = False) -> bool:
	"""Check if the given path is an ensemble directory based on naming convention."""
	entries = _get_cached_entries(path, force_refresh=force_refresh)

	is_family = False
	rep_count = 0
	for name, is_dir in entries:
		if not is_dir and name == "family_state.json":
			is_family = True
		if is_dir and name.lower().startswith("rep"):
			rep_count += 1

	if is_family:
		return False

	return rep_count > 1


def create_list_of_stock_recommender_models_from_single_run_directory_with_repeats(ensemble_normalize:bool, repeat_run_home_dir: str) -> list[
	StockRecommender]:
	entries = sorted(os.listdir(repeat_run_home_dir))
	# Try to load SingleRun from the directory
	single_run_path = _find_single_run_path(repeat_run_home_dir)

	if not single_run_path:
		raise FileNotFoundError(f"Could not find single_run_*.json in {repeat_run_home_dir} or its subdirectories.")

	single_run = SingleRun.load(single_run_path)

	#put in the main directory, which might be an ensemble or a single run
	recommenders = [StockRecommenderFromModel(single_run, repeat_run_home_dir, ensemble_normalize=ensemble_normalize, )]
	for name in entries:
		possible_rep_entry = os.path.join(repeat_run_home_dir, name)
		if name.lower().startswith("rep") and _dir_has_model(os.path.join(repeat_run_home_dir, name)):
			one_recommender = StockRecommenderFromModel(single_run, possible_rep_entry,	ensemble_normalize=ensemble_normalize,)
			recommenders.append(one_recommender)
	return recommenders

def _dir_has_model(run_dir: str) -> bool:
	"""Return True if the directory appears to contain a model manifest.

	We reuse StockRecommenderFromModel.resolve_manifest_path to locate a
	candidate manifest and then check that it exists on disk. This does not
	instantiate any models or require PyTorch.
	"""
	try:
		manifest_path = resolve_manifest_path(run_dir)
	except Exception as e:
		traceback.print_exception(e)
		return False
	return os.path.isfile(manifest_path)

def _find_single_run_path(run_dir: str) -> Optional[str]:
	"""Locate single_run_*.json for a given directory, supporting family/timestamp layouts."""
	import glob

	# Priority 1: Direct hit in run_dir
	patterns = [
		os.path.join(run_dir, "single_run_epoch.json"),
		os.path.join(run_dir, "single_run_*.json")
	]
	for pattern in patterns:
		matches = glob.glob(pattern)
		if matches:
			return matches[0]

	# Priority 2: If it's a rep dir, check parent
	if "rep" in os.path.basename(run_dir).lower():
		parent_dir = os.path.dirname(run_dir)
		for pattern in patterns:
			matches = glob.glob(os.path.join(parent_dir, os.path.basename(pattern)))
			if matches:
				return matches[0]

	# Priority 3: Search one level deep
	entries = _get_cached_entries(run_dir)
	for entry, is_dir in entries:
		if not is_dir:
			continue
		sub_dir = os.path.join(run_dir, entry)
		sub_entries = _get_cached_entries(sub_dir)
		for sub_f, sub_is_dir in sub_entries:
			if not sub_is_dir:
				for pattern in patterns:
					pat_name = os.path.basename(pattern)
					if re.match(pat_name.replace("*", ".*"), sub_f):
						return os.path.join(sub_dir, sub_f)
	return None


def _get_score_dirs(run_dir: str) -> List[str]:
	"""Return the list of directories that might contain score files (reps or run_dir itself)."""
	try:
		entries = _get_cached_entries(run_dir)
		rep_dirs = []
		for name, is_dir in sorted(entries):
			if is_dir and name.lower().startswith("rep") and name[3:].isdigit():
				rep_dirs.append(os.path.join(run_dir, name))
		if rep_dirs:
			return rep_dirs
	except Exception as e:
		print(f"Error listing {run_dir}: {e}")
	return [run_dir]


def _identify_epochs_with_scores(score_dirs: List[str]) -> List[str]:
	"""Identify all epochs that have saved score files in the given directories."""
	epochs_with_scores = set()
	try:
		for d in score_dirs:
			entries = _get_cached_entries(d)
			for fname, is_dir in entries:
				if not is_dir and fname.startswith("stock_scores_epoch_") and fname.endswith(".csv"):
					# Match epoch_(\d+)
					m = re.search(r"epoch_(\d+)", fname)
					if m:
						epochs_with_scores.add(m.group(1))
	except Exception as e:
		print(f"Error identifying epochs in score dirs: {e}")
	return sorted(list(epochs_with_scores), key=int)


def _get_epoch_dates(score_dirs: List[str], epoch: str) -> set[int]:
	"""Return the set of date_ids that have score files for a specific epoch across all score_dirs."""
	epoch_dates = set()
	for d in score_dirs:
		entries = _get_cached_entries(d)
		prefix = f"stock_scores_epoch_{epoch}_date_"
		for fname, is_dir in entries:
			if not is_dir and fname.startswith(prefix) and fname.endswith(".csv"):
				m = re.search(rf"epoch_{epoch}_date_(\d+)", fname)
				if m:
					epoch_dates.add(int(m.group(1)))
	return epoch_dates


def _map_dates_to_indices(provider: TrainingInstanceProvider, epoch_dates: set[int]) -> List[int]:
	"""Map date_ids to 1-based instance indices in the provider."""
	all_dates = provider.cumulative_dates_for_train_validation_and_test_sets.tolist()
	num_prior = provider.number_of_prior_days_to_include_in_feature
	available_indices = []
	for date_id in sorted(list(epoch_dates)):
		try:
			# Instance k corresponds to all_dates[num_prior + k - 1]
			# date_id = all_dates[idx] => idx = num_prior + k - 1 => k = idx - num_prior + 1
			idx = all_dates.index(date_id)
			k = idx - num_prior + 1
			if k >= 1:
				available_indices.append(k)
		except ValueError:
			continue
	return available_indices

def extending_list(list_to_extend: List[str], new_list: List[str]):
	"""Extend a list with another list, preserving the original list."""
	list_to_extend.extend(new_list)

def generate_simulations_from_directories(
	directories: List[str],
	top_k: List[int] = [1, 3, 5, 10],
	dbinfo_file: str = "dbinfo.id",
	only_ensemble: bool = False,
	force: bool = False,
) -> List[str]:
	"""
	Unified method to generate simulations from a list of directories.
	Handles family, single run (ensemble), and rep directories.
	"""
	all_generated = []
	for d in directories:
		if not os.path.isdir(d):
			print(f"Warning: {d} is not a directory. Skipping.")
			continue

		if is_family_directory(d, force_refresh=force):
			print(f"Processing family directory: {d}")
			sub_runs = []
			find_sub_runs_for_given_run_directory(d, sub_runs)
			# If it's a family directory, it should generate a list of single run directories in it,
			# then call the previous method to handle single run directories.
			temp_generated = generate_simulations_from_directories(sub_runs, top_k, dbinfo_file, only_ensemble, force=force)
			all_generated.extend(temp_generated)
		elif is_ensemble_directory_given(d, force_refresh=force):
			print(f"Processing ensemble directory: {d}")
			# Generate simulations for the ensemble itself and its reps (if requested)
			temp_generated = handle_single_run_directory(d, top_k, dbinfo_file, only_ensemble, force=force)
			all_generated.extend(temp_generated)
		elif is_rep_directory(d, force_refresh=force):
			# If rep directories, the list should repeatedly call a method to generate simulations from a rep directory.
			print(f"Processing rep directory: {d}")
			temp_generated = _run_simulation_for_single_or_ensemble(d, top_k, dbinfo_file, only_ensemble, force=force)
			all_generated.extend(temp_generated)
		else:
			print(f"INVALID DIRECTORY: {d}")
			# Or if it's just a plain single run directory without reps.
			# Try treating it as a single run anyway if it has a manifest or json
			if _find_single_run_path(d) or _dir_has_model(d):
				print(f"Processing single run directory: {d}")
				all_generated.extend(_run_simulation_for_single_or_ensemble(d, top_k, dbinfo_file, only_ensemble, force=force))
			else:
				print(f"INVALID DIRECTORY: {d}")
				# Don't raise error, just skip
				# raise ValueError(f"Invalid directory: {d}")

	return all_generated


def run_simulation_from_saved_scores(
	run_dir: str,
	top_k: List[int] = [1, 3, 5, 10],
	dbinfo_file: str = "dbinfo.id",
	only_ensemble: bool = False,
	force: bool = False,
) -> List[str]:
	"""
	Higher-level entry point for viewers.
	Scans the run_dir for all epochs that have saved scores and generates
	the corresponding stock_simulation_*.csv files.
	"""
	return generate_simulations_from_directories([run_dir], top_k, dbinfo_file, only_ensemble, force=force)


def grow_generate_run_list(all_generated: list[Any] | list[str], dbinfo_file: str, sub_runs: list[Any],
                           top_k: list[int], only_ensemble: bool = False):
	for sr in sub_runs:
		all_generated.extend(run_simulation_from_saved_scores(sr, top_k=top_k, dbinfo_file=dbinfo_file, only_ensemble=only_ensemble))


def find_sub_runs_for_given_run_directory(run_dir: str, sub_runs: list[Any]):
	entries = _get_cached_entries(run_dir)
	for entry, is_dir in sorted(entries):
		if not is_dir:
			continue
		full_entry = os.path.join(run_dir, entry)
		# Check for sub-run indicators
		sub_entries = _get_cached_entries(full_entry)

		is_sub_run = False
		if re.match(r"run_single_\d{8}_\d{6}_\d{2,}", entry) or \
			re.match(r"run_single_\d{8}_\d{6}_\d", entry) or \
			re.match(r"\d{8}_\d{6}", entry):
			is_sub_run = True
		else:
			for sub_f, sub_is_dir in sub_entries:
				if not sub_is_dir:
					if sub_f == "single_run_epoch.json" or (sub_f.startswith("single_run_") and sub_f.endswith(".json")):
						is_sub_run = True
						break

		if is_sub_run:
			sub_runs.append(full_entry)


def handle_single_run_directory(
	run_dir: str,
	top_k: List[int] = [1, 3, 5, 10],
	dbinfo_file: str = "dbinfo.id",
	only_ensemble: bool = False,
	force: bool = False,
) -> List[str]:
	"""
	Generate simulations for a single run directory, including ensemble if applicable.
	"""
	return _run_simulation_for_single_or_ensemble(run_dir, top_k, dbinfo_file, only_ensemble, force=force)


@line_profiler.profile
def _run_simulation_for_single_or_ensemble(
	run_dir: str,
	top_k: List[int] = [1, 3, 5, 10],
	dbinfo_file: str = "dbinfo.id",
	only_ensemble: bool = False,
	force: bool = False,
) -> List[str]:
	"""Internal helper to process a single run or an ensemble directory."""
	from trainer.state.single_run import SingleRun
	from trainer.datasets import IndexDataset
	from training_instance_provider import TrainingInstanceProvider

	# 1. Check for scores first - if no scores at all, don't bother initializing provider
	score_dirs = _get_score_dirs(run_dir)
	epochs_list = _identify_epochs_with_scores(score_dirs)

	if not epochs_list:
		# If it's a force regeneration, we should have refreshed the cache in caller
		# but if it was not a force, maybe we just need a refresh now because we expected something.
		# Refreshing anyway if empty to be sure.
		for sd in score_dirs:
			_get_cached_entries(sd, force_refresh=True)
		epochs_list = _identify_epochs_with_scores(score_dirs)
		if not epochs_list:
			print(f"DEBUG: [_run_simulation_for_single_or_ensemble] No score files found in {run_dir} or its score dirs {score_dirs}")
			return []

	start_total = time.perf_counter()

	single_run_path = _find_single_run_path(run_dir)
	if not single_run_path:
		print(f"Error: Could not find single_run_epoch.json for {run_dir}")
		return []

	single_run = SingleRun.load(single_run_path)

	# 2. Create Recommenders
	from StockRecommenderFromModel import StockRecommenderFromSavedScores
	# If it's an ensemble (has rep directories), we want the first recommender
	# to represent the ensemble (saving to run_dir).
	recommenders = [StockRecommenderFromSavedScores(single_run, output_directory=run_dir)]

	# If it's an ensemble (has rep directories), we might want multiple recommenders
	# but for saved scores, they are often aggregated or we can just use one
	# since we are loading precalculated scores anyway.
	# However, _load_epoch_scores expects a list of recommenders matching the net_X files.
	# Let's see how many net_X files there are.
	score_entries = _get_cached_entries(score_dirs[0])
	max_net_idx = -1
	epoch_prefix = f"stock_scores_epoch_{epochs_list[0]}_date_"
	for fname, is_dir in score_entries:
		if not is_dir and fname.startswith(epoch_prefix) and fname.endswith(".csv"):
			m = re.search(r"net_(\d+)", fname)
			if m:
				max_net_idx = max(max_net_idx, int(m.group(1)))

	if max_net_idx >= 0:
		if is_ensemble_directory_given(run_dir):
			# In an ensemble, the first recommender in the list passed to perform_analysis
			# is treated as the "parent" where ensemble results are saved.
			# We want it to point to run_dir.
			# Then we include all reps as source recommenders.
			ensemble_parent = StockRecommenderFromSavedScores(single_run, output_directory=run_dir)
			reps = []
			for sd in score_dirs:
				for _ in range(max_net_idx + 1):
					reps.append(StockRecommenderFromSavedScores(single_run, output_directory=sd))
			recommenders = [ensemble_parent] + reps
		else:
			# Not an ensemble, just use the first score dir (run_dir itself or its only rep)
			recommenders = [StockRecommenderFromSavedScores(single_run, output_directory=score_dirs[0]) for _ in range(max_net_idx + 1)]

	if not recommenders:
		print(f"Error: No recommenders found for {run_dir}")
		return []

	# 3. Setup Data Provider
	first_recommender = recommenders[0]
	start_prov = time.perf_counter()
	provider = TrainingInstanceProvider(
		dbinfo_file=dbinfo_file,
		current_day_change=first_recommender.get_current_day_change_used(),
		volume_ratio=first_recommender.get_volume_ratio_used(),
		vwap_ratio=first_recommender.get_vwap_ratio_used(),
		tranaction_ratio=first_recommender.get_tranaction_ratio_used(),
		day_change_centered=first_recommender.get_day_change_centered(),
		start_date_id=single_run.train_validation_test_sets_start_date_id,
		end_date_id=single_run.train_validation_test_sets_end_date_id,
	)
	provider.set_how_many_days_in_past_to_use(first_recommender.get_required_previous_days())
	print(f"DEBUG: [run_simulation_from_saved_scores] TrainingInstanceProvider initialized in {time.perf_counter() - start_prov:.2f}s")

	# 4. Process each epoch
	print(f"DEBUG: [run_simulation_from_saved_scores] Found {len(epochs_list)} epochs: {epochs_list}")

	generated_paths = []
	for epoch_idx, epoch in enumerate(epochs_list):
		epoch_dates = _get_epoch_dates(score_dirs, epoch)
		if not epoch_dates:
			continue

		print(f"DEBUG: [run_simulation_from_saved_scores] Epoch {epoch}: {len(epoch_dates)} dates found. Mapping to provider indices...")
		available_indices = _map_dates_to_indices(provider, epoch_dates)
		if not available_indices:
			continue

		dates_to_handle = IndexDataset(provider, available_indices)

		print(f"Generating simulation for epoch {epoch} ({epoch_idx+1}/{len(epochs_list)}) in {run_dir}...")
		start_epoch = time.perf_counter()
		out_path = perform_analysis(
			recommenders,
			top_k=top_k,
			dates_to_handle=dates_to_handle,
			dbinfo_file=dbinfo_file,
			out_filename_suffix=f"_epoch_{epoch}",
			only_ensemble=only_ensemble,
			force=force,
		)
		generated_paths.append(out_path)

	print(f"DEBUG: [run_simulation_from_saved_scores] All done in {time.perf_counter() - start_total:.2f}s")
	return generated_paths


def main(argv: Optional[List[str]] = None) -> None:
	args = parse_args(argv)
	print(f"Starting simulation with args: {args}")

	# Parse --top-k which may be a single int or comma-separated list
	top_k_parsed = parse_int_list(args.top_k, "top-k")

	if args.from_saved_scores:
		generate_simulations_from_directories(
			[args.model],
			top_k=top_k_parsed,
			dbinfo_file=args.dbinfo_file,
			only_ensemble=args.only_ensemble,
			force=args.force,
		)
		return

	model_arg = args.model
	stock_recommenders = create_list_of_stock_recommender_models_from_single_run_directory_with_repeats(args.ensemble_normalize, model_arg)

	if not stock_recommenders:
		print(f"No models found in {model_arg}")
		return

	# If it's an ensemble directory, we might need to adjust which models are being used
	# ensure the first one is the "ensemble" one if it's there
	is_ensemble = is_ensemble_directory_given(model_arg)
	if is_ensemble:
		print(f"DEBUG: Detected ensemble directory {model_arg}")
	elif len(stock_recommenders) > 1:
		print(f"DEBUG: Detected multiple models in {model_arg} but not identified as standard ensemble.")

	# Use the first recommender to configure the data provider
	first_recommender = stock_recommenders[0]

	# Create the TrainingInstanceProvider
	# Note: We don't have start_date_id/end_date_id from CLI, so we load everything
	# and then filter by date-range if provided.
	from training_instance_provider import TrainingInstanceProvider
	provider = TrainingInstanceProvider(
		dbinfo_file=args.dbinfo_file,
		limit=args.limit,
		current_day_change=first_recommender.get_current_day_change_used(),
		volume_ratio=first_recommender.get_volume_ratio_used(),
		vwap_ratio=first_recommender.get_vwap_ratio_used(),
		tranaction_ratio=first_recommender.get_tranaction_ratio_used(),
		day_change_centered=first_recommender.get_day_change_centered(),
	)
	provider.set_how_many_days_in_past_to_use(first_recommender.get_required_previous_days())

	# Determine which dates to handle
	all_dates = provider.cumulative_dates_for_train_validation_and_test_sets.tolist()
	available_indices = []

	if args.date_range:
		# Format: "start_date_id-end_date_id"
		parts = args.date_range.split("-")
		if len(parts) != 2:
			raise ValueError(f"Invalid date-range format: {args.date_range}. Expected start_id-end_id")
		start_id = int(parts[0])
		end_id = int(parts[1])

		# We need to find all date_ids in [start_id, end_id] that are available in the provider.
		# A date_id is available for simulation if it has enough prior context.
		# Instance k corresponds to all_dates[num_prior + k - 1]
		num_prior = provider.number_of_prior_days_to_include_in_feature
		max_inst = provider.get_maximum_number_of_test_train_instances()

		for k in range(1, max_inst + 1):
			date_id = all_dates[num_prior + k - 1]
			if start_id <= date_id <= end_id:
				available_indices.append(k)
	else:
		# Use all available instances
		available_indices = list(range(1, provider.get_maximum_number_of_test_train_instances() + 1))

	if not available_indices:
		print("No dates found in the specified range with sufficient prior data.")
		return

	dates_to_handle = IndexDataset(provider, available_indices)

	# Start processing
	simulate_using_models_passed(stock_recommenders, args, top_k_parsed=top_k_parsed, dates_to_handle=dates_to_handle)


if __name__ == "__main__":
	main()
