import argparse
import os
import time
import traceback
from typing import Tuple, Dict, Optional, List

import numpy as np
import torch
from torch import nn

from StockRecommenderFromModel import StockRecommenderFromModel
from debug_feature_logging import dump_feature_window, env_feature_debug_enabled
from feature_windowing import (
	best_fit_feature_vector,
	concat_features_prior_only,
	concat_features_current_then_prior,
)
from get_ticker_position_on_date import get_ticker_positions
from load_database_rows_into_tensors import fetch_grouped_by_date
from load_raw_stock_data_rows import fetch_open_close_for_date_tickers
from models.utils import resolve_manifest_path  # type: ignore
from trainer.datasets import IndexDataset
from trainer.state.single_run import SingleRun
from trainer.util.argparse_utils import parse_int_list


def simulate(
	stock_recommenders: List[StockRecommenderFromModel],
	top_k: List[int],
	dates_to_handle: IndexDataset,
	dbinfo_file: str = "dbinfo.id",
	out_filename_suffix: Optional[str] = None,
) -> str:

	"""Run a trading simulation for a single model or an ensemble of models.
	return a csv filename with the results.

	"""
	out_path = ""
	for recommender_index in range(len(stock_recommenders)):
		stock_recommender = stock_recommenders[recommender_index]
		required_prior = stock_recommender.get_required_previous_days()

		# Portfolio initial values
		portfolio_values_by_k: Dict[int, float] = {k: 1000.0 for k in top_k}
		stock_pick_daily_returns: Dict[int, float] = {}
		stock_pick_daily_ranks: Dict[int, float] = {}

		# Prepare output file path
		model_dir = stock_recommender.get_output_directory()
		num_days = len(dates_to_handle)
		stock_simulation_size = f"{num_days}days"

		if stock_recommender.get_model_number()>1:
			out_name = f"stock_simulation_ensemble_{stock_simulation_size}{out_filename_suffix or ''}.csv"
		else:
			out_name = f"stock_simulation_{stock_simulation_size}{out_filename_suffix or ''}.csv"
		out_path = os.path.join(model_dir, out_name)

		# Write header
		with open(out_path, "w", encoding="utf-8") as f:
			cols = [
				"feature_date_id",
				"open_date_id",
				"close_date_id",
				"topNRecommends",
			]
			for k in top_k:
				cols += [f"Return Percent top{k}"]
				cols += [f"Rank Percent top{k}"]
				cols += [f"Portfolio return top{k}"]
			f.write(",".join(cols) + "\n")

			# Get all date_ids in order from the provider
			provider = dates_to_handle.provider
			all_dates = provider.cumulative_dates_for_train_validation_and_test_sets.tolist()

			feature_label_date_gen = dates_to_handle.get_next_feature_label_date()
			for feat_vec, label_vec, feature_date_id in feature_label_date_gen:
				# feature_date_id is the date the feature vector belongs to.
				# We want to trade on the NEXT available date.
				try:
					date_idx = all_dates.index(int(feature_date_id))
				except ValueError:
					print(f"Warning: feature_date_id {feature_date_id} not found in provider dates")
					continue

				trade_idx = date_idx + 1
				if trade_idx >= len(all_dates):
					# No next day data available to trade on
					continue

				trade_date_id = int(all_dates[trade_idx])

				# Get recommendations
				max_k = max(top_k)
				# getBuyRecommendations returns (scores, top_ids)
				_scores, top_ids_all = stock_recommender.getBuyRecommendations(
					feat_vec, top_k=max_k, index_base=1, current_date_id=feature_date_id, dbinfo_file=dbinfo_file,
				)

				# Fetch actual open/close prices to calculate real returns
				needed_tickers_list = [int(tid) for tid in top_ids_all[:max_k]]
				price_data_map = fetch_open_close_for_date_tickers(
					dbinfo_file=dbinfo_file,
					date_id=trade_date_id,
					ticker_ids=needed_tickers_list,
				)

				# Update each K-specific portfolio using average close/open ratio
				for k in top_k:
					chosen = top_ids_all[:k]
					valid_ratios: List[float] = []
					for tid in chosen:
						prices = price_data_map.get(int(tid))
						if prices is not None:
							open_p, close_p = prices
							if open_p > 0 and not np.isnan(open_p) and not np.isnan(close_p):
								valid_ratios.append(close_p / open_p)

					if valid_ratios:
						day_ratio_avg = float(np.mean(valid_ratios))
						stock_pick_daily_returns[k] = day_ratio_avg - 1.0

						# Calculate percentile rank of day_ratio_avg against all stocks in model's ticker pool
						# Filter by recommender's ticker pool
						pool_ticker_ids = stock_recommender.ticker_ids
						positions_map = get_ticker_positions(
							ticker_ids=chosen,
							date_id=trade_date_id,
							dbinfo_file=dbinfo_file,
							pool_ticker_ids=pool_ticker_ids
						)
						# Average position of the chosen tickers
						if positions_map:
							stock_pick_daily_ranks[k] = float(np.mean(list(positions_map.values())))
						else:
							stock_pick_daily_ranks[k] = 0.0

						# Multiply portfolio by the average of top-k close/open ratios
						portfolio_values_by_k[k] *= day_ratio_avg
					else:
						stock_pick_daily_returns[k] = 0.0
						stock_pick_daily_ranks[k] = 0.0
						portfolio_values_by_k[k] *= 1.0

				# Record trade date ids as the next day's date (buy and sell same day)
				open_date_id = trade_date_id
				close_date_id = trade_date_id
				topn_str = "|".join(str(int(tid)) for tid in top_ids_all[:max_k])

				row_vals = [
					str(feature_date_id),
					str(open_date_id),
					str(close_date_id),
					topn_str,
				]
				for k in top_k:
					row_vals += [f"{stock_pick_daily_returns[k]:.6f}"]
					row_vals += [f"{stock_pick_daily_ranks[k]:.6f}"]
					row_vals += [f"{portfolio_values_by_k[k]:.6f}"]
				f.write(",".join(row_vals) + "\n")

	return out_path


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
	return p.parse_args(argv)

