"""Legacy recommendation frontend using the recoverable market-data updater."""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from stockguidance.database.dates import normalize_session_date as _normalize_db_transaction_date
from stockguidance.cli.update_arguments import add_update_policy_arguments
from stockguidance.cli.update_setup import assemble_and_update
from stockguidance.workflows.failures import StageExecutionError

import numpy as np

from StockRecommenderFromModel import StockRecommenderFromModel
from database.db_connect import get_connection
from feature_vector_builder import FeatureVectorBuilder
from trainer.state.single_run import SingleRun
from load_latest_raw_stock_data_date import (
    get_latest_raw_stock_data,
)
from train_pytorch_network import TrainPytorchNetwork



def _get_recent_date_id_window_from_daily_delta_stock_data(
    max_days: int,
    *,
    dbinfo_file: str = "dbinfo.id",
) -> Optional[tuple[int, int]]:
	"""Return an inclusive (start_id, end_id) window for the most recent days.

	The window covers up to ``max_days`` distinct ``date_id`` values present in
	``daily_delta_stock_data``. If fewer than ``max_days`` exist the full range
	is returned. If no rows exist, returns ``None``.
	"""

	if max_days <= 0:
		return None

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(
			"""
			SELECT DISTINCT date_id
			FROM daily_delta_stock_data
			ORDER BY date_id DESC
			LIMIT %s
			""",
			(max_days,),
		)
		rows = cur.fetchall()
		if not rows:
			return None
		date_ids = [int(r[0]) for r in rows]
		end_id = max(date_ids)
		start_id = min(date_ids)
		return start_id, end_id
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _lookup_transaction_date_for_date_id(
    date_id: int,
    *,
    dbinfo_file: str = "dbinfo.id",
) -> Optional[date]:
	"""Return the ``transaction_date`` corresponding to a given ``date_id``.

	Looks up the value in ``transaction_dates`` and normalises it to
	:class:`datetime.date`. Returns ``None`` if no matching row is found.
	"""

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(
			"SELECT transaction_date FROM transaction_dates WHERE id = %s",
			(date_id,),
		)
		row = cur.fetchone()
		if not row:
			return None
		return _normalize_db_transaction_date(row[0])
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _lookup_date_id_for_transaction_date(
    target_date: date,
    *,
    dbinfo_file: str = "dbinfo.id",
) -> Optional[int]:
	"""Return the ``date_id`` corresponding to a given ``transaction_date``.

	Returns ``None`` if no matching row is found.
	"""

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		cur.execute(
			"SELECT id FROM transaction_dates WHERE transaction_date = %s",
			(target_date,),
		)
		row = cur.fetchone()
		if not row:
			return None
		return int(row[0])
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _load_ticker_symbols(
    ticker_ids: list[int],
    *,
    dbinfo_file: str = "dbinfo.id",
) -> dict[int, str]:
	"""Load ``ticker_symbol`` strings for the given numeric ids.

	Returns a mapping ``{ticker_id: ticker_symbol}``. Missing ids are simply
	omitted from the mapping.
	"""

	if not ticker_ids:
		return {}

	conn = get_connection(dbinfo_file=dbinfo_file)
	try:
		cur = conn.cursor()
		placeholders = ",".join(["%s"] * len(ticker_ids))
		sql = f"SELECT id, label FROM ticker_symbol WHERE id IN ({placeholders})"
		cur.execute(sql, ticker_ids)
		mapping: dict[int, str] = {}
		for tid, sym in cur.fetchall():
			mapping[int(tid)] = str(sym)
		return mapping
	finally:
		try:
			conn.close()
		except Exception:
			pass


def _score_for_ticker_id(scores: np.ndarray, ticker_id: int, rec: StockRecommenderFromModel) -> float:
	"""Return the model score corresponding to ``ticker_id``.

	Handles both the dense-id case (no explicit ticker_id_map) and the case
	where the recommender uses a sidecar ``ticker_id_map``.
	"""

	idx: int
	try:
		# Use public ticker_ids if available; then try private _ticker_id_map (legacy/internal)
		ticker_map = getattr(rec, "ticker_ids", None)
		if ticker_map is None:
			ticker_map = getattr(rec, "_ticker_id_map", None)

		output_size = int(getattr(rec, "_output_size", len(scores)))
		if ticker_map and len(ticker_map) == output_size:
			try:
				idx = ticker_map.index(ticker_id)  # type: ignore[attr-defined]
			except ValueError:
				return float("nan")
		else:
			idx = ticker_id - 1
		if idx < 0 or idx >= len(scores):
			return float("nan")
		return float(scores[idx])
	except Exception:
		return float("nan")




