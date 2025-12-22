from __future__ import annotations

import argparse
import os
from typing import List, Optional


def build_parser() -> argparse.ArgumentParser:
	"""Build the ArgumentParser for ``train_pytorch_network``.

	This function centralises the CLI definition so that it can be reused by
	both the script entry point and programmatic callers.
	"""

	parser = argparse.ArgumentParser(
		description="Train a PyTorch dense network on stock instances.",
	)
	parser.add_argument(
		"--resume-directory",
		type=str,
		default=None,
		help=(
			"Path to a run-family directory to resume. When provided, other hyper-parameter "
			"arguments are ignored and the script resumes from stored state."
		),
	)
	parser.add_argument(
		"--mode",
		type=str,
		default="train",
		choices=["train", "memest"],
		help="Run mode: train or memest (memory estimate only)",
	)
	parser.add_argument(
		"--torch_interface-profiler",
		action="store_true",
		default=False,
		help=(
			"If set, enable torch_interface.profiler with CUDA memory profiling for a short window "
			"so you can inspect which ops allocate GPU memory. "
			"Produces traces viewable in TensorBoard."
		),
	)
	parser.add_argument(
		"--torch_interface-profiler-dir",
		type=str,
		default=os.path.join("data", "torch_profiler"),
		help=(
			"Output directory for torch_interface.profiler TensorBoard traces "
			"(per-run subdirectories will be created under this)."
		),
	)
	parser.add_argument(
		"--torch_interface-profiler-wait",
		type=int,
		default=1,
		help="Profiler schedule: number of initial steps to skip (wait phase).",
	)
	parser.add_argument(
		"--torch_interface-profiler-warmup",
		type=int,
		default=1,
		help="Profiler schedule: number of warmup steps before active profiling.",
	)
	parser.add_argument(
		"--torch_interface-profiler-active",
		type=int,
		default=10,
		help="Profiler schedule: number of active profiling steps.",
	)
	parser.add_argument(
		"--ensemble-interval",
		type=int,
		default=0,
		help=(
			"If > 0 and run-repeats > 1, run an ensemble simulate_trading pass across all "
			"repeats every N epochs. Works similarly to --trading-interval but operates "
			"on the ensemble of repeated runs."
		),
	)
	# Accept single int or inclusive range like "2-4" for multiple runs
	parser.add_argument(
		"--prior-days",
		type=str,
		default="4",
		help=(
			"How many prior days to include in the feature window (>=0) or a range "
			"like '2-4' to run multiple"
		),
	)
	parser.add_argument(
		"--epochs",
		type=int,
		default=80,
		help="Number of training epochs",
	)
	parser.add_argument(
		"--batch-size",
		type=int,
		default=256,
		help="Mini-batch size",
	)
	# Allow single value or comma-separated list for grid runs
	parser.add_argument(
		"--lr",
		type=str,
		default="1e-7",
		help=(
			"Learning rate for Adam; accepts a single value (e.g., 1e-3) or a "
			"comma-separated list (e.g., 0.01,0.001,0.0001)"
		),
	)
	# Learning rate scheduler adjustment parameter (ExponentialLR gamma)
	parser.add_argument(
		"--learning-rate-adjustment-parameter",
		type=float,
		default=0.998,
		help=(
			"Learning rate adjustment parameter used as gamma in ExponentialLR. "
			"Controls per-epoch multiplicative decay of the learning rate."
		),
	)
	parser.add_argument(
		"--metrics-file",
		type=str,
		default=os.path.join("data", "training_metrics.csv"),
		help="Path to CSV for epoch metrics",
	)
	parser.add_argument(
		"--device",
		type=str,
		default="auto",
		choices=["auto", "cpu", "cuda"],
		help="Device selection",
	)
	parser.add_argument(
		"--limit",
		type=int,
		default=None,
		help="Optional limit of DB rows during load (for debugging)",
	)
	parser.add_argument(
		"--hidden-layers",
		type=int,
		default=6000,
		help="Hidden layer width for MLP (defaults to input size if <=0)",
	)
	# Accept single int or inclusive range like "2-4" for multiple runs
	parser.add_argument(
		"--mlp-layers",
		type=str,
		default="4",
		help=(
			"Total number of Linear layers in the MLP (including the output layer), "
			"or a range like '2-4'"
		),
	)
	parser.add_argument(
		"--train-percent",
		type=float,
		default="0.90",
		help="percent of data to train on",
	)
	parser.add_argument(
		"--val-percent",
		type=float,
		default="0.05",
		help="percent of data to use for validation",
	)
	# parser.add_argument(
	# 	"--data-head-ignore-percent",
	# 	type=float,
	# 	default=0.0,
	# 	help=(
	# 		"Fraction [0.0-1.0] of the beginning of the dataset to ignore when "
	# 		"choosing instances for train/val/test (e.g. 0.70 ignores the first "
	# 		"70 percent of instances)."
	# 	),
	# )
	# parser.add_argument(
	# 	"--data-tail-ignore-percent",
	# 	type=float,
	# 	default=0.0,
	# 	help=(
	# 		"Fraction [0.0-1.0] of the *end* of the dataset to ignore when choosing "
	# 		"instances for train/val/test (e.g. 0.10 ignores the last 10 percent of "
	# 		"instances)."
	# 	),
	# )
	parser.add_argument(
		"--raw-train-start-with-special-characters",
		type=str,
		default=None,
		help=(
			"Optional starting date_id (inclusive) for the training/validation/test "
			"window. When provided together with --raw-train-end-with-special-characters, these take "
			"precedence over --data-head-ignore-percent / --data-tail-ignore-percent "
			"for defining the usable date range. Accepts either an absolute "
			"date_id (e.g. 15315), a relative offset from the start as '+N' "
			"(0-based index into the available date_id array), or a relative "
			"offset from the end as '-N' (N steps back from the last available "
			"date_id). If omitted, the first available date_id is used."
		),
	)
	parser.add_argument(
		"--raw-train-end-with-special-characters",
		type=str,
		default=None,
		help=(
			"Optional ending date_id (inclusive) for the training/validation/test "
			"window. When provided together with --raw-train-start-with-special-characters, these take "
			"precedence over --data-head-ignore-percent / --data-tail-ignore-percent "
			"for defining the usable date range. Accepts either an absolute "
			"date_id (e.g. 16767), a relative offset from the start as '+N' "
			"(0-based index into the available date_id array), or a relative "
			"offset from the end as '-N' (N steps back from the last available "
			"date_id). If omitted, the last available date_id is used."
		),
	)
	# Random training window duration (in date-index steps): picks a random contiguous
	# window so that end_index - start_index == duration. Accept both hyphen/underscore.
	parser.add_argument(
		"--random-training-duration",
		dest="random_training_duration",
		type=int,
		default=None,
		help=(
			"If set, choose a random contiguous training window whose length (in date index steps) "
			"equals this duration, i.e. end_index - start_index == duration. This allows evaluating "
			"the model at random points in time rather than always at the end. When provided, it "
			"takes precedence over --train-start-date-id/--train-end-date-id."
		),
	)
	parser.add_argument(
		"--run-repeats",
		type=int,
		default=1,
		help=(
			"How many times to repeat each distinct parameter configuration. "
			"Values <= 0 are treated as 1."
		),
	)
	parser.add_argument(
		"--log-interval",
		type=int,
		default=50,
		help="How many batches between progress prints during training (0 to disable)",
	)
	# Rank displacement metric window
	parser.add_argument(
		"--rank-lower",
		type=int,
		default=1,
		help="Lower bound (inclusive, 1-based) of rank window for displacement error metric",
	)
	parser.add_argument(
		"--rank-upper",
		type=int,
		default=10,
		help="Upper bound (inclusive, 1-based) of rank window for displacement error metric",
	)
	# Ranking loss switching (metric is always logged)
	parser.add_argument(
		"--use-ranking-error-loss",
		action="store_true",
		help="Use differentiable ranking surrogate loss (softmax KL) instead of MSE for backprop",
	)
	parser.add_argument(
		"--rank-temp",
		type=float,
		default=1.0,
		help="Temperature for ranking surrogate loss softmax (T>0, higher is softer)",
	)
	# Ability to disable rank error calculation (saves CPU during training/eval)
	parser.add_argument(
		"--enable-rank-err",
		action="store_true",
		help="Enable rank error calculation and logging ",
	)
	parser.add_argument(
		"--split-mode",
		type=str,
		choices=[
			"sequential",
			"random_order_train_validation",
			"mix_training_and_validation",
		],
		default="random_order_train_validation",
		help=(
			"How to split train/val/test: sequential (by time/index), "
			"random_order_train_validation (shuffled indices), "
			"mix_training_and_validation (intermix training and validation indices)"
		),
	)
	parser.add_argument(
		"--seed",
		type=int,
		default=None,
		help="Random seed used when --split-mode random is selected (optional)",
	)
	parser.add_argument(
		"--precision",
		type=str,
		choices=["auto", "fp32", "fp16", "bf16"],
		default="fp32",
		help=(
			"Math precision for training/inference: auto (CUDA->fp16 else fp32), "
			"fp32, fp16, or bf16"
		),
	)
	parser.add_argument(
		"--run-repeat-index",
		type=int,
		default=1,
		help=(
			"Internal index (1-based) of this repeat when --run-repeats > 1. "
			"Normally set by the launcher; users do not need to specify this."
		),
	)
	parser.add_argument(
		"--initial-run",
		type=str,
		default=None,
		help=(
			"Identifier of the initial run in a compound invocation. "
			"This is normally set by higher-level harnesses to allow grouping "
			"related runs in comparison tools."
		),
	)
	parser.add_argument(
		"--arch",
		type=str,
		default="mlp",
		choices=["conv_trans", "mlp"],
		help="Model architecture: conv_trans (Conv+Transformer) or mlp (configurable-layer MLP)",
	)
	parser.add_argument(
		"--activation",
		type=str,
		default="leaky_relu",
		help=(
			"Activation for MLP hidden layers (ignored for conv_trans). "
			"Accepts a single value or a comma-separated list. Supported: "
			"relu, gelu, tanh, sigmoid, silu, swish, leaky_relu, elu, tanshrink"
		),
	)
	parser.add_argument(
		"--final-activation",
		type=str,
		default="sigmoid",
		help=(
			"Activation for MLP output layers (ignored for conv_trans). "
			"Accepts a single value or a comma-separated list. Supported: "
			"relu, gelu, tanh, sigmoid, silu, swish, leaky_relu, elu, tanshrink, "
			"softmax, logsoftmax"
		),
	)
	parser.add_argument(
		"--day-change-centered",
		type=str,
		default="zero",
		help=(
			"Center of next_day_change_in_price ratio. "
			"Accepts a single value or a comma-separated list. Supported values: "
			"one, half, zero, gapped01, binary_labels, top_100_binary_labels."
		),
	)
	parser.add_argument(
		"--include-features",
		type=str,
		default="",
		help=(
			"Comma-separated list of optional input features to include. "
			"Supported entries: price-change-today, volume-ratio, vwap-ratio, "
			"transaction-ratio. Example: --include-features "
			"price-change-today,volume-ratio"
		),
	)
	parser.add_argument(
		"--include-one-feature",
		type=str,
		default="",
		help=(
			"Comma-separated list of optional features where exactly one will be "
			"included per sub-run. Supported entries are the same as for "
			"--include-features: price-change-today, volume-ratio, vwap-ratio, "
			"transaction-ratio. When provided, compound runs will iterate over "
			"these values, spawning one run per feature."
		),
	)
	# DataLoader and shuffling options
	parser.add_argument(
		"--dont-use-dataloader",
		action="store_true",
		help=(
			"don't use torch_interface DataLoader for batching (enables workers, pin_memory, "
			"and optional shuffling)"
		),
	)
	# Optional post-training simulation
	parser.add_argument(
		"--simulate-after",
		action="store_true",
		help="After saving the trained model, run simulate_trading on it",
	)
	parser.add_argument(
		"--simulate-percent",
		type=float,
		default=5.0,
		help="Percent of dates to simulate in simulate_trading (last N percent)",
	)
	parser.add_argument(
		"--simulate-top-k",
		type=str,
		default="1,3,5",
		help=(
			"Top-K tickers to buy per day for simulate_trading. Accepts a single int "
			"(e.g., 10) or a comma-separated list (e.g., 3,5,10)."
		),
	)
	parser.add_argument(
		"--simulate-prior-only",
		action="store_true",
		help=(
			"When set, simulation builds features strictly from prior days "
			"[d-1..d-N] if available"
		),
	)
	parser.add_argument(
		"--trading-interval",
		type=int,
		default=0,
		help=(
			"If > 0, run simulate_trading periodically during training after every "
			"N epochs and write per-interval CSVs named "
			"stock_simulation_<simulate_set_size>_epoch_<epoch>.csv into the run "
			"directory. A value of 0 disables periodic simulation."
		),
	)
	parser.add_argument(
		"--shuffle",
		action="store_true",
		help=(
			"Shuffle training samples each epoch (only when --use-dataloader). "
			"Default off"
		),
	)
	parser.add_argument(
		"--num-workers",
		type=int,
		default=0,
		help="Number of DataLoader workers (only when --use-dataloader)",
	)
	parser.add_argument(
		"--pin-memory",
		action="store_true",
		help=(
			"Enable DataLoader pin_memory for faster host->GPU transfer "
			"(only when --use-dataloader)"
		),
	)
	return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
	"""Parse argv using the shared training ArgumentParser.

	This mirrors the previous inline ``argparse`` usage in
	:func:`train_pytorch_network._main_impl`.
	"""

	parser = build_parser()
	return parser.parse_args(argv)


__all__ = ["build_parser", "parse_args"]
