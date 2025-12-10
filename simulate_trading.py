import argparse
import os
import time
from typing import Tuple, Dict, Optional, List, Set

import numpy as np
import torch

from StockRecommenderFromModel import StockRecommenderFromModel
from feature_windowing import (
	best_fit_feature_vector,
	concat_features_prior_only,
	concat_features_current_then_prior,
)
from load_database_rows_into_tensors import fetch_grouped_by_date
from load_raw_stock_data_rows import fetch_as_numpy as fetch_raw_oc
from trainer.state.single_run import SingleRun
from trainer.util.argparse_utils import parse_int_list


def _build_open_close_matrices(
	keys_np: np.ndarray,
	feats_np: np.ndarray,
	dates: np.ndarray,
	T: int,
) -> Tuple[np.ndarray, np.ndarray]:
	"""Return open_mat, close_mat of shape (D, T) aligned to provided dates.

	Missing entries are NaN.
	"""
	D = int(dates.shape[0])
	open_mat = np.full((D, T), np.nan, dtype=np.float64)
	close_mat = np.full((D, T), np.nan, dtype=np.float64)

	# Map date_id -> row index in matrices
	date_to_idx: Dict[int, int] = {int(d): i for i, d in enumerate(dates)}

	for i in range(keys_np.shape[0]):
		dt_id = int(keys_np[i, 0])
		tkr_id = int(keys_np[i, 1])
		di = date_to_idx.get(dt_id)
		if di is None:
			continue
		ti = tkr_id - 1
		if not (0 <= ti < T):
			continue
		o = feats_np[i, 0]
		c = feats_np[i, 1]
		open_mat[di, ti] = o
		close_mat[di, ti] = c

	return open_mat, close_mat


def _concat_features(
	X_flat: np.ndarray,
	date_array_index: int,
	num_prior: int,
	prefer_prior_only: bool,
	expected_size: int,
	dates: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, Optional[int]]:
	"""Return the feature vector and the feature_date_id used to construct it.

	We attempt strictly prior-only first (if requested) and, if its size doesn't
	match the model's expected input, fall back to including the current day.

	If `dates` is provided, the returned tuple is (vec, feature_date_id) where
	feature_date_id corresponds to the most recent date included in the vector
	(d-1 for prior-only, or d when including current). If `dates` is None,
	the second element will be None.
	"""
	# Try prior-only if preferred and possible
	used_prior_only = False
	vec: Optional[np.ndarray] = None
	if prefer_prior_only and num_prior > 0:
		prior_vec = concat_features_prior_only(X_flat, date_array_index, num_prior)
		if int(prior_vec.size) == int(expected_size):
			vec = prior_vec
			used_prior_only = True

	# Fall back to including current day
	if vec is None:
		include_vec = concat_features_current_then_prior(X_flat, date_array_index, num_prior)
		if int(include_vec.size) != int(expected_size):
			# Delegate to best_fit_feature_vector to raise a descriptive error
			# (keeps error messaging consistent across the project)
			_ = best_fit_feature_vector(
				X_flat, date_array_index, num_prior, expected_size, prefer_prior_only=prefer_prior_only
			)
		vec = include_vec

	# Determine feature_date_id if dates provided
	feature_date_id: Optional[int] = None
	if dates is not None and dates.size > 0:
		feature_idx = (date_array_index - 1) if (used_prior_only and num_prior > 0) else date_array_index
		feature_idx = max(0, feature_idx)
		feature_date_id = int(dates[feature_idx])

	return vec, feature_date_id


def _average_scores(score_list: List[np.ndarray]) -> np.ndarray:
	"""Average a list of 1D score arrays elementwise.

	All arrays must be broadcast-compatible and share the same shape.
	"""
	if not score_list:
		raise ValueError("score_list must not be empty for ensemble averaging")
	s0 = np.asarray(score_list[0])
	if s0.ndim != 1:
		raise ValueError("Scores for ensemble averaging must be 1D arrays")
	stack = np.stack([np.asarray(s, dtype=np.float32).reshape(s0.shape) for s in score_list], axis=0)
	return stack.mean(axis=0)