def simulate_using_models_passed(models_passed: List[StockRecommenderFromModel], args: argparse.Namespace, top_k_parsed=None,
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
		)
		print(f"Simulation complete. Results written to: {out_path}")

	finally:
		elapsed = time.perf_counter() - start_time
		print(f"Completed simulations in {elapsed:.2f} seconds")

def is_ensemble_directory_given(path: str) -> bool:
	"""Check if the given path is an ensemble directory based on naming convention."""
	entries = sorted(os.listdir(path))
	rep_dirs = []
	for name in entries:
		if name.lower().startswith("rep") and os.path.isdir(os.path.join(path, name)):
			rep_dirs.append(name)
	return len(rep_dirs) > 1


def create_list_of_stock_recommender_models_from_single_run_directory_with_repeats(ensemble_normalize:bool ,  repeat_run_home_dir: str) -> list[
	StockRecommenderFromModel]:
	entries = sorted(os.listdir(repeat_run_home_dir))
	#put in the main directory, which might be an ensemble or a single run
	recommenders = [StockRecommenderFromModel(repeat_run_home_dir, ensemble_normalize=ensemble_normalize, )]
	for name in entries:
		possible_rep_entry = os.path.join(repeat_run_home_dir, name)
		if name.lower().startswith("rep") and _dir_has_model(os.path.join(repeat_run_home_dir, name)):
			one_recommender = StockRecommenderFromModel(possible_rep_entry,	ensemble_normalize=ensemble_normalize,)
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

def main(argv: Optional[List[str]] = None) -> None:
	args = parse_args(argv)
	print(f"Starting simulation with args: {args}")

	# Parse --top-k which may be a single int or comma-separated list
	top_k_parsed = parse_int_list(args.top_k, "top-k")

	model_arg = args.model
	stock_recommenders = create_list_of_stock_recommender_models_from_single_run_directory_with_repeats(args.ensemble_normalize, model_arg)

	if not stock_recommenders:
		print(f"No models found in {model_arg}")
		return

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
