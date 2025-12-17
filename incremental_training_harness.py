"""Incremental training + one‑day‑ahead simulation harness.

This module provides a small driver that repeatedly:

1. Trains a model via ``train_pytorch_network.main`` on a *window* of dates.
2. Runs a one‑day‑ahead simulation for the next day using ``simulate_trading``.
3. Appends the resulting portfolio values to an incremental CSV whose
   format matches the existing ``stock_simulation_*.csv`` files.

The intent is to support experiments where validation/test sets are not used
and a separate method is used to evaluate performance. To achieve this the
driver configures the training run such that **all** usable instances in the
selected window are assigned to the training split.

High‑level behaviour
--------------------

Given:

* ``training_days`` – number of consecutive instances used for training.
* ``simulation_days`` – number of one‑day‑ahead simulation steps to perform.
* ``mode`` – either "sequential" (windows slide towards the end) or
  "random" (windows are scattered randomly across the available history).

The driver first uses :class:`TrainingInstanceProvider` to determine how many
instances ``N`` are available for the configured feature/label set. It then
creates a series of (train_window_start, simulation_index) pairs either by
sliding from the tail or by sampling uniformly at random. For each pair it
invokes a *fresh* training run, followed by a one‑day simulation whose result
is appended to an incremental CSV.

Notes / limitations
-------------------

* This harness intentionally keeps its scope narrow and does **not** attempt
  to expose the full CLI surface of ``train_pytorch_network``. Instead it
  accepts a small set of commonly‑used options and passes them through.
* To avoid invasive changes to the existing training script, windowing is
  achieved by setting ``data_head_ignore_percent`` and
  ``data_tail_ignore_percent`` so the effective usable range corresponds to
  the desired window, with ``train_percent=1.0`` and ``val_percent=0.0``.
* For the simulation step we currently call ``simulate_trading.simulate``
  over its default range and then take the **latest** row from the resulting
  CSV as the incremental step. The portfolio values are rescaled so that the
  starting balance equals the previous step's balance, yielding a continuous
  equity curve in the incremental file while keeping the simulation logic
  unchanged.

This keeps changes to the existing codebase minimal while providing the
requested new behaviour.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from StockRecommenderFromModel import StockRecommenderFromModel
from trainer.util.argparse_utils import parse_int_list
from training_instance_provider import TrainingInstanceProvider


@dataclass
class WindowSpec:
	train_start: int  # 1‑based instance index (inclusive)
	train_end: int    # 1‑based instance index (inclusive)
	sim_index: int    # 1‑based instance index used for one‑day‑ahead sim


def _compute_windows_sequential(n: int, training_days: int, simulation_days: int) -> List[WindowSpec]:
	"""Compute sliding windows anchored near the end of the available range.

	We treat instances as a simple 1..N sequence. For a given training window
	length ``T`` and desired number of simulation steps ``S``, we create
	windows whose simulation indices form a contiguous tail segment.
	"""

	if training_days <= 0 or simulation_days <= 0:
		raise ValueError("training_days and simulation_days must be positive")

	total_needed = training_days + simulation_days
	if n < total_needed:
		# Not enough history to honour the full request; fall back to the
		# largest feasible number of simulation steps.
		simulation_days = max(1, n - training_days)

	windows: List[WindowSpec] = []

	# We choose the earliest training start such that the last simulation
	# index does not exceed ``n - 1``.  The final *instance* (index ``n``)
	# corresponds to the very last available date_id; simulating on that
	# date would require an "open" on a non-existent following day.  To
	# avoid requesting forecasts beyond the available date range we restrict
	# simulation indices to the range ``1 .. n-1``.
	first_sim_index = n - simulation_days
	first_train_start = max(1, first_sim_index - training_days)

	for offset in range(simulation_days):
		sim_idx = first_sim_index + offset
		train_end = sim_idx - 1
		train_start = max(1, train_end - training_days + 1)
		if train_start > train_end:
			continue
		windows.append(WindowSpec(train_start=train_start, train_end=train_end, sim_index=sim_idx))

	return windows


def _compute_windows_random(n: int, training_days: int, simulation_days: int, *, rng: random.Random) -> List[WindowSpec]:
	"""Compute randomly scattered windows.

	Each simulation step picks a random training window of length ``training_days``
	followed by a simulation index one step ahead of that window. Windows are
	independent; the same region may be reused across steps.
	"""

	if training_days <= 0 or simulation_days <= 0:
		raise ValueError("training_days and simulation_days must be positive")

	max_train_start = max(1, n - training_days)
	windows: List[WindowSpec] = []

	for _ in range(simulation_days):
		train_start = rng.randint(1, max_train_start)
		train_end = train_start + training_days - 1
		if train_end >= n:
			train_end = n - 1
		sim_idx = min(n, train_end + 1)
		windows.append(WindowSpec(train_start=train_start, train_end=train_end, sim_index=sim_idx))

	return windows


def _run_one_window(
	base_args: List[str],
	train_start_date_id: int,
	train_end_date_id: int,
	metrics_dir: str,
) -> str:
	"""Invoke ``train_pytorch_network`` for a single window.

	Returns the run directory where the model and metrics were written.

	This now uses the :class:`TrainPytorchNetwork` wrapper so that we can
	query the base run directory directly instead of inferring it from the
	metrics directory contents.
	"""

	from train_pytorch_network import TrainPytorchNetwork  # Imported lazily

	# Force a fresh metrics file path per window so that each run creates its
	# own timestamped subdirectory beneath ``run_group_dir``.
	metrics_file = os.path.join(metrics_dir, "training_metrics.csv")

	argv = list(base_args)
	argv.extend(
		[
			f"--metrics-file={metrics_file}",
			f"--train-start-date-id={int(train_start_date_id)}",
			f"--train-end-date-id={int(train_end_date_id)}",
			"--train-percent=1.0",
			"--val-percent=0.0",
			"--split-mode=sequential",
			"--log-interval=10",

		]
	)

	# Ensure the directory exists so training does not fail.
	os.makedirs(metrics_dir, exist_ok=True)

	start_time = time.perf_counter()
	runner = TrainPytorchNetwork(argv)
	exit_code = runner.run()
	elapsed = time.perf_counter() - start_time
	print(
		"train_main elapsed time: "
		f"{elapsed:.2f} seconds for window train_start_date_id={train_start_date_id}, "
		f"train_end_date_id={train_end_date_id} (exit_code={exit_code})"
	)
	if exit_code != 0:
		raise RuntimeError(
			f"Training failed for window train_start_date_id={train_start_date_id}, "
			f"train_end_date_id={train_end_date_id} with exit code {exit_code}"
		)

	run_dir = runner.training_directory
	if not run_dir:
		raise RuntimeError(
			"Training completed but no run directory was reported by TrainPytorchNetwork"
		)

	print(f"Selected run directory: {run_dir}")
	return run_dir



def _run_one_day_simulation(
	recommender: StockRecommenderFromModel,
	top_k: List[int],
	date_ids_to_handle: Optional[List[int]] = None,
) -> Tuple[int, int, int, List[float]]:
	"""Run a simulation for ``run_dir`` and return the latest portfolio row.

	The return value is ``(feature_date_id, open_date_id, close_date_id,
	[portfolio_value_k for k in top_k])`` where the last element contains
	one portfolio value **per** requested ``top_k`` entry, in the same order
	as the ``top_k`` argument.
	"""
	import simulate_trading as _sim

	# We rely on ``simulate_trading.simulate`` to write its stock_simulation
	# CSV into ``run_dir``. To keep this harness light‑weight we simulate over
	# a small percentage (1%) of the data and then take the last row.
	_sim.simulate(
		None,
		recommender,

#		percent=1.0,
		top_k=top_k,
		limit=None,
		prefer_prior_only=True,
		debug_sim_output=False,
		dates_to_handle=date_ids_to_handle,
	)
	out_path = os.path.join(recommender.output_directory)
	with open(out_path, "r", newline="") as f:
		reader = csv.reader(f)
		header = next(reader, None)
		if not header:
			raise RuntimeError(f"Empty simulation output: {out_path}")
		rows = list(reader)
	if not rows:
		raise RuntimeError(f"No rows in simulation output: {out_path}")

	last = rows[-1]
	feature_date_id = int(last[0]) if last[0] else 0
	open_date_id = int(last[1]) if last[1] else 0
	close_date_id = int(last[2]) if last[2] else 0

	# ``simulate_trading`` writes one column per requested top_k when more
	# than one value is provided (e.g. portfolio_top1, portfolio_top3, ...).
	# For backwards compatibility with the single-K case it may instead use
	# a lone "portfolio_value" column.  Here we always resolve a value per
	# requested K in order, raising if a column is unexpectedly missing.
	name_to_index = {name: idx for idx, name in enumerate(header)}
	portfolio_values: List[float] = []
	for k in top_k:
		col_name = f"portfolio_top{k}"
		if col_name in name_to_index:
			col_idx = name_to_index[col_name]
		elif len(top_k) == 1 and "portfolio_value" in name_to_index:
			# Legacy single-K layout from simulate_trading.
			col_idx = name_to_index["portfolio_value"]
		else:
			raise RuntimeError(
				f"Simulation output missing expected column '{col_name}' in {out_path} (header={header})"
			)
		try:
			portfolio_values.append(float(last[col_idx]))
		except (TypeError, ValueError, IndexError) as exc:
			raise RuntimeError(
				f"Invalid portfolio value for column '{col_name}' (index {col_idx}) in {out_path}: {last}"
			) from exc

	return feature_date_id, open_date_id, close_date_id, portfolio_values


def _instance_index_to_date_id(provider: TrainingInstanceProvider, instance_index: int) -> int:
	"""Map a 1-based instance index to its underlying date_id.

	This mirrors the mapping used by
	:py:meth:`TrainingInstanceProvider.get_feature_and_label_pair` so that we can
	log human-readable date identifiers for debugging purposes.
	"""

	# ``TrainingInstanceProvider`` defines instance k to correspond to the
	# absolute date index: num_prior + (k - 1).
	base_index = provider._num_prior + (instance_index - 1)  # type: ignore[attr-defined]
	return int(provider._dates[base_index])  # type: ignore[attr-defined]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(description="Incremental training and 1‑day‑ahead simulation harness")

	# Core incremental harness parameters (used in normal run mode).
	p.add_argument("--training-days", type=int, required=True, help="Number of consecutive days to use for training in each window")
	p.add_argument("--simulation-days", type=int, required=True, help="Number of incremental one‑day‑ahead simulations to perform")
	p.add_argument(
		"--window-mode",
		choices=["sequential", "random"],
		default="sequential",
		help=(
			"How to choose training windows: 'sequential' slides windows towards the "
			"end of the available dates, 'random' scatters windows randomly."
		),
	)
	p.add_argument("--top-k", type=str, default="1,3,5", help="Top‑K portfolio to track in the incremental CSV")
	p.add_argument("--metrics-dir", type=str, default="data", help="Base directory where training runs will be stored")
	p.add_argument("--output", type=str, default="stock_simulation_incremental.csv", help="Path to incremental CSV output")
	p.add_argument("--seed", type=int, default=1234, help="Random seed for window selection when --window-mode=random")
	# When enabled, print detailed per-window calculations during recalculation
	p.add_argument(
		"--verbose-recalc",
		action="store_true",
		help=(
			"When used together with --recalculate-from-run-dir, print detailed per-window "
			"portfolio calculations while rebuilding stock_simulation_incremental.csv."
		),
	)

	# Recalculation mode: recompute the incremental CSV for a *previous* harness
	# run using its existing per-window stock_simulation_*.csv files.  When this
	# argument is provided, no new training or simulation runs are executed – the
	# harness merely re-derives stock_simulation_incremental.csv.
	p.add_argument(
		"--recalculate-from-run-dir",
		type=str,
		default=None,
		help=(
			"Path to an existing incremental harness run directory whose per-window "
			"stock_simulation_*.csv files should be reused to recompute the final "
			"stock_simulation_incremental.csv without retraining or re-simulating."
		),
	)

	# Minimal subset of training options we pass through. Additional options
	# can be added here as needed; they are forwarded verbatim to
	# ``train_pytorch_network.main``.
	p.add_argument("--prior-days", type=int, default=4, help="Number of prior days for feature windows (training script argument)")
	p.add_argument("--mlp-layers", type=int, default=4, help="Hidden layers for MLP (training script argument)")
	p.add_argument("--lr", type=float, default=1e-7, help="Learning rate (training script argument)")
	p.add_argument("--epochs", type=int, default=50, help="Epochs per window (training script argument)")

	return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
	args = parse_args(argv)

	# ------------------------------------------------------------------
	# Optional recalculation mode
	# ------------------------------------------------------------------
	# If the user supplies --recalculate-from-run-dir, we skip all training
	# and simulation work and instead recompute the incremental CSV using the
	# existing per-window stock_simulation_*.csv files under that directory.
	previous_run_dir = getattr(args, "recalculate_from_run_dir", None)
	if previous_run_dir:
		prev_dir_abs = os.path.abspath(str(previous_run_dir))
		if not os.path.isdir(prev_dir_abs):
			raise RuntimeError(
				f"--recalculate-from-run-dir path does not exist or is not a directory: {prev_dir_abs}"
			)
		return _recalculate_from_previous_run(prev_dir_abs, args.output, args.top_k, verbose=args.verbose_recalc)

	# Discover total number of instances using the same provider logic as the
	# main training script.
	provider = TrainingInstanceProvider(use_torch=False)
	provider.set_how_many_days_in_past_to_use(int(args.prior_days))
	n = provider.get_maximum_number_of_test_train_instances()
	if n <= 1:
		raise RuntimeError("Not enough instances available for incremental training")

	if args.window_mode == "sequential":
		windows = _compute_windows_sequential(n, args.training_days, args.simulation_days)
	else:
		rng = random.Random(int(args.seed))
		windows = _compute_windows_random(n, args.training_days, args.simulation_days, rng=rng)

	if not windows:
		raise RuntimeError("No windows could be constructed with the given parameters")

	# ------------------------------------------------------------------
	# Establish a dedicated directory for this incremental run.
	#
	# All artifacts produced by this harness – the multiple
	# ``train_pytorch_network`` runs *and* the incremental CSV output –
	# are kept beneath this single directory, which itself lives under the
	# user‑configurable metrics directory (by default, ``data``).
	#
	# Layout (example):
	#
	#   data/
	#     incremental_run_20250101_120000/
	#       stock_simulation_incremental.csv
	#       2025‑01‑01_12‑00‑00/    # first train_pytorch_network run
	#       2025‑01‑01_12‑05‑00/    # second run, etc.
	#
	# This satisfies the requirement that the harness keeps *all* of its
	# work in a single directory under the data directory, while still
	# honouring custom --metrics-dir values.
	base_metrics_dir = os.path.abspath(str(args.metrics_dir))
	run_timestamp = time.strftime("incremental_run_%Y%m%d_%H%M%S")
	harness_run_dir = os.path.join(base_metrics_dir, run_timestamp)
	os.makedirs(harness_run_dir, exist_ok=True)
	print(f"[incremental_training] Using harness run directory: {harness_run_dir}")

	# Parse and validate the requested top‑K list **once** up front.  We
	# reuse the same list for all windows and also to define the incremental
	# CSV schema.
	top_ks = parse_int_list(args.top_k, "top_k")
	if not top_ks:
		raise RuntimeError("--top-k must specify at least one integer value")

	base_train_args = [
		"--mode=train",
		f"--prior-days={args.prior_days}",
		f"--mlp-layers={args.mlp_layers}",
		f"--lr={args.lr}",
		f"--epochs={args.epochs}",
	]

	# Prepare incremental output file inside the harness run directory.
	#
	# If the caller provided a custom output *file name* without a
	# directory component (the common case), we place that file directly
	# inside ``harness_run_dir``.  If they provided an absolute path, we
	# respect it as‑is so that advanced users can opt out of this
	# behaviour deliberately.
	if os.path.isabs(args.output):
		out_path = args.output
	else:
		out_filename = os.path.basename(args.output) or "stock_simulation_incremental.csv"
		out_path = os.path.join(harness_run_dir, out_filename)

	out_dir = os.path.dirname(out_path)
	if out_dir:
		os.makedirs(out_dir, exist_ok=True)

	incremental_balance = 1000.0

	with open(out_path, "w", newline="") as f_out:
		writer = csv.writer(f_out)
		# One column per requested K so the incremental CSV mirrors the
		# structure of individual simulation runs.
		portfolio_cols = [f"portfolio_top{k}" for k in top_ks]
		writer.writerow(["feature_date_id", "open_date_id", "close_date_id", *portfolio_cols])

		# Maintain an incremental balance curve **per** K so that the harness
		# output tracks the same portfolio variants as individual simulations.
		incremental_balances = {k: incremental_balance for k in top_ks}

		for idx, w in enumerate(windows, start=1):
			# Convert the training instance window to concrete date_id bounds so
			# that the underlying training run can use the new
			# train_start_date_id / train_end_date_id arguments instead of
			# fractional head/tail ignores.
			train_start_date_id = _instance_index_to_date_id(provider, w.train_start)
			train_end_date_id = _instance_index_to_date_id(provider, w.train_end)
			# Direct all training runs for this harness into the dedicated
			# ``harness_run_dir`` so their timestamped subdirectories and
			# metrics files are kept together with the harness output.
			run_dir = _run_one_window(
				base_train_args,
				train_start_date_id,
				train_end_date_id,
				metrics_dir=harness_run_dir,
			)

			# Debugging output: show which date is being forecasted and which dates
			# are used for training this window.
			forecast_date_id = _instance_index_to_date_id(provider, w.sim_index)
			training_date_ids = [
				_instance_index_to_date_id(provider, i)
				for i in range(w.train_start, w.train_end + 1)
			]
			print(
				f"[incremental_training] window {idx}: forecasting on date_id={forecast_date_id} "
				f"(sim_instance_index={w.sim_index})"
			)
			print(
				f"[incremental_training] window {idx}: training on date_ids={training_date_ids} "
				f"(train_instance_indices={w.train_start}..{w.train_end})"
			)
			# Constrain each simulation run to the specific forecast date for this
			# window. ``simulate_trading`` expects ``dates_to_handle`` to be an
			# ordered list of feature-date identifiers and iterates over all but
			# the final element so that it can trade on the following day.  To
			# obtain exactly one simulation row per internal run with
			# ``feature_date_id = forecast_date_id`` and
			# ``open_date_id = forecast_date_id + 1``, we pass a
			# two-element list: [forecast_date_id, forecast_date_id + 1].
			per_window_dates_to_handle = [int(forecast_date_id), int(forecast_date_id) + 1]
			feature_date_id, open_date_id, close_date_id, raw_portfolio_values = _run_one_day_simulation(
				run_dir,
				top_ks,
				date_ids_to_handle=per_window_dates_to_handle,
			)

			if len(raw_portfolio_values) != len(top_ks):
				raise RuntimeError(
					f"Expected {len(top_ks)} portfolio values from simulation, "
					f"got {len(raw_portfolio_values)} (top_ks={top_ks})"
				)

			# Rescale each simulated portfolio value so that a starting balance
			# of 1000 maps to the current incremental balance for that K.
			row_balances: List[str] = []
			for k, raw_value in zip(top_ks, raw_portfolio_values):
				current_balance = incremental_balances[k]
				scale = current_balance / 1000.0
				new_balance = raw_value * scale
				incremental_balances[k] = new_balance
				row_balances.append(f"{new_balance:.6f}")

			writer.writerow([feature_date_id, open_date_id, close_date_id, *row_balances])

	return 0


def _recalculate_from_previous_run(previous_run_dir: str, output: str, top_k_arg: str, *, verbose: bool = False) -> int:
	"""Recompute the incremental CSV for a previous harness run.

	This mode is intended for scenarios where the training runs and per-window
	simulations have already been performed, and only the *final* incremental
	CSV needs to be regenerated – for example, after changing how top-K
	portfolios are aggregated.

	The function:

	* Discovers child run directories under ``previous_run_dir`` that contain
	  ``stock_simulation_*.csv`` files.
	* Sorts them by directory name to preserve the original window order.
	* Reads the last row from each simulation CSV.
	* Extracts one portfolio value per requested top-K (either inferred from
	  the header or derived from ``top_k_arg`` for legacy single-K runs).
	* Rebuilds ``stock_simulation_incremental.csv`` using the same incremental
	  scaling logic as the main harness.
	"""

	# Discover per-window simulation CSVs under the previous run directory.
	run_entries: List[Tuple[str, str]] = []  # (run_dir_name, sim_csv_path)
	for name in sorted(os.listdir(previous_run_dir)):
		full = os.path.join(previous_run_dir, name)
		if not os.path.isdir(full):
			continue
		# Look for stock_simulation_*.csv files in this child directory.
		candidates = [
			fn
			for fn in os.listdir(full)
			if fn.startswith("stock_simulation_") and fn.endswith(".csv")
		]
		if not candidates:
			continue
		# If multiple candidates exist (e.g. ensemble and non-ensemble), pick the
		# lexicographically last one as a simple, stable heuristic.
		chosen = sorted(candidates)[-1]
		run_entries.append((name, os.path.join(full, chosen)))

	if not run_entries:
		raise RuntimeError(
			f"No child run directories with stock_simulation_*.csv files found under {previous_run_dir}"
		)

	# Ensure deterministic window order by sorting on the run directory name.
	run_entries.sort(key=lambda pair: pair[0])

	# Inspect the header of the first simulation CSV to determine which
	# portfolio columns are available.
	first_sim_path = run_entries[0][1]
	with open(first_sim_path, "r", newline="") as f_first:
		reader = csv.reader(f_first)
		header = next(reader, None)
		if not header:
			raise RuntimeError(f"Empty simulation output: {first_sim_path}")

	# Identify all portfolio_topK columns present.
	header_top_ks: List[int] = []
	for col in header:
		if col.startswith("portfolio_top"):
			try:
				k_val = int(col[len("portfolio_top") :])
			except ValueError:
				continue
			header_top_ks.append(k_val)
	header_top_ks = sorted(set(header_top_ks))

	cli_top_ks = parse_int_list(top_k_arg, "top_k") if top_k_arg else []

	if header_top_ks:
		# Normal multi-K case: trust the simulation headers as the source of
		# truth, and simply mirror those columns in the incremental file.
		top_ks = header_top_ks
	else:
		# Legacy single-K layout with a lone "portfolio_value" column.
		if "portfolio_value" not in header:
			raise RuntimeError(
				f"Simulation output {first_sim_path} is missing portfolio_topK and portfolio_value columns"
			)
		if cli_top_ks and len(cli_top_ks) == 1:
			# Preserve the user-specified K purely for naming the incremental
			# column; the underlying values all come from "portfolio_value".
			top_ks = cli_top_ks
		else:
			# Fall back to a synthetic K=1 label if nothing better is provided.
			top_ks = [1]

	if not top_ks:
		raise RuntimeError("Could not determine any top_k values from simulation headers or CLI")

	# Decide where to write the recomputed incremental CSV.
	if os.path.isabs(output):
		out_path = output
	else:
		out_filename = os.path.basename(output) or "stock_simulation_incremental.csv"
		out_path = os.path.join(previous_run_dir, out_filename)

	out_dir = os.path.dirname(out_path)
	if out_dir:
		os.makedirs(out_dir, exist_ok=True)

	# Mirror the main harness logic: start each portfolio with a 1000-unit
	# balance and rescale subsequent simulation values so that the equity
	# curves are continuous across windows.
	incremental_balance = 1000.0
	incremental_balances = {k: incremental_balance for k in top_ks}

	with open(out_path, "w", newline="") as f_out:
		writer = csv.writer(f_out)
		portfolio_cols = [f"portfolio_top{k}" for k in top_ks]
		writer.writerow(["feature_date_id", "open_date_id", "close_date_id", *portfolio_cols])

		for idx, (run_name, sim_path) in enumerate(run_entries, start=1):
			with open(sim_path, "r", newline="") as f_sim:
				reader = csv.reader(f_sim)
				header_local = next(reader, None)
				if not header_local:
					raise RuntimeError(f"Empty simulation output: {sim_path}")
				rows = list(reader)
				if not rows:
					raise RuntimeError(f"No rows in simulation output: {sim_path}")

				last = rows[-1]
				feature_date_id = int(last[0]) if last[0] else 0
				open_date_id = int(last[1]) if last[1] else 0
				close_date_id = int(last[2]) if last[2] else 0

				name_to_index = {name: idx for idx, name in enumerate(header_local)}
				portfolio_values: List[float] = []
				for k in top_ks:
					col_name = f"portfolio_top{k}"
					if col_name in name_to_index:
						col_idx = name_to_index[col_name]
					elif len(top_ks) == 1 and "portfolio_value" in name_to_index:
						# Legacy single-K layout from simulate_trading.
						col_idx = name_to_index["portfolio_value"]
					else:
						raise RuntimeError(
							f"Simulation output missing expected column '{col_name}' in {sim_path} (header={header_local})"
						)
					try:
						portfolio_values.append(float(last[col_idx]))
					except (TypeError, ValueError, IndexError) as exc:
						raise RuntimeError(
							f"Invalid portfolio value for column '{col_name}' (index {col_idx}) in {sim_path}: {last}"
						) from exc

				if len(portfolio_values) != len(top_ks):
					raise RuntimeError(
						f"Expected {len(top_ks)} portfolio values from simulation, got {len(portfolio_values)} "
						f"for run {run_name} (top_ks={top_ks})"
					)

				row_balances: List[str] = []
				if verbose:
					print(
						f"[recalc] window {idx} (run={run_name}): feature_date_id={feature_date_id}, "
						f"open_date_id={open_date_id}, close_date_id={close_date_id}"
					)
				for k, raw_value in zip(top_ks, portfolio_values):
					current_balance = incremental_balances[k]
					scale = current_balance / 1000.0
					new_balance = raw_value * scale
					incremental_balances[k] = new_balance
					row_balances.append(f"{new_balance:.6f}")
					if verbose:
						print(
							f"    K={k}: raw_portfolio={raw_value:.6f}, "
							f"prev_balance={current_balance:.6f}, scale={scale:.6f}, "
							f"new_balance={new_balance:.6f}"
						)

				writer.writerow([feature_date_id, open_date_id, close_date_id, *row_balances])

	print(
		f"[incremental_training] Recalculated incremental CSV with {len(run_entries)} windows "
		f"from previous run directory: {previous_run_dir}\n"
		f"[incremental_training] Output written to: {out_path}"
	)
	return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
	raise SystemExit(main(sys.argv[1:]))