def simulate(
	single_run: SingleRun,
	model_path: str,
	top_k:List[int],
	host: str,
	database: str,
	user: Optional[str],
	password: Optional[str],
	port: int,
	driver: str,
	limit: Optional[int],
	loaded_model: Optional["torch.nn.Module"] = None,
	model_dict: Optional[Dict[str, str]] = None,
	prefer_prior_only: bool = True,
	debug_sim_output: bool = False,
	device: str = "auto",
	dates_to_handle: Optional[List[int]] = None,
	single_logic:bool = True,
	single_model_rec: Optional[StockRecommenderFromModel] = None,
	ensemble_logic: bool = False,
#	ensemble_recs: Optional[List[StockRecommenderFromModel]] = None,
	ensemble_normalize: bool = False,
) -> str:
	"""Run a trading simulation for a single model or an ensemble of models.
	return a csv filename with the results.

	When ``ensemble_logic`` is False (default), ``model_path`` should point to a
	single training run directory or manifest and a single
	:class:`StockRecommenderFromModel` is used.

	When ``ensemble_logic`` is True, ``ensemble_recs`` must be a non-empty list
	of *compatible* StockRecommenderFromModel instances (same input_size,
	output_size, required_previous_days and feature flags). In this mode, for
	each step we:

	* run all models on the same feature vector,
	* sum their output scores elementwise,
	* divide by the number of models,
	* use the averaged scores to pick the top-K tickers and compute returns.
	"""
	ensemble_recs: List[StockRecommenderFromModel] = []
	if ensemble_logic and len(ensemble_recs)== 0:
		rec = StockRecommenderFromModel(
			model_path,
			device=device,
			ensemble_normalize=ensemble_normalize,
		)

	if single_logic:
		if single_model_rec is not None:
			rec = single_model_rec
		elif loaded_model is not None:
			rec = StockRecommenderFromModel(
				loaded_model,
				model_path=model_path,
				model_dict=model_dict,
				device=device,
				ensemble_normalize=ensemble_normalize,
			)
		else:
			rec = StockRecommenderFromModel(
				model_path,
				device=device,
				ensemble_normalize=ensemble_normalize,
			)
	#Ensemble calculations handled inside StockRecommenderFromModel
	ensemble_recs = [rec]

	required_prior = int(single_run.prior_days) if single_run is not None else 0
	expected_inp = int(rec.get_input_size()) if rec is not None else 0
	# print("prefer prior only is ", prefer_prior_only)
	# print("expected inp is ", expected_inp)
	# print("required prior is ", required_prior)
	# print("top k is ", top_k)
	# print("limit is ", limit)
	# print("driver is ", driver)
	# print("port is ", port)
	# print("user is ", user)
	# print("current day change used is ", rec.get_current_day_change_used())
	# print("features used is ", single_run.include_features if single_run is not None else "")
	# Load per-date flattened features (X_flat) and dates
	dates, X_flat, _Y = fetch_grouped_by_date(
		host=host,
		database=database,
		user=user,
		password=password,
		port=port,
		driver=driver,
		limit=limit,
		return_2d=False,
		current_day_change=rec.get_current_day_change_used(),
		volume_ratio=rec.get_volume_ratio_used(),
		vwap_ratio=rec.get_vwap_ratio_used(),
		tranaction_ratio=rec.get_tranaction_ratio_used(),
	)

	D = int(dates.shape[0])
	T = rec.output_size  # number of tickers
	TF = int(X_flat.shape[1])
	if TF % T != 0:
		raise ValueError(f"Feature width {TF} is not divisible by T={T}.")

	# Raw open/close matrices aligned to dates
	keys_oc, feats_oc = fetch_raw_oc(host=host, database=database, user=user, password=password, port=port, driver=driver, limit=limit)
	open_mat, close_mat = _build_open_close_matrices(keys_oc, feats_oc, dates, T)

	# ------------------------------------------------------------------
	# Determine simulation date window
	#
	# Primary mechanism: explicit list of all dates to handle.
	#
	# ``dates_to_handle`` is expected to be a list of integer date_ids, either
	# passed directly by callers (e.g. the incremental harness) or parsed from
	# the CLI ``--date-range`` argument. These ids are intersected with the
	# available ``date_ids`` loaded from the database.
	#
	# When multiple dates are provided we keep the existing convention where we
	# iterate over all but the final element, using each as the *feature* date
	# and trading on the following calendar day.
	#
	# When exactly one date is provided we interpret it as the *trade* date and
	# use the previous available date as the feature date (i.e. trade on N using
	# features from N-1). This matches the expectation for inputs like
	# ``--date-range 4-4``.
	#
	# For debugging, we print out the list of feature-date indices that will be
	# used for simulation once after it is computed.
	# ------------------------------------------------------------------
	date_ids = dates.astype(int).reshape(-1)
	date_ids_list = date_ids.tolist()
	date_ids_to_simulate_trading_on: List[int] = []

	if dates_to_handle is not None:
		# Normalise and clamp to the known date ids
		filtered_dates = sorted(set(int(d) for d in dates_to_handle).intersection(set(date_ids_list)))
		print("dates to handle:", filtered_dates)
		print("date handle list size ", len(filtered_dates))
		if len(filtered_dates) == 1:
			# Single-date window: trade on this date using features from the
			# previous available date, if possible.
			trade_date = filtered_dates[0]
			try:
				trade_idx = date_ids_list.index(trade_date)
			except ValueError:
				trade_idx = -1
			if trade_idx <= 0:
				raise ValueError(
					f"Cannot simulate for single date {trade_date}: no prior feature day available"
				)
			feature_idx = trade_idx - 1
			# Ensure we have enough prior days for the model requirements.
			if feature_idx - required_prior + 1 < 0:
				raise ValueError(
					f"Not enough prior days available before feature date {int(date_ids[feature_idx])} "
					f"to satisfy required_previous_days={required_prior}"
				)
			date_ids_to_simulate_trading_on.append(feature_idx)
		elif len(filtered_dates) >= 2:
			# Multi-date window: iterate over all but the final element so that we
			# can trade on the following calendar day.
			for feature_date in filtered_dates[:-1]:  # d+1 must be < D for trading
				try:
					idx = date_ids_list.index(feature_date)
				except ValueError:
					continue
				date_ids_to_simulate_trading_on.append(idx)
		print("dates to simulate length ", len(date_ids_to_simulate_trading_on))
		if not date_ids_to_simulate_trading_on:
			raise ValueError(
				f"No simulation dates found for dates given"
			)

	# For filename compatibility, derive an effective percentage based on
	# the number of simulated feature dates relative to the total.
	if date_ids_to_simulate_trading_on:
		effective_pct = 100.0 * (len(date_ids_to_simulate_trading_on) / float(D))
	else:
		effective_pct = 0.0
	pct = max(0.0, min(100.0, effective_pct))

	# Normalize top_k(s)
	# Unique, positive ints only

	# Portfolio initial values
	portfolio_values_by_k: Dict[int, float] = {k: 1000.0 for k in top_k}

	# Prepare output file path
	# We normalise the filename scheme so that both single-model and ensemble
	# simulations use the same ``stock_simulation_<size>.csv`` pattern, with
	# ensembles simply gaining an ``ensemble_`` prefix.  The ``<size>`` suffix
	# encodes either the number of simulated days (``Nd``) or an approximate
	# percentage of the available window (``Np``), matching what the
	# comparison/visualisation tools expect.
	stock_simulation_size = (
		f"{str(len(date_ids_to_simulate_trading_on))}days"
		if date_ids_to_simulate_trading_on
		else f"{str(int(pct))}percent"
	)
	if ensemble_logic:
		# For ensembles, prefer the directory passed in as model_path (root of reps)
		if os.path.isdir(model_path):
			model_dir = os.path.abspath(model_path)
		else:
			model_dir = os.path.dirname(os.path.abspath(rec.resolve_manifest_path(model_path)))
		out_name = f"stock_simulation_ensemble_{stock_simulation_size}.csv"
	else:
		model_dir = os.path.dirname(os.path.abspath(rec.resolve_manifest_path(model_path)))
		out_name = f"stock_simulation_{stock_simulation_size}.csv"
	out_path = os.path.join(model_dir, out_name)

	# Write header
	with open(out_path, "w", encoding="utf-8") as f:
		cols = [
			"feature_date_id",
			"open_date_id",
			"close_date_id",
			"top1_market_percentile",
			"top1_daily_return",
			"topNRecommends",
		]
		if len(top_k) == 1:
			# Extended header to include explicit date ids used
			cols_single =cols +  [
				"portfolio_value",
			]
			if debug_sim_output:
				# Add debug-only columns: top3 ids (comma-separated), close_ratio for top1, raw close for top1
				cols_single += [
					"top3_ticker_ids",
					"close_ratio_top1",
					"raw_close_top1",
					"scores_std",
					"top1_score",
				]
			f.write(",".join(cols_single) + "\n")
		else:
			cols += [f"portfolio_top{k}" for k in top_k]
			if debug_sim_output:
				cols += [
					"top3_ticker_ids",
					"close_ratio_top1",
					"raw_close_top1",
					"scores_std",
					"top1_score",
				]
			f.write(",".join(cols) + "\n")

		for date_array_index in date_ids_to_simulate_trading_on:
			# Build feature vector using prior days information
			feat_vec, feature_date_id = _concat_features(
				X_flat, date_array_index, required_prior, prefer_prior_only, expected_inp, dates
			)

			# Get recommendations up to the largest K once (ensure at least 3 for debug output)
			max_k = max(top_k)
			req_k = int(max(max_k, 3))

			if ensemble_logic:
				# Run all models, average their raw scores, then derive rankings.
				all_scores: List[np.ndarray] = []
				for m in ensemble_recs or []:
					model_scores, _ = m.getBuyRecommendations(
						feat_vec, top_k=req_k, index_base=0
					)
					all_scores.append(model_scores)
				avg_scores = _average_scores(all_scores)
				T_local = avg_scores.shape[0]
				# Sort to get top indices (largest first)
				idx_desc = np.argsort(avg_scores)[::-1]
				idx_desc = idx_desc[: min(req_k, T_local)]
				# Map to ticker ids using the canonical recommender's mapping
				# if rec._ticker_id_map and len(rec._ticker_id_map) == rec._output_size:  # type: ignore[attr-defined]
				# 	top_ids_all = [int(rec._ticker_id_map[i]) for i in idx_desc]
				# else:
					# Dense indices -> 1..T
				top_ids_all = [int(i + 1) for i in idx_desc]
				# For debug columns we treat the averaged scores as the score vector
				scores_all = avg_scores
			if single_logic:
				# Single-model case (backwards compatible path)
				scores_all, top_ids_all = rec.getBuyRecommendations(
					feat_vec, top_k=req_k, index_base=1)

			# Compute portfolio daily return using equal-weight allocation among valid picks
			# IMPORTANT: Trades are executed the day AFTER the feature vector date.
			# We buy at next day's open and sell at next day's close (same day),
			# so both open/close belong to the next calendar date relative to the feature.
			trade_idx = date_array_index + 1
			if trade_idx >= D:
				# No next-day data available; skip this index (should be rare due
				# to how sim_indices is constructed).
				continue
			todays_open = open_mat[trade_idx]
			todays_close = close_mat[trade_idx]

			# Update each K-specific portfolio using equal-weight allocation
			for k in top_k:
				chosen = top_ids_all[:k]
				valid_returns: List[float] = []
				for tid in chosen:
					idx = tid - 1
					if not (0 <= idx < T):
						continue
					o = todays_open[idx]
					c = todays_close[idx]
					if np.isfinite(o) and np.isfinite(c) and o > 0.0 and c > 0.0:
						valid_returns.append((c / o) - 1.0)
				if valid_returns:
					day_ret = float(np.mean(valid_returns))
					portfolio_values_by_k[k] *= (1.0 + day_ret)
				else:
					# No valid trades; remain in cash
					portfolio_values_by_k[k] *= 1.0

			# --- Calculate Top 1 Market Percentile (CDF) and daily return ---
			top1_ret_val = float('nan')
			cdf_val = float('nan')
			if top_ids_all and len(top_ids_all) >= 1:
				t1 = int(top_ids_all[0])
				idx1 = t1 - 1
				if 0 <= idx1 < T:
					o1 = todays_open[idx1]
					c1 = todays_close[idx1]
					if np.isfinite(o1) and np.isfinite(c1) and o1 > 0:
						top1_ret_val = (c1 / o1) - 1.0

						# Market returns
						valid_mask = (todays_open > 0) & np.isfinite(todays_open) & np.isfinite(todays_close)
						if np.any(valid_mask):
							m_opens = todays_open[valid_mask]
							m_closes = todays_close[valid_mask]
							m_rets = (m_closes / m_opens) - 1.0
							cdf_val = np.mean(m_rets <= top1_ret_val)

			cdf_str = f"{cdf_val:.6f}" if np.isfinite(cdf_val) else ""
			top1_ret_str = f"{top1_ret_val:.6f}" if np.isfinite(top1_ret_val) else ""

			# --- Top-N recommended tickers (largest k) ---
			max_k = max(top_k)
			rec_ids = top_ids_all[: max_k] if top_ids_all is not None else []
			topn_str = "|".join(str(int(tid)) for tid in rec_ids) if rec_ids else ""

			# Record trade date ids as the next day's date (buy and sell same day)
			open_date_id = int(dates[trade_idx])
			close_date_id = int(dates[trade_idx])

			# Prepare debug extras if requested
			debug_cols: List[str] = []
			if debug_sim_output:
				# Top 3 ticker ids column, comma-separated inside quotes for CSV safety
				if top_ids_all is not None and len(top_ids_all) > 0:
					top3 = [str(int(tid)) for tid in top_ids_all[: min(3, len(top_ids_all))]]
					top3_col = '"' + ",".join(top3) + '"'
				else:
					top3_col = '""'

				# Close ratio and raw close for top-1 ticker (if available)
				close_ratio_str = ""
				raw_close_str = ""
				if top_ids_all is not None and len(top_ids_all) >= 1:
					t1 = int(top_ids_all[0])
					i1 = t1 - 1
					# raw close from close_mat for the TRADE day (next day after feature)
					if 0 <= i1 < T:
						cval = close_mat[trade_idx, i1]
						if np.isfinite(cval):
							raw_close_str = f"{float(cval):.6f}"
					# close_ratio from features X_flat for the day, assuming feature 0 is close_ratio
					F = TF // T if T > 0 else 0
					if F > 0 and 0 <= i1 < T:
						feat_idx = (i1 * F) + 0
						if 0 <= feat_idx < X_flat.shape[1]:
							cr = X_flat[date_array_index, feat_idx]
							if np.isfinite(cr):
								close_ratio_str = f"{float(cr):.6f}"

				# Score dispersion diagnostics
				try:
					std_val = float(np.std(scores_all)) if isinstance(scores_all, np.ndarray) else float('nan')
					max_val = float(np.max(scores_all)) if isinstance(scores_all, np.ndarray) else float('nan')
				except Exception:
					std_val = float('nan')
					max_val = float('nan')

				debug_cols = [
					top3_col,
					close_ratio_str,
					raw_close_str,
					(f"{std_val:.6f}" if np.isfinite(std_val) else ""),
					(f"{max_val:.6f}" if np.isfinite(max_val) else ""),
				]

			if len(top_k) == 1:
				only_k = top_k[0]
				row = [
					str(feature_date_id if feature_date_id is not None else ""),
					str(open_date_id),
					str(close_date_id),
					cdf_str,
					top1_ret_str,
					topn_str,
					f"{portfolio_values_by_k[only_k]:.6f}",
				]
				if debug_sim_output:
					row += debug_cols
				f.write(",".join(row) + "\n")
			else:
				row_vals = [
					str(feature_date_id if feature_date_id is not None else ""),
					str(open_date_id),
					str(close_date_id),
					cdf_str,
					top1_ret_str,
					topn_str,
				]
				row_vals += [f"{portfolio_values_by_k[k]:.6f}" for k in top_k]
				if debug_sim_output:
					row_vals += debug_cols
				f.write(",".join(row_vals) + "\n")

	return out_path