def _build_arg_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description=(
			"Reconcile declared Polygon history and corrections before legacy recommendations."
		)
	)
	parser.add_argument(
		"--dbinfo-file",
		default="dbinfo.id",
		help="Path to DB info file (default: dbinfo.id)",
	)
	parser.add_argument(
		"--api-key",
		default="y41KT5lnM51gLWEx_TbQ21DeQKxOabXk",  # os.getenv("POLYGON_API_KEY", DEFAULT_API_KEY),
		help=(
			"Polygon.io API key. Defaults to POLYGON_API_KEY env or the "
			"DEFAULT_API_KEY from polygon_fetch."
		),
	)
	parser.add_argument(
		"--data-dir",
		default="data",
		help="Root directory for request-scoped history, source responses, and manifests.",
	)
	parser.add_argument(
		"--start-date",
		default=None,
		help=(
			"Optional inclusive start date; defaults to the persisted supported-history boundary."
		),
	)
	parser.add_argument(
		"--end-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD), inclusive. If omitted, uses "
			"today (UTC)."
		),
	)
	parser.add_argument(
		"--max-retries",
		type=int,
		default=5,
		help="Max retries for HTTP 429/5xx responses when calling Polygon.",
	)
	parser.add_argument(
		"--ensemble",
		type=int,
		default=1,
		help=(
			"Number of models to train and use as an ensemble. "
			"Values <= 1 result in a single-model run."
		),
	)
	parser.add_argument(
		"--ensemble-normalize",
		action="store_true",
		help=(
			"If set, normalize each ensemble member's output so that its "
			"scores sum to 1. The normalized outputs are then summed "
			"across all models to form the ensemble scores."
		),
	)
	parser.add_argument(
		"--pretrained-dir",
		default=None,
		help=(
			"If provided, use an existing model from this directory "
			"instead of training a new one. The execution flow remains "
			"the same, using this model for recommendations."
		),
	)
	parser.add_argument(
		"--target-date",
		default=None,
		help=(
			"Optional ISO date (YYYY-MM-DD). If provided, this date will be "
			"used as the 'current' date for recommendations and data "
			"processing. If omitted, uses the latest date in the database."
		),
	)
	parser.add_argument(
		"--log-features",
		action="store_true",
		help="If set, log the full feature vector to a CSV file and show the feature window table.",
	)
	parser.add_argument(
		"--random-dates",
		default=None,
		help="If set, format 'x,y'. Pick x random dates from 2015-01-01 to 2025-12-31, get recommendations, and print values at y random output locations.",
	)
	add_update_policy_arguments(parser)
	return parser


def find_single_run_json(base_dir: str | Path) -> Path:
	"""Locate the SingleRun state JSON file in the given directory or its subdirectories.

	Searches for 'single_run_*.json'. If multiple matches are found, raises RuntimeError.
	If none are found, raises FileNotFoundError.
	"""
	import glob
	base_path = Path(base_dir)
	# Pattern for wildcard search
	pattern = "single_run_*.json"

	# Look in the base directory
	matches = list(base_path.glob(pattern))

	# If none found in base directory, look in subdirectories (e.g., rep subdirs)
	if not matches:
		matches = list(base_path.glob(f"*/{pattern}"))

	if not matches:
		# Special case: check for single_run_epoch.json specifically if it doesn't match the wildcard
		# (though wildcard should cover it)
		epoch_matches = list(base_path.glob("single_run_epoch.json"))
		if not epoch_matches:
			epoch_matches = list(base_path.glob("*/single_run_epoch.json"))
		matches = epoch_matches

	if not matches:
		raise FileNotFoundError(f"Could not find any single_run_*.json in {base_dir} or its subdirectories")

	if len(matches) > 1:
		raise RuntimeError(
			f"Multiple SingleRun state files found in {base_dir}: "
			+ ", ".join(m.name for m in matches)
		)

	return matches[0]


