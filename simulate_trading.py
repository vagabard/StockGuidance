import argparse
import math
import os
import time
from typing import Tuple, Dict, Optional, List, Union, Set

import numpy as np

from StockRecommenderFromModel import StockRecommenderFromModel
from feature_windowing import (
	best_fit_feature_vector,
	concat_features_prior_only,
	concat_features_current_then_prior,
)
from load_database_rows_into_tensors import fetch_grouped_by_date
from load_raw_stock_data_rows import fetch_as_numpy as fetch_raw_oc


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
	d: int,
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
		prior_vec = concat_features_prior_only(X_flat, d, num_prior)
		if int(prior_vec.size) == int(expected_size):
			vec = prior_vec
			used_prior_only = True

	# Fall back to including current day
	if vec is None:
		include_vec = concat_features_current_then_prior(X_flat, d, num_prior)
		if int(include_vec.size) != int(expected_size):
			# Delegate to best_fit_feature_vector to raise a descriptive error
			# (keeps error messaging consistent across the project)
			_ = best_fit_feature_vector(
				X_flat, d, num_prior, expected_size, prefer_prior_only=prefer_prior_only
			)
		vec = include_vec

	# Determine feature_date_id if dates provided
	feature_date_id: Optional[int] = None
	if dates is not None and dates.size > 0:
		feature_idx = (d - 1) if (used_prior_only and num_prior > 0) else d
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
	model_path: str,
	percent: float,
	top_k: Union[int, List[int]],
	host: str,
	database: str,
	user: Optional[str],
	password: Optional[str],
	port: int,
	driver: str,
	limit: Optional[int],
	loaded_model: Optional["torch.nn.Module"],
	sp500_ticker_id: Optional[int],
	prefer_prior_only: bool = True,
	debug_sim_output: bool = False,
	device: str = "auto",
	ensemble_logic: bool = False,
	ensemble_recs: Optional[List[StockRecommenderFromModel]] = None,
) -> str:
	"""Run a trading simulation for a single model or an ensemble of models.

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
	if ensemble_logic:
		if not ensemble_recs:
			raise ValueError("ensemble_logic=True requires a non-empty ensemble_recs list")
		# Use the first model as canonical and validate compatibility
		rec = ensemble_recs[0]
		for other in ensemble_recs[1:]:
			if (
				other.get_input_size() != rec.get_input_size()
				or other._output_size != rec._output_size  # type: ignore[attr-defined]
				or other.get_required_previous_days() != rec.get_required_previous_days()
				or other.get_current_day_change_used() != rec.get_current_day_change_used()
				or other.get_volume_ratio_used() != rec.get_volume_ratio_used()
				or other.get_vwap_ratio_used() != rec.get_vwap_ratio_used()
				or other.get_tranaction_ratio_used() != rec.get_tranaction_ratio_used()
			):
				raise ValueError("All ensemble models must have identical input/output sizes and feature flags")
	else:
		if loaded_model is not None:
			rec = StockRecommenderFromModel(loaded_model, device=device)
		else:
			rec = StockRecommenderFromModel(model_path, device=device)
		ensemble_recs = [rec]

	required_prior = int(rec.get_required_previous_days())
	expected_inp = int(rec.get_input_size())
	#print("close only is ", close_ratio_only)
	print("sp500 ticker id is ", sp500_ticker_id)
	print("prefer prior only is ", prefer_prior_only)
	print("expected inp is ", expected_inp)
	print("required prior is ", required_prior)
	print("top k is ", top_k)
	print("limit is ", limit)
	print("driver is ", driver)
	print("port is ", port)
	print("user is ", user)
	print("current day change used is ", rec.get_current_day_change_used())
	print("volume ratio used is ", rec.get_volume_ratio_used())
	print("vwap ratio used is ", rec.get_vwap_ratio_used())
	print("tranaction ratio used is ", rec.get_tranaction_ratio_used())
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
	T = rec._output_size  # number of tickers
	TF = int(X_flat.shape[1])
	if TF % T != 0:
		raise ValueError(f"Feature width {TF} is not divisible by T={T}.")

	# Raw open/close matrices aligned to dates
	keys_oc, feats_oc = fetch_raw_oc(host=host, database=database, user=user, password=password, port=port, driver=driver, limit=limit)
	open_mat, close_mat = _build_open_close_matrices(keys_oc, feats_oc, dates, T)

	# Determine simulation date window (last percent of dates)
	pct = max(0.0, min(100.0, float(percent)))
	sim_len = max(1, int(math.ceil(D * (pct / 100.0))))
	start_idx = max(required_prior, D - sim_len)

	# Normalize top_k(s)
	if isinstance(top_k, int):
		top_ks: List[int] = [int(top_k)]
	else:
		# Unique, positive ints only
		top_ks = sorted({int(k) for k in top_k if isinstance(k, (int, np.integer)) or str(k).strip().isdigit()})
		top_ks = [k for k in top_ks if k > 0]
		if not top_ks:
			top_ks = [10]
	# Cap each K to available tickers T, unique and sort
	top_ks = sorted(set(min(k, T) for k in top_ks))

	# Portfolio and benchmark initial values
	sp500_value: Optional[float] = None
	if sp500_ticker_id is not None and 1 <= sp500_ticker_id <= T:
		sp500_value = 1000.0
	portfolio_values_by_k: Dict[int, float] = {k: 1000.0 for k in top_ks}

	# Prepare output file path
	if ensemble_logic:
		# For ensembles, prefer the directory passed in as model_path (root of reps)
		if os.path.isdir(model_path):
			model_dir = os.path.abspath(model_path)
		else:
			model_dir = os.path.dirname(os.path.abspath(rec._resolve_manifest_path(model_path)))
		out_name = f"stock_simulation_ensemble_{int(pct)}.csv"
	else:
		model_dir = os.path.dirname(os.path.abspath(rec._resolve_manifest_path(model_path)))
		out_name = f"stock_simulation_{int(pct)}.csv"
	out_path = os.path.join(model_dir, out_name)

	# Write header
	with open(out_path, "w", encoding="utf-8") as f:
		if len(top_ks) == 1:
			# Extended header to include explicit date ids used
			cols_single = [
				"feature_date_id",
				"open_date_id",
				"close_date_id",
				"portfolio_value",
			]
			if sp500_value is not None:
				cols_single.append("sp500_value")
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
			cols = [
				"feature_date_id",
				"open_date_id",
				"close_date_id",
			]
			if sp500_value is not None:
				cols.append("sp500_value")
			cols += [f"portfolio_top{k}" for k in top_ks]
			if debug_sim_output:
				cols += [
					"top3_ticker_ids",
					"close_ratio_top1",
					"raw_close_top1",
					"scores_std",
					"top1_score",
				]
			f.write(",".join(cols) + "\n")

		for d in range(start_idx, D):
			# Build feature vector using prior days information
			feat_vec, feature_date_id = _concat_features(
				X_flat, d, required_prior, prefer_prior_only, expected_inp, dates
			)

			# Get recommendations up to the largest K once (ensure at least 3 for debug output)
			max_k = max(top_ks)
			req_k = int(max(max_k, 3))

			if ensemble_logic:
				# Run all models, average their raw scores, then derive rankings.
				all_scores: List[np.ndarray] = []
				for m in ensemble_recs or []:
					model_scores, _ = m.getBuyRecommendations(
						feat_vec, top_k=req_k, return_scores=True, index_base=0
					)
					all_scores.append(model_scores)
				avg_scores = _average_scores(all_scores)
				T_local = avg_scores.shape[0]
				# Sort to get top indices (largest first)
				idx_desc = np.argsort(avg_scores)[::-1]
				idx_desc = idx_desc[: min(req_k, T_local)]
				# Map to ticker ids using the canonical recommender's mapping
				if rec._ticker_id_map and len(rec._ticker_id_map) == rec._output_size:  # type: ignore[attr-defined]
					top_ids_all = [int(rec._ticker_id_map[i]) for i in idx_desc]
				else:
					# Dense indices -> 1..T
					top_ids_all = [int(i + 1) for i in idx_desc]
				# For debug columns we treat the averaged scores as the score vector
				scores_all = avg_scores
			else:
				# Single-model case (backwards compatible path)
				scores_all, top_ids_all = rec.getBuyRecommendations(
					feat_vec, top_k=req_k, return_scores=True, index_base=1
				)

			# Compute portfolio daily return using equal-weight allocation among valid picks
			# IMPORTANT: Trades are executed the day AFTER the feature vector date.
			# We buy at next day's open and sell at next day's close (same day),
			# so both open/close belong to the next calendar date relative to the feature.
			trade_idx = d + 1
			if trade_idx >= D:
				# No next-day data available; stop simulation for remaining dates
				break
			todays_open = open_mat[trade_idx]
			todays_close = close_mat[trade_idx]

			# Update each K-specific portfolio using equal-weight allocation
			for k in top_ks:
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

			# Benchmark: S&P 500 ticker daily return
			if sp500_value is not None and sp500_ticker_id is not None and 1 <= sp500_ticker_id <= T:
				idx = sp500_ticker_id - 1
				o = todays_open[idx]
				c = todays_close[idx]
				if np.isfinite(o) and np.isfinite(c) and o > 0.0 and c > 0.0:
					sp500_value *= (c / o)
				# else hold cash (no change)

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
							cr = X_flat[d, feat_idx]
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

			if len(top_ks) == 1:
				only_k = top_ks[0]
				row = [
					str(feature_date_id if feature_date_id is not None else ""),
					str(open_date_id),
					str(close_date_id),
					f"{portfolio_values_by_k[only_k]:.6f}",
				]
				if sp500_value is not None:
					row.append(f"{sp500_value:.6f}")
				if debug_sim_output:
					row += debug_cols
				f.write(",".join(row) + "\n")
			else:
				row_vals = [
					str(feature_date_id if feature_date_id is not None else ""),
					str(open_date_id),
					str(close_date_id),
				]
				if sp500_value is not None:
					row_vals.append(f"{sp500_value:.6f}")
				row_vals += [f"{portfolio_values_by_k[k]:.6f}" for k in top_ks]
				if debug_sim_output:
					row_vals += debug_cols
				f.write(",".join(row_vals) + "\n")

	return out_path


def parse_args(argv: Optional[List[str]] = None):
	p = argparse.ArgumentParser(description="Simulate daily trading using a trained model.")
	p.add_argument("model", help="Path to model directory or model_manifest.json")
	p.add_argument("--percent", type=float, default=10.0, help="Percentage of dates to simulate (use last N percent)")
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

	#p.add_argument("--close-ratio-only", action="store_true",default=True, help="Use only close_ratio_to_prior features if available")
	p.add_argument("--sp500-ticker-id", type=int, default=None, help="Ticker id to use as S&P 500 benchmark")
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
	return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
	args = parse_args(argv)
	print(f"Starting simulation with args: {args}")

	# Parse --top-k which may be a single int or comma-separated list
	top_k_arg = args.top_k
	if isinstance(top_k_arg, str):
		parts = [p.strip() for p in top_k_arg.split(",") if p.strip()]
		try:
			if len(parts) <= 1:
				top_k_parsed: Union[int, List[int]] = int(parts[0]) if parts else 10
			else:
				top_k_parsed = [int(p) for p in parts]
		except Exception:
			top_k_parsed = 10
	else:
		# Already an int (e.g., when called programmatically)
		top_k_parsed = int(top_k_arg)

	model_arg = args.model

	def _dir_has_model(run_dir: str) -> bool:
		"""Return True if the directory appears to contain a model manifest.

		We reuse StockRecommenderFromModel._resolve_manifest_path to locate a
		candidate manifest and then check that it exists on disk. This does not
		instantiate any models or require PyTorch.
		"""
		try:
			manifest_path = StockRecommenderFromModel._resolve_manifest_path(run_dir)  # type: ignore[attr-defined]
		except Exception:
			return False
		return os.path.isfile(manifest_path)

	def _run_for_path(path: str, visited: Optional[Set[str]] = None) -> None:
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
				entries = sorted(os.listdir(path))
				rep_dirs = [
					os.path.join(path, name)
					for name in entries
					if name.lower().startswith("rep") and os.path.isdir(os.path.join(path, name))
				]

				# Always run individual rep simulations for any detected rep* dirs
				if rep_dirs:
					print(f"Detected {len(rep_dirs)} rep* directories under {path}; running simulation separately for each rep.")
					last_out: Optional[str] = None
					for d in rep_dirs:
						print(f"Running simulation for {d}...")
						last_out = simulate(
							model_path=d,
							percent=args.percent,
							top_k=top_k_parsed,
							host=args.host,
							database=args.database,
							user=args.user,
							password=args.password,
							port=args.port,
							driver=args.driver,
							limit=args.limit,
							#close_ratio_only=args.close_ratio_only,
							sp500_ticker_id=args.sp500_ticker_id,
							prefer_prior_only=bool(args.prior_only),
							debug_sim_output=bool(args.debug_sim_output),
							device=str(args.device),
						)
					if last_out is not None:
						print(f"Simulation complete for reps under {path}. Last results written to: {last_out}")

					# If ensemble logic is requested and we have multiple reps, run an
					# additional ensemble simulation over all rep* runs.
					if args.ensemble_logic and len(rep_dirs) > 1:
						print(
							f"Detected {len(rep_dirs)} rep* directories under {path}; "
							"running additional ensemble simulation with averaged scores "
							"after individual rep simulations."
						)
						ensemble_recs: List[StockRecommenderFromModel] = [
							StockRecommenderFromModel(d, device=args.device) for d in rep_dirs
						]
						out_path = simulate(
							model_path=path,
							percent=args.percent,
							top_k=top_k_parsed,
							host=args.host,
							database=args.database,
							user=args.user,
							password=args.password,
							port=args.port,
							driver=args.driver,
							limit=args.limit,
							#close_ratio_only=args.close_ratio_only,
							sp500_ticker_id=args.sp500_ticker_id,
							prefer_prior_only=bool(args.prior_only),
							debug_sim_output=bool(args.debug_sim_output),
							device=str(args.device),
							ensemble_logic=True,
							ensemble_recs=ensemble_recs,
						)
						print(f"Ensemble simulation complete. Results written to: {out_path}")
					return

				# No rep* directories. If this directory itself contains a model
				# manifest, behave like the original single-model path.
				if _dir_has_model(path):
					out_path = simulate(
						model_path=path,
						percent=args.percent,
						top_k=top_k_parsed,
						host=args.host,
						database=args.database,
						user=args.user,
						password=args.password,
						port=args.port,
						driver=args.driver,
						limit=args.limit,
						#close_ratio_only=args.close_ratio_only,
						sp500_ticker_id=args.sp500_ticker_id,
						prefer_prior_only=bool(args.prior_only),
						debug_sim_output=bool(args.debug_sim_output),
						device=str(args.device),
					)
					print(f"Simulation complete. Results written to: {out_path}")
					return

				# Directory without rep* subdirectories and without a model manifest:
				# descend into its subdirectories and repeat the same checks.
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
					_run_for_path(sub, visited)
			else:
				# Non-directory path: single-model behaviour.
				out_path = simulate(
					model_path=path,
					percent=args.percent,
					top_k=top_k_parsed,
					host=args.host,
					database=args.database,
					user=args.user,
					password=args.password,
					port=args.port,
					driver=args.driver,
					limit=args.limit,
					#close_ratio_only=args.close_ratio_only,
					sp500_ticker_id=args.sp500_ticker_id,
					prefer_prior_only=bool(args.prior_only),
					debug_sim_output=bool(args.debug_sim_output),
					device=str(args.device),
				)
				print(f"Simulation complete. Results written to: {out_path}")

		finally:
			elapsed = time.perf_counter() - start_time
			print(f"Completed simulations for {path} in {elapsed:.2f} seconds")
	# Start processing from the user-provided model argument
	_run_for_path(model_arg)


if __name__ == "__main__":
	main()