def parse_args(argv: Optional[List[str]] = None):
	p = argparse.ArgumentParser(description="Simulate daily trading using a trained model.")
	p.add_argument("model", help="Path to model directory or model_manifest.json")
#	p.add_argument("--percent", type=float, default=10.0, help="Percentage of dates to simulate (use last N percent)")
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

	# Data loading / DB options
	p.add_argument("--host", default=os.getenv("MYSQL_HOST", "localhost"))
	p.add_argument("--database", "--db", dest="database", default=os.getenv("MYSQL_DB", "stockforecasting"))
	p.add_argument("--user", default=os.getenv("MYSQL_USER", "Redbeard"))
	p.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"))
	p.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
	p.add_argument("--driver", default="auto", choices=["auto", "mysqlconnector", "pymysql"], help="DB driver")
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
		"--ensemble-logic",
		action="store_true",
		help=(
			"If set and the provided model path is a directory containing rep* subdirectories, "
			"load all those runs as an ensemble, average their output scores per step, and "
			"use the averaged scores to drive trading. By default (flag not set), when such "
			"a directory is provided, a separate simulation is run for each rep* subdirectory."
		),
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

def simulate_using_models_on_path(path: str, args: argparse.Namespace, visited: Optional[Set[str]] = None, top_k_parsed=None,
                                  dates_to_handle=None, include_ensemble: bool = False) -> None:
	"""Recursively run simulations starting from ``path``.

	Behaviour matches the previous flat-directory logic but, when a directory
	contains neither rep* subdirectories nor a model manifest, we descend into
	its subdirectories and repeat the same checks.

	Additionally, we record and print the wall-clock time taken to process
	each path (directory or single-model file), including any recursive
	descendants.
	"""
	if visited is None:
		visited = set()
	abs_path = os.path.abspath(path)
	if abs_path in visited:
		print(f"Skipping already-visited directory: {abs_path}")
		return
	visited.add(abs_path)

	start_time = time.perf_counter()
	print(f"Starting simulations for {path}...")
	try:
		if os.path.isdir(path):
			all_models_found: List[
				StockRecommenderFromModel] = create_list_of_stock_recommender_models_from_single_run_directory_with_repeats(args,path)

			# Always run individual rep simulations for any detected rep* dirs
			if all_models_found:
				print(f"Detected {len(all_models_found)} rep* directories under {path}; running simulation separately for each rep.")
				last_out: Optional[str] = None
				for one_recommender in all_models_found:
					print(f"Running simulation for  ...")
					last_out = simulate(
						None,
						model_path=path,
						top_k=top_k_parsed,
						host=args.host,
						database=args.database,
						user=args.user,
						password=args.password,
						port=args.port,
						driver=args.driver,
						limit=args.limit,
						prefer_prior_only=bool(args.prior_only),
						debug_sim_output=bool(args.debug_sim_output),
						device=str(args.device),
						dates_to_handle=dates_to_handle,
						single_model_rec=one_recommender,
						ensemble_normalize=bool(args.ensemble_normalize),
					)
				if last_out is not None:
					print(f"Simulation complete for reps under {path}. Last results written to: {last_out}")

				# If ensemble logic is requested and we have multiple reps, run an
				# additional ensemble simulation over all rep* runs.
				if include_ensemble and len(all_models_found) > 1:
					print(
						f"Detected {len(all_models_found)} rep* directories under {path}; "
						"running additional ensemble simulation with averaged scores "
						"after individual rep simulations."
					)
					out_path = simulate(
						None,
						model_path=path,
						top_k=top_k_parsed,
						host=args.host,
						database=args.database,
						user=args.user,
						password=args.password,
						port=args.port,
						driver=args.driver,
						limit=args.limit,
						prefer_prior_only=bool(args.prior_only),
						debug_sim_output=bool(args.debug_sim_output),
						device=str(args.device),
						ensemble_logic=True,
						dates_to_handle=dates_to_handle,
						ensemble_normalize=bool(args.ensemble_normalize),
					)
					print(f"Ensemble simulation complete. Results written to: {out_path}")
				return

			# No rep* directories. If this directory itself contains a model
			# manifest, behave like the original single-model path.
			if _dir_has_model(path):
				out_path = simulate(
					None,
					model_path=path,
					top_k=top_k_parsed,
					host=args.host,
					database=args.database,
					user=args.user,
					password=args.password,
					port=args.port,
					driver=args.driver,
					limit=args.limit,
					prefer_prior_only=bool(args.prior_only),
					debug_sim_output=bool(args.debug_sim_output),
					device=str(args.device),
					dates_to_handle=dates_to_handle,
					ensemble_normalize=bool(args.ensemble_normalize),
				)
				print(f"Simulation complete. Results written to: {out_path}")
				return

			# Directory without rep* subdirectories and without a model manifest:
			# descend into its subdirectories and repeat the same checks.
			entries = sorted(os.listdir(path))
			subdirs = [
				os.path.join(path, name)
				for name in entries
				if os.path.isdir(os.path.join(path, name))
			]
			if not subdirs:
				print(f"No models or rep* directories found in {path} and no subdirectories to search.")
				return
			for sub in subdirs:
				print(f"Descending into subdirectory {sub} to search for models or rep* runs...")
				simulate_using_models_on_path(sub, args, visited,top_k_parsed= top_k_parsed,dates_to_handle= dates_to_handle,include_ensemble= include_ensemble)
		else:
			# Non-directory path: single-model behaviour.
			out_path = simulate(
				None,
				model_path=path,
				top_k=top_k_parsed,
				host=args.host,
				database=args.database,
				user=args.user,
				password=args.password,
				port=args.port,
				driver=args.driver,
				limit=args.limit,
				prefer_prior_only=bool(args.prior_only),
				debug_sim_output=bool(args.debug_sim_output),
				device=str(args.device),
				dates_to_handle=dates_to_handle,
			)
			print(f"Simulation complete. Results written to: {out_path}")

	finally:
		elapsed = time.perf_counter() - start_time
		print(f"Completed simulations for {path} in {elapsed:.2f} seconds")