def main(argv: Optional[List[str]] = None) -> int:
	"""Entry point for the end-to-end "choose next day stocks" pipeline.

	When invoked as a script, ``argv`` is left as ``None`` so that
	:func:`argparse.ArgumentParser.parse_args` consumes ``sys.argv[1:]`` as
	usual.  When called programmatically (for example from ``batch_run``), a
	list of CLI-style argument tokens can be passed in explicitly.
	"""
	parser = _build_arg_parser()
	args = parser.parse_args(argv)

	target_date_obj = None
	target_date_id = None
	if args.target_date:
		try:
			target_date_obj = date.fromisoformat(args.target_date)
		except ValueError:
			print(f"Error: Invalid --target-date format '{args.target_date}'. Use YYYY-MM-DD.")
			return 1

	args.data_root = args.data_dir
	if target_date_obj is not None:
		requested_end = date.fromisoformat(args.end_date) if args.end_date else target_date_obj
		args.end_date = min(requested_end, target_date_obj).isoformat()
	try:
		update_result = assemble_and_update(args)
	except StageExecutionError as error:
		print(str(error))
		return 1
	except Exception:
		print("Required market-data update setup failed.")
		return 1
	if not update_result.successful:
		print("Required market-data coverage is incomplete or unverified.")
		return 1
	latest_after = get_latest_raw_stock_data(dbinfo_file=args.dbinfo_file)
	if latest_after is None:
		print("No tradable raw data is available after reconciliation.")
		return 1
	latest_after_date_id, _ = latest_after
	if target_date_obj is not None:
		target_date_id = _lookup_date_id_for_transaction_date(target_date_obj, dbinfo_file=args.dbinfo_file)
		if target_date_id is None:
			print("Target session is unavailable after ingestion.")
			return 1
		latest_after_date_id = target_date_id

	# Default hyperparameters
	prior_days = 10
	hidden_layers = "500"
	mlp_layers = 2
	activation = "leaky_relu"
	final_activation = "tanh"
	day_change_centered = "proportional_normalized_zero_centered"
	precision = "fp32"
	learning_rate = "0.01"
	layer_types = "linear"
	learning_rate_adjustment_parameter = "1"
	batch_size = 256
	epochs = 1600
	layer_normalization = False
	period_to_train_on = 5080
	optimizer = "adamw"

	pretrained_dir = args.pretrained_dir
	run_dir = None
	single_run = None

	if pretrained_dir:
		run_dir = Path(pretrained_dir)
		if not run_dir.exists():
			print(f"Error: Pretrained directory {pretrained_dir} does not exist.")
			return 1
		print(f"Using pretrained model from: {run_dir}")
		sr_path = find_single_run_json(run_dir)
		single_run = SingleRun.load(str(sr_path))
		# Use hyperparameters from the pretrained model
		prior_days = single_run.prior_days
		mlp_layers = single_run.mlp_layers
		activation = single_run.activation
		final_activation = single_run.final_activation
		day_change_centered = single_run.day_change_centered
		learning_rate = str(single_run.lr)
		learning_rate_adjustment_parameter = str(single_run.learning_rate_adjustment_parameter)
		optimizer = single_run.optimizer

		# If the pretrained model has training dates, use them to ensure the ticker pool is identical
		train_start_id = single_run.train_validation_test_sets_start_date_id
		train_end_id = single_run.train_validation_test_sets_end_date_id

		# period_to_train_on is not directly in SingleRun, but we can estimate it or just use the default
		# since it's only used for determining the training window which is skipped if using pretrained.

	# ------------------------------------------------------------------
	# 4. Train a PyTorch network on the most recent 1000 days
	# ------------------------------------------------------------------
	# Hyperparameters for this automated training run.

	if not pretrained_dir:
		print(f"Preparing to train PyTorch model on most recent {period_to_train_on} days of data...")
		window = _get_recent_date_id_window_from_daily_delta_stock_data(
			period_to_train_on,
			dbinfo_file=args.dbinfo_file,
		)
		if window is None:
			print("No date_id window available in daily_delta_stock_data; skipping training.")
			return 0

		# The most recent trading day does not yet have a usable "next day" label,
		# so restrict the effective training window to end at the second to last date.
		conn = get_connection(dbinfo_file=args.dbinfo_file)
		try:
			cur = conn.cursor()
			if target_date_id is not None:
				# If target_date is specified, the training window must end at or before target_date_id - 1
				# (because target_date_id doesn't have a label yet relative to itself, or rather,
				# target_date_id - 1 is the last day that HAS a label if target_date_id is our "latest")
				cur.execute(
					"SELECT DISTINCT date_id FROM daily_delta_stock_data WHERE date_id <= %s ORDER BY date_id DESC LIMIT 2",
					(target_date_id,)
				)
			else:
				cur.execute("SELECT DISTINCT date_id FROM daily_delta_stock_data ORDER BY date_id DESC LIMIT 2")
			rows = cur.fetchall()
			if len(rows) < 2:
				print("Not enough labeled days available to train a model; skipping training.")
				return 0
			train_end_id = int(rows[1][0])
			# The window from _get_recent_date_id_window_from_daily_delta_stock_data(1001)
			# might have a start_id that we want to keep, but we need to ensure it's
			# <= train_end_id.
			train_start_id = window[0]
			if train_start_id > train_end_id:
				train_start_id = train_end_id # fallback
		finally:
			conn.close()
	else:
		print(f"Skipping training; using pretrained model. Input features based on model's prior_days={prior_days}...")
		# Validation for pretrained model training dates
		if train_start_id is None or train_end_id is None:
			print("Warning: Pretrained model does not have explicit training dates. Falling back to current window for ticker pool.")
			window = _get_recent_date_id_window_from_daily_delta_stock_data(
				period_to_train_on,
				dbinfo_file=args.dbinfo_file,
			)
			if window is None:
				print("No date_id window available in daily_delta_stock_data; skipping training.")
				return 0
			conn = get_connection(dbinfo_file=args.dbinfo_file)
			try:
				cur = conn.cursor()
				if target_date_id is not None:
					cur.execute(
						"SELECT DISTINCT date_id FROM daily_delta_stock_data WHERE date_id <= %s ORDER BY date_id DESC LIMIT 2",
						(target_date_id,)
					)
				else:
					cur.execute("SELECT DISTINCT date_id FROM daily_delta_stock_data ORDER BY date_id DESC LIMIT 2")
				rows = cur.fetchall()
				if len(rows) >= 2:
					train_end_id = int(rows[1][0])
				else:
					train_end_id = window[1]
				train_start_id = window[0]
				if train_start_id > train_end_id:
					train_start_id = train_end_id
			finally:
				conn.close()

	print(
		"Reference window (date_id) for model (up to second most recent day): "
		f"{train_start_id} through {train_end_id} (inclusive).",
	)

	if not pretrained_dir:
		train_argv = [
			f"--prior-days={prior_days}",
			f"--epochs={epochs}",
			f"--batch-size={batch_size}",
			f"--lr={learning_rate}",
			f"--learning-rate-adjustment-parameter={learning_rate_adjustment_parameter}",
			"--train-percent=1.0",
			"--val-percent=0.0",
			f"--hidden-layer-size-list={hidden_layers}",
			f"--mlp-layers={mlp_layers}",
			f"--activation={activation}",
			f"--final-activation={final_activation}",
			f"--layer-types={layer_types}",
			f"--day-change-centered={day_change_centered}",
			f"--precision={precision}",
			f"--raw-train-start-with-special-characters={train_start_id}",
			f"--raw-train-end-with-special-characters={train_end_id}",
			f"--optimizer={optimizer}",
			"--split-mode=sequential",
			f"--layer-normalization={layer_normalization}",
			"--shuffle"
		]
		print(day_change_centered)

		# If an ensemble size > 1 was requested, pass this through as run repeats so
		# that TrainPytorchNetwork creates rep1/rep2/... subdirectories that can be
		# loaded as an ensemble later.
		ensemble_size = int(getattr(args, "ensemble", 1) or 1)
		if ensemble_size > 1:
			train_argv.append(f"--run-repeats={ensemble_size}")
		print("Starting training with arguments:", " ".join(train_argv))
		trainer = TrainPytorchNetwork(train_argv)
		exit_code = trainer.run()
		if exit_code != 0:
			print(f"Training failed with exit code {exit_code}; aborting recommendations.")
			return int(exit_code)

		run_dir = trainer.training_directory
		if not run_dir:
			print("Training did not report a run directory; cannot load model for recommendations.")
			return 0

		print(f"Training complete. Using run directory for recommendations: {run_dir}")
	else:
		print(f"Using pretrained model from: {run_dir}")

	# ------------------------------------------------------------------
	# 5. Build feature vector for the latest date and get top-5 stocks
	# ------------------------------------------------------------------
	print("Fetching grouped features from database for recommendation step...")

	# Load SingleRun if not already loaded
	if single_run is None:
		sr_path = find_single_run_json(run_dir)
		single_run = SingleRun.load(str(sr_path))
	print("about to make stock from model ",single_run.day_change_centered)
	rec = StockRecommenderFromModel(
		single_run,
		str(run_dir),
		device="auto",
		ensemble_normalize=bool(getattr(args, "ensemble_normalize", False)),
	)

	# For inference, we need to use the exact same ticker_ids that the model
	# was trained on.
	#
	# The model was trained with ticker_ids present in both train_start_id
	# and train_end_id.

	# Build feature vector for the latest date and get top-5 stocks
	builder = FeatureVectorBuilder.from_db(
		rec,
		dbinfo_file=args.dbinfo_file,
		start_date_id=train_start_id,
		end_date_id=latest_after_date_id,
		ticker_pool_start_date_id=train_start_id,
		ticker_pool_end_date_id=train_end_id,
	)

	if args.random_dates:
		try:
			x_str, y_str = args.random_dates.split(",")
			num_random_dates = int(x_str)
			num_random_outputs = int(y_str)
		except ValueError:
			print(f"Error: Invalid --random-dates format '{args.random_dates}'. Use 'x,y'.")
			return 1

		# Define the range 1-1-2015 to 31-12-2025
		start_range = date(2015, 1, 1)
		end_range = date(2025, 12, 31)

		# Get all date_ids within this range that are also in builder.dates
		valid_date_ids = []
		conn = get_connection(dbinfo_file=args.dbinfo_file)
		try:
			cur = conn.cursor()
			cur.execute(
				"SELECT id FROM transaction_dates WHERE transaction_date >= %s AND transaction_date <= %s",
				(start_range, end_range)
			)
			rows = cur.fetchall()
			db_date_ids = {int(r[0]) for r in rows}
			# Intersect with dates available in the builder (which has features)
			builder_dates = set(builder.dates.tolist())
			valid_date_ids = sorted(list(db_date_ids.intersection(builder_dates)))
		finally:
			conn.close()

		if not valid_date_ids:
			print(f"Error: No valid dates found between {start_range} and {end_range} with available features.")
			return 1

		import random
		selected_date_ids = random.sample(valid_date_ids, min(num_random_dates, len(valid_date_ids)))
		print(f"Selected {len(selected_date_ids)} random dates for evaluation.")

		# Determine y random output locations once for all dates
		y_indices = []
		# We need at least one model run to know the number of outputs, or we can use rec.output_size
		num_outputs = int(rec.output_size)
		if num_outputs > 0:
			y_indices = random.sample(range(num_outputs), min(num_random_outputs, num_outputs))
			print(f"Randomly selected {len(y_indices)} output indices to monitor for all dates: {y_indices}")

		# Select 2*y random feature vector locations once for all dates
		num_features_total = int(rec.input_size)
		num_random_features = 2 * num_random_outputs
		f_indices = []
		if num_features_total > 0:
			f_indices = random.sample(range(num_features_total), min(num_random_features, num_features_total))
			print(f"Randomly selected {len(f_indices)} feature indices to monitor for all dates: {f_indices}")

		# Load all ticker symbols for the model pool once
		ticker_ids_pool = rec.ticker_ids if rec.ticker_ids is not None else []
		ticker_symbols_pool = _load_ticker_symbols(ticker_ids_pool, dbinfo_file=args.dbinfo_file)

		for d_id in selected_date_ids:
			tx_date = _lookup_transaction_date_for_date_id(d_id, dbinfo_file=args.dbinfo_file)
			print(f"\n--- Recommendations for date_id {d_id}, date {tx_date} ---")

			try:
				f_vec, _, labels = builder.build_for_date(
					date_id=d_id,
					expected_input_size=int(rec.input_size),
					prefer_prior_only=False
				)

				# Print feature/label table for random dates as well
				num_features = rec.get_num_features_per_ticker()
				ticker_ids = builder.model_ticker_ids if builder.model_ticker_ids is not None else builder.db_ticker_ids
				if ticker_ids is not None:
					num_tickers = len(ticker_ids)
					num_days = int(f_vec.size // (num_tickers * num_features))
					matches = np.where(builder.dates == d_id)[0]
					if matches.size > 0:
						base_idx = int(matches[0])
						feature_names = ["change_in_price_over_day"]
						if rec.get_current_day_change_used():
							feature_names.append("change_in_price_over_day_current")
						if rec.get_volume_ratio_used():
							feature_names.append("volume_ratio")
						if rec.get_vwap_ratio_used():
							feature_names.append("vwap_ratio")
						if rec.get_tranaction_ratio_used():
							feature_names.append("transaction_ratio")
						if len(feature_names) < num_features:
							for i in range(len(feature_names), num_features):
								feature_names.append(f"F{i}")
						elif len(feature_names) > num_features:
							feature_names = feature_names[:num_features]

						show_ticker_ids = ticker_ids
						if num_tickers > 10:
							show_ticker_ids = ticker_ids[:10]

						from debug_feature_logging import dump_feature_table, dump_label_table, dump_output_table
						dump_feature_table(
							X_flat=builder.X_flat,
							dates=builder.dates,
							d=base_idx,
							num_prior=num_days - 1,
							ticker_ids=show_ticker_ids,
							feature_names=feature_names,
							header_note=f"Random Date {tx_date}"
						)
						if builder.Y_labels is not None:
							dump_label_table(
								labels=builder.Y_labels,
								dates=builder.dates,
								d=base_idx,
								num_prior=num_days - 1,
								ticker_ids=show_ticker_ids,
								header_note=f"Random Date {tx_date}"
							)
			except Exception as e:
				print(f"Failed to build feature vector for date_id {d_id}: {e}")
				continue

			scores, top_ids = rec.getBuyRecommendations(
				f_vec,
				top_k=5,
				index_base=1,
				current_date_id=d_id,
				dbinfo_file=args.dbinfo_file,
			)

			dump_output_table(
				outputs=scores,
				date_id=d_id,
				ticker_ids=show_ticker_ids,
				header_note=f"Random Date {tx_date}"
			)

			top_ticker_ids = [int(t) for t in top_ids]
			ticker_map = _load_ticker_symbols(top_ticker_ids, dbinfo_file=args.dbinfo_file)

			for rank, tid in enumerate(top_ids, start=1):
				tid_int = int(tid)
				ticker = ticker_map.get(tid_int, f"ticker_id={tid_int}")
				score = _score_for_ticker_id(scores, tid_int, rec)
				print(f"#{rank}: {ticker} (score={score:.6f})")

			# Print values at the pre-selected y random outputs
			if y_indices:
				print(f"Values at {len(y_indices)} selected output locations:")
				for idx in y_indices:
					val = float(scores[idx])
					# Get ticker symbol for output location if possible
					ticker_info = ""
					if ticker_ids_pool and idx < len(ticker_ids_pool):
						t_id = ticker_ids_pool[idx]
						t_symbol = ticker_symbols_pool.get(t_id, f"ticker_id={t_id}")
						ticker_info = f" ({t_symbol})"
					print(f"  Index {idx}{ticker_info}: {val:.6f}")

				# Check variability of output labels at the same y locations
				if labels is not None:
					print(f"Labels at the same {len(y_indices)} output locations:")
					for idx in y_indices:
						# Ensure idx is within labels bounds (should be if y was sampled from range(num_outputs))
						if idx < len(labels):
							l_val = float(labels[idx])
							# Get ticker symbol for output location if possible
							ticker_info = ""
							if ticker_ids_pool and idx < len(ticker_ids_pool):
								t_id = ticker_ids_pool[idx]
								t_symbol = ticker_symbols_pool.get(t_id, f"ticker_id={t_id}")
								ticker_info = f" ({t_symbol})"
							print(f"  Index {idx}{ticker_info}: {l_val:.6f}")
						else:
							print(f"  Index {idx}: OUT OF BOUNDS for labels")

			# Print values at the pre-selected 2*y random features
			if f_indices:
				print(f"Values at {len(f_indices)} selected feature locations:")
				num_tickers = len(ticker_ids_pool)
				for idx in f_indices:
					f_val = float(f_vec[idx])
					# Calculate ticker, day offset, and feature index from flattened vector index
					ticker_info = ""
					if num_tickers > 0 and num_features > 0:
						day_offset = idx // (num_tickers * num_features)
						ticker_idx = (idx // num_features) % num_tickers
						feature_idx = idx % num_features

						if ticker_idx < len(ticker_ids_pool):
							t_id = ticker_ids_pool[ticker_idx]
							t_symbol = ticker_symbols_pool.get(t_id, f"ticker_id={t_id}")
							f_name = feature_names[feature_idx] if feature_idx < len(feature_names) else f"F{feature_idx}"
							ticker_info = f" ({t_symbol}, {f_name}, day -{day_offset})"

					print(f"  Index {idx}{ticker_info}: {f_val:.6f}")

		return 0

	dates = builder.dates
	# Use the most recent available feature date.
	last_idx = int(len(dates) - 1)
	feature_date_id = int(dates[last_idx])
	recent_tx_date = _lookup_transaction_date_for_date_id(
		feature_date_id,
		dbinfo_file=args.dbinfo_file,
	)
	if recent_tx_date is not None:
		print(
			"Building feature vector for latest data point: "
			f"date_id {feature_date_id}, transaction_date {recent_tx_date}...",
		)
	else:
		print(
			"Building feature vector for latest data point: "
			f"date_id {feature_date_id} (transaction_date lookup returned no row)...",
		)

	# Force inclusion of the latest (current) day in the feature window so
	# recommendations use the most up-to-date datapoint. If the model was
	# trained without including the current day, this will raise a clear size
	# mismatch error rather than silently dropping the latest day.
	feat_vec, feature_date_id, _labels = builder.build_for_date(
		date_id=latest_after_date_id,
		expected_input_size=int(rec.input_size),
		prefer_prior_only=False
	)

	print(f"DEBUG: Ticker pool size in builder: {len(builder.model_ticker_ids) if builder.model_ticker_ids is not None else 'None'}")
	print(f"DEBUG: DB Ticker ids size: {len(builder.db_ticker_ids) if hasattr(builder, 'db_ticker_ids') and builder.db_ticker_ids is not None else 'Unknown'}")
	print(f"DEBUG: Feature vector size: {feat_vec.size}")
	print(f"DEBUG: Expected input size: {rec.input_size}")

	# Write the collected recommendation output to a timestamped file in the
	# training run directory.
	timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

	if args.random_dates or getattr(args, "log_features", False):
		# Log each feature value alongside ticker_id and date_id
		# Feature vector layout: [d, d-1, ..., d-prior]
		# Each day: [ticker1_features, ticker2_features, ...]
		# Each ticker_features: [f1, f2, ..., fn]

		num_features = rec.get_num_features_per_ticker()
		ticker_ids = builder.model_ticker_ids if builder.model_ticker_ids is not None else builder.db_ticker_ids

		if ticker_ids is not None:
			num_tickers = len(ticker_ids)
			num_days = int(feat_vec.size // (num_tickers * num_features))

			# We need the actual date_ids for the window
			# date_idx was used in build_for_date. We can reconstruct the sequence of IDs.
			# Since builder.build_for_date used date_id=None (latest),
			# the window is dates[last_idx], dates[last_idx-1], ...

			# Find the index of feature_date_id in builder.dates
			matches = np.where(builder.dates == feature_date_id)[0]
			if matches.size > 0:
				base_idx = int(matches[0])
				window_date_ids = []
				for off in range(num_days):
					idx = base_idx - off
					if idx >= 0:
						window_date_ids.append(int(builder.dates[idx]))
					else:
						window_date_ids.append(-1) # Should not happen with valid window

				from debug_feature_logging import dump_feature_window_table
				# Reconstruct a per-date X_flat and Y_labels for dump_feature_window_table
				# However, builder.X_flat already has this data! We just need to find the window.
				# X_flat in builder is (D, T*F), dates in builder is (D,).

				feature_names = ["change_in_price_over_day"]
				if rec.get_current_day_change_used():
					feature_names.append("change_in_price_over_day_current")
				if rec.get_volume_ratio_used():
					feature_names.append("volume_ratio")
				if rec.get_vwap_ratio_used():
					feature_names.append("vwap_ratio")
				if rec.get_tranaction_ratio_used():
					feature_names.append("transaction_ratio")

				# Ensure feature_names matches F
				if len(feature_names) < num_features:
					for i in range(len(feature_names), num_features):
						feature_names.append(f"F{i}")
				elif len(feature_names) > num_features:
					feature_names = feature_names[:num_features]

				# Filter tickers to show only top-10 recommended in the table if too many tickers
				show_ticker_ids = ticker_ids
				if num_tickers > 10:
					show_ticker_ids = ticker_ids[:10]
					print(f"DEBUG: Showing table for top 10 of {num_tickers} tickers.")

				from debug_feature_logging import dump_feature_table, dump_label_table, dump_output_table
				dump_feature_table(
					X_flat=builder.X_flat,
					dates=builder.dates,
					d=base_idx,
					num_prior=num_days - 1,
					ticker_ids=show_ticker_ids,
					feature_names=feature_names,
					header_note="Recommendation Window"
				)
				if builder.Y_labels is not None:
					dump_label_table(
						labels=builder.Y_labels,
						dates=builder.dates,
						d=base_idx,
						num_prior=num_days - 1,
						ticker_ids=show_ticker_ids,
						header_note="Recommendation Window"
					)

				if getattr(args, "log_features", False):
					log_lines = []
					# feat_vec is flat. We iterate by day, then ticker, then feature
					current_flat_idx = 0
					for day_idx in range(num_days):
						d_id = window_date_ids[day_idx]
						for t_idx in range(num_tickers):
							t_id = ticker_ids[t_idx]
							for f_idx in range(num_features):
								val = float(feat_vec[current_flat_idx])
								# value,ticker_id,date_id
								log_lines.append(f"{val:.10f},{t_id},{d_id}")
								current_flat_idx += 1

					feature_log_file = Path(run_dir) / f"features-{timestamp}.csv"
					try:
						feature_log_file.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
						print(f"Feature vector logged to {feature_log_file}")
					except Exception as e:
						print(f"Failed to write feature log {feature_log_file}: {e}")
		else:
			print("Warning: Could not log features because ticker IDs are unknown.")

	# Request top-5 ticker recommendations from the trained model.
	scores, top_ids = rec.getBuyRecommendations(
		feat_vec,
		top_k=5,
		index_base=1,
		current_date_id=feature_date_id,
		dbinfo_file=args.dbinfo_file,
	)

	if args.random_dates or getattr(args, "log_features", False):
		if ticker_ids is not None:
			dump_output_table(
				outputs=scores,
				date_id=feature_date_id,
				ticker_ids=show_ticker_ids,
				header_note="Recommendation Window"
			)

	# Load human-readable ticker symbols for the recommended ids.
	top_ticker_ids = [int(t) for t in top_ids]
	ticker_map = _load_ticker_symbols(
		top_ticker_ids,
		dbinfo_file=args.dbinfo_file,
	)

	# Collect final recommendation output so we can both print it and save it
	# to a timestamped recommendations-<timestamp>.txt file.
	rec_output_lines: list[str] = []

	header_type = "freshly trained" if not pretrained_dir else "pretrained"
	header = f"Top 5 stock recommendations from {header_type} model for date_id {feature_date_id}:"
	if recent_tx_date is not None:
		header = header +f", transaction_date {recent_tx_date}:"


	print(header)
	rec_output_lines.append(header)

	for rank, tid in enumerate(top_ids, start=1):
		tid_int = int(tid)
		ticker = ticker_map.get(tid_int, f"ticker_id={tid_int}")
		score = _score_for_ticker_id(scores, tid_int, rec)
		line = f"#{rank}: {ticker} (score={score:.6f})"
		print(line)
		rec_output_lines.append(line)

	# Write the collected recommendation output to a timestamped file in the
	# training run directory.
	rec_file = Path(run_dir) / f"recommendations-{timestamp}.txt"
	try:
		rec_file.write_text("\n".join(rec_output_lines) + "\n", encoding="utf-8")
		print(f"Recommendations saved to {rec_file}")
	except Exception as e:  # pragma: no cover - defensive logging
		print(f"Failed to write recommendations file {rec_file}: {e}")

	return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
	raise SystemExit(main())