def create_list_of_stock_recommender_models_from_single_run_directory_with_repeats(args:argparse.Namespace ,  repeat_run_home_dir: str) -> list[
	StockRecommenderFromModel]:
	entries = sorted(os.listdir(repeat_run_home_dir))
	rep_dirs = [
		os.path.join(repeat_run_home_dir, name)
		for name in entries
		if name.lower().startswith("rep") and os.path.isdir(os.path.join(repeat_run_home_dir, name))
	]
	return [
		StockRecommenderFromModel(
			d,
			device=args.device,
			ensemble_normalize=bool(args.ensemble_normalize),
		)
		for d in rep_dirs
	]


def _dir_has_model(run_dir: str) -> bool:
	"""Return True if the directory appears to contain a model manifest.

	We reuse StockRecommenderFromModel.resolve_manifest_path to locate a
	candidate manifest and then check that it exists on disk. This does not
	instantiate any models or require PyTorch.
	"""
	try:
		manifest_path = StockRecommenderFromModel.resolve_manifest_path(run_dir)  # type: ignore[attr-defined]
	except Exception:
		return False
	return os.path.isfile(manifest_path)

def main(argv: Optional[List[str]] = None) -> None:
	args = parse_args(argv)
	print(f"Starting simulation with args: {args}")

	# Parse --top-k which may be a single int or comma-separated list
	top_k_parsed = parse_int_list(args.top_k, "top-k")
	dates_to_handle = parse_int_list(args.date_range, "date-range") if args.date_range is not None else None
	include_ensemble = args.include_ensemble

	model_arg = args.model



	# Start processing from the user-provided model argument
	simulate_using_models_on_path(model_arg, args, top_k_parsed=top_k_parsed, dates_to_handle=dates_to_handle, include_ensemble=include_ensemble)


if __name__ == "__main__":
	main()
