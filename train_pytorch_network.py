"""
PyTorch training module for date-indexed stock data instances.

This script uses `training_instance_provider.TrainingInstanceProvider` to
construct sequential instances without shuffling and trains a compact
convolution + transformer network (no dense Linear layers).

Data split policy (deterministic, by instance index):
  - First 75% of instance indexes -> training set
  - Next 15% (75%..90%) -> validation set
  - Final 10% (90%..) -> test set (used only for final scoring)

Feature window:
  - Configure how many prior days to include via `--prior-days` (>=0).
  - Input size is inferred from the returned feature tensor length.
  - Output size equals the label tensor length.

Network architecture (revised):
  - Conv1d with kernel_size=15 (connects to 14 nearest neighbors) on the 1D input sequence
  - Transformer encoder layer (channels as model width)
  - Conv1d with kernel_size=15
  - Transformer encoder layer
  - Conv1d with kernel_size=15
  - 1x1 Conv1d to project to output channels, then global average pooling over sequence length to produce output_size

Metrics:
  - Standard Mean Squared Error (MSE) on training and validation per epoch
  - MSE restricted to the largest-valued 10 outputs (per-sample, by predicted
	output values) and the smallest-valued 10 outputs, for both train and val
  - Final metrics are also computed on the held-out test set

All metrics are printed and also written to CSV so they can be graphed later.

Example (train):
  python train_pytorch_network.py --mode train --prior-days 3 --epochs 20 --batch-size 32 \
	  --lr 1e-3 --metrics-file data/training_metrics.csv

Memory estimate only (no training):
  python train_pytorch_network.py --mode memest --prior-days 3 --batch-size 64
"""
from __future__ import annotations

import argparse
import csv
import os
import time
import sys
import subprocess
from typing import Iterable, List, Optional

import numpy as np

# Trainer package imports (split from monolithic script)
from trainer.compat import TORCH_AVAILABLE, torch
from trainer.dataloaders import build_dataloaders
from trainer.datasets import batched
from trainer.device import setup_device_and_precision, enforce_cuda_or_fail
from trainer.factory import create_model_and_optimizer
from trainer.io_run import write_label_limits, write_run_params, save_model_and_manifest
from trainer.losses import ranking_surrogate_loss
from trainer.memory import estimate_memory
from trainer.metrics import mse_restricted, eval_epoch
from trainer.ranking import (
	ranking_score,
	ranking_max_score,
	ranking_error_from_positions,
	vector_to_ranking_indices,
	batch_positions_from_prediction_and_labels,
)
from trainer.splits import split_datasets
from trainer.utils import count_elements
from training_instance_provider import TrainingInstanceProvider


# Dataset classes, batching, metrics, and other helpers are provided by trainer.* modules
def ranking_error(
	predicted_order: Iterable,
	true_order: Iterable,
) -> float:
	"""Return a normalized error in [0, 1] where 0 is perfect ranking and 1 is worst.

	Error = 1 - (score / max_score).
	If ``max_score`` is 0 (degenerate N=0), returns 0.0.
	"""
	s = ranking_score(predicted_order, true_order)
	m = ranking_max_score(len(list(true_order)))
	return 0.0 if m == 0.0 else max(0.0, min(1.0, 1.0 - (s / m)))


# -------------------------------
# New single-argument API (integers only)
# -------------------------------

def _positions_from_orders(predicted_order: Iterable, true_order: Iterable) -> List[int]:
	"""Build a positions array from two orders (internal helper).

	Returns a list `pos` of length N, where `pos[p-1]` is the 1-based true position
	of the item placed at predicted position `p`.
	"""
	pred = list(predicted_order)
	truth = list(true_order)
	if len(pred) != len(truth):
		raise ValueError("predicted_order and true_order must have the same length")
	N = len(pred)
	if N == 0:
		return []

	# Map item -> 0-based index in true order
	pos_true = {item: i for i, item in enumerate(truth)}

	positions: List[int] = []
	for item in pred:
		if item not in pos_true:
			raise ValueError("All items in predicted_order must exist in true_order")
		t0 = pos_true[item]  # 0-based
		positions.append(t0 + 1)  # store 1-based true position
	return positions




def matrix_to_ranking_indices(mat) -> List[List[int]]:
	"""Return rankings per row for a 2D matrix of scores.

	- Input can be: torch.Tensor (2D), numpy.ndarray (2D), or a sequence of 1D sequences.
	- For each row, higher score ranks earlier; ties broken by smaller column index.

	Returns a list of lists of 0-based indices: one ranking per row.
	"""
	# Batch implementation using NumPy stable argsort along axis=1
	if TORCH_AVAILABLE and isinstance(mat, torch.Tensor):  # type: ignore
		if mat.ndim != 2:
			raise ValueError("matrix_to_ranking_indices: expected 2D tensor")
		arr = mat.detach().cpu().numpy()
	elif isinstance(mat, np.ndarray):
		if mat.ndim != 2:
			raise ValueError("matrix_to_ranking_indices: expected 2D array")
		arr = mat
	else:
		arr = np.asarray([list(row) for row in mat], dtype=np.float64)

	idx = np.argsort(-arr, axis=1, kind="mergesort")
	return idx.astype(np.int64).tolist()


def positions_from_prediction_and_labels(pred_scores, true_labels) -> List[int]:
	"""Build the integer `positions` list from prediction and label vectors.

	Contract (per sample):
	  - `pred_scores`: 1D scores (higher is better) from the model for each output node.
	  - `true_labels`: 1D ground-truth scores/labels for the same outputs.

	Returns:
	  - `positions` list of length N where `positions[p-1]` is the 1-based true
		rank of the item placed at predicted position `p`.

	This output is directly consumable by `ranking_score_from_positions` and
	`ranking_error_from_positions`.
	"""
	# Produce 0-based index rankings for predictions and labels
	pred_order = vector_to_ranking_indices(pred_scores)
	true_order = vector_to_ranking_indices(true_labels)

	# Fast path when using integer index lists 0..N-1 (which vector_to_ranking_indices returns)
	N = len(pred_order)
	if N == 0:
		return []
	# Convert to NumPy for vectorized inverse mapping
	pred_idx = np.asarray(pred_order, dtype=np.int64)
	true_idx = np.asarray(true_order, dtype=np.int64)
	# Validate same length
	if pred_idx.shape[0] != true_idx.shape[0]:
		raise ValueError("positions_from_prediction_and_labels: predicted and label lengths differ")
	# Build inverse rank mapping for true order: item -> true 0-based position
	inv_true = np.empty(N, dtype=np.int64)
	inv_true[true_idx] = np.arange(N, dtype=np.int64)
	# positions[p-1] = 1 + true_rank_of_item_at_pred_pos
	positions = (inv_true[pred_idx] + 1).astype(np.int64)
	return positions.tolist()

def main(argv: Optional[List[str]] = None) -> int:
	parser = argparse.ArgumentParser(description="Train a PyTorch dense network on stock instances.")
	parser.add_argument("--mode", type=str, default="train", choices=["train", "memest"], help="Run mode: train or memest (memory estimate only)")
	# Accept single int or inclusive range like "2-4" for multiple runs
	parser.add_argument("--prior-days", type=str, default="0", help="How many prior days to include in the feature window (>=0) or a range like '2-4' to run multiple")
	parser.add_argument("--epochs", type=int, default=10, help="Number of training epochs")
	parser.add_argument("--batch-size", type=int, default=32, help="Mini-batch size")
	# Allow single value or comma-separated list for grid runs
	parser.add_argument("--lr", type=str, default="1e-3", help="Learning rate for Adam; accepts a single value (e.g., 1e-3) or a comma-separated list (e.g., 0.01,0.001,0.0001)")
	parser.add_argument("--metrics-file", type=str, default=os.path.join("data", "training_metrics.csv"), help="Path to CSV for epoch metrics")
	parser.add_argument("--device", type=str, default="auto", choices=["auto", "cpu", "cuda"], help="Device selection")
	parser.add_argument("--limit", type=int, default=None, help="Optional limit of DB rows during load (for debugging)")
	parser.add_argument("--hidden-layers", type=int, default=0, help="Hidden layer width for MLP (defaults to input size if <=0)")
	# Accept single int or inclusive range like "2-4" for multiple runs
	parser.add_argument("--mlp-layers", type=str, default="3", help="Total number of Linear layers in the MLP (including the output layer), or a range like '2-4'")
	parser.add_argument("--log-interval", type=int, default=50, help="How many batches between progress prints during training (0 to disable)")
	parser.add_argument("--close-ratio-only", action="store_true", help="Use only the close_ratio_to_prior as the single input feature per ticker (smaller feature vector)")
	# Rank displacement metric window
	parser.add_argument("--rank-lower", type=int, default=1, help="Lower bound (inclusive, 1-based) of rank window for displacement error metric")
	parser.add_argument("--rank-upper", type=int, default=10, help="Upper bound (inclusive, 1-based) of rank window for displacement error metric")
	# Ranking loss switching (metric is always logged)
	parser.add_argument("--use-ranking-error-loss", action="store_true", help="Use differentiable ranking surrogate loss (softmax KL) instead of MSE for backprop")
	parser.add_argument("--rank-temp", type=float, default=1.0, help="Temperature for ranking surrogate loss softmax (T>0, higher is softer)")
	# Ability to disable rank error calculation (saves CPU during training/eval)
	parser.add_argument(
		"--disable-rank-err",
		action="store_true",
		help="Disable rank error calculation and logging (sets values to 0.0)",
	)
	parser.add_argument(
		"--split-mode",
		type=str,
		choices=["sequential", "random_order_train_validation", "mix_training_and_validation"],
		default="sequential",
		help="How to split train/val/test: sequential (by time/index), random_order_train_validation (shuffled indices), mix_training_and_validation (intermix training and validation indices)",
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
		default="auto",
		help="Math precision for training/inference: auto (CUDA->fp16 else fp32), fp32, fp16, or bf16",
	)
	parser.add_argument(
		"--arch",
		type=str,
		default="conv_trans",
		choices=["conv_trans", "mlp"],
		help="Model architecture: conv_trans (Conv+Transformer) or mlp (configurable-layer MLP)",
	)
	parser.add_argument(
		"--activation",
		type=str,
		default="relu",
		help=(
			"Activation for MLP hidden layers (ignored for conv_trans). "
			"Accepts a single value or a comma-separated list. Supported: "
			"relu, gelu, tanh, sigmoid, silu, swish, leaky_relu, elu, tanshrink"
		),
	)
	parser.add_argument(
		"--day-change-centered",
		type=str,
		default="one",
		choices=["one", "half", "zero", "gapped01"],
		help="Center of next_day_change_in_price ratio",
	)
	parser.add_argument(
		"--change-in-price-today",
		action="store_true",
		help="include change_in_price_today in the input features",
	)

	parser.add_argument(
		"--volume-ratio",
		action="store_true",
		help="include volume_ratio in the input features",
	)

	parser.add_argument(
		"--vwap-ratio",
		action="store_true",
		help="include vwap_ratio in the input features",
	)

	parser.add_argument(
		"--transaction-ratio",
		action="store_true",
		help="include transaction_ratio in the input features",
	)
	# DataLoader and shuffling options
	parser.add_argument(
		"--use-dataloader",
		action="store_true",
		help="Use torch DataLoader for batching (enables workers, pin_memory, and optional shuffling)",
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
		default=10.0,
		help="Percent of dates to simulate in simulate_trading (last N percent)",
	)
	parser.add_argument(
		"--simulate-top-k",
		type=str,
		default="1,3,5",
		help=(
			"Top-K tickers to buy per day for simulate_trading. Accepts a single int (e.g., 10) "
			"or a comma-separated list (e.g., 3,5,10)."
		),
	)
	parser.add_argument(
		"--simulate-sp500-id",
		type=int,
		default=None,
		help="Optional ticker id to use as S&P500 benchmark during simulation",
	)
	parser.add_argument(
		"--simulate-prior-only",
		action="store_true",
		help="When set, simulation builds features strictly from prior days [d-1..d-N] if available",
	)
	parser.add_argument(
		"--shuffle",
		action="store_true",
		help="Shuffle training samples each epoch (only when --use-dataloader). Default off",
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
		help="Enable DataLoader pin_memory for faster host->GPU transfer (only when --use-dataloader)",
	)
	args = parser.parse_args(argv)

	# Helper to parse single int or inclusive range "a-b" into a list of ints
	def _parse_int_or_range(val: str) -> List[int]:
		try:
			return [int(val)]
		except Exception:
			pass
		if isinstance(val, str) and "-" in val:
			parts = val.split("-", 1)
			try:
				a = int(parts[0].strip())
				b = int(parts[1].strip())
			except Exception:
				raise argparse.ArgumentTypeError(f"Invalid range value: {val}")
			if b < a:
				a, b = b, a
			# constrain to non-negative
			a = max(0, a)
			b = max(0, b)
		return list(range(a, b + 1))

	# Helper to parse a comma-separated list of floats for learning rates
	def _parse_lr_list(val) -> List[float]:
		# Already a float or int
		try:
			if isinstance(val, (float, int)):
				return [float(val)]
		except Exception:
			pass
		s = str(val).strip() if val is not None else "1e-3"
		if "," in s:
			parts = [p.strip() for p in s.split(",") if p.strip()]
			out: List[float] = []
			for p in parts:
				try:
					out.append(float(p))
				except Exception:
					raise argparse.ArgumentTypeError(f"Invalid --lr entry: '{p}'")
			if not out:
				return [1e-3]
			return out
		# Single value path
		try:
			return [float(s)]
		except Exception:
			raise argparse.ArgumentTypeError(f"Invalid --lr value: '{val}'")

	# Helper to parse a comma-separated list of activations
	VALID_ACTS = {"relu", "gelu", "tanh", "sigmoid", "silu", "swish", "leaky_relu", "elu", "tanshrink"}
	def _parse_activation_list(val) -> List[str]:
		s = str(val).strip() if val is not None else "relu"
		if "," in s:
			parts = [p.strip().lower() for p in s.split(",") if p.strip()]
			if not parts:
				return ["relu"]
			for p in parts:
				if p not in VALID_ACTS:
					raise argparse.ArgumentTypeError(f"Invalid --activation entry: '{p}'. Supported: {sorted(VALID_ACTS)}")
			return parts
		# Single value path
		p = s.lower()
		if p not in VALID_ACTS:
			raise argparse.ArgumentTypeError(f"Invalid --activation value: '{s}'. Supported: {sorted(VALID_ACTS)}")
		return [p]

	# Helper to parse simulate top-k which may be a single int or comma-separated list
	def _parse_topk(val) -> List[int]:
		# Already an int
		try:
			if isinstance(val, int):
				v = int(val)
				return [v]
		except Exception:
			pass
		s = str(val).strip() if val is not None else "10"
		if "," in s:
			parts = [p.strip() for p in s.split(",") if p.strip()]
			out: List[int] = []
			for p in parts:
				try:
					out.append(int(p))
				except Exception:
					raise argparse.ArgumentTypeError(f"Invalid --simulate-top-k entry: '{p}'")
			if not out:
				return [10]
			return out
		# Single value path
		try:
			return [int(s)]
		except Exception:
			raise argparse.ArgumentTypeError(f"Invalid --simulate-top-k value: '{val}'")

	prior_days_list = _parse_int_or_range(str(args.prior_days))
	mlp_layers_list = _parse_int_or_range(str(args.mlp_layers))
	lr_list = _parse_lr_list(getattr(args, "lr", "1e-6"))
	activation_list = _parse_activation_list(getattr(args, "activation", "relu"))

	# Determine if this is a single run explicitly specified without range syntax
	# We only consider it a "no-range" run when both options are single values
	# and the original argument strings did not contain a '-'. This preserves
	# prior behavior for explicit ranges like "2-4" as well as single-value lists.
	single_run_no_range = (
		len(prior_days_list) == 1
		and len(mlp_layers_list) == 1
		and len(lr_list) == 1
		and len(activation_list) == 1
		and ("-" not in str(args.prior_days))
		and ("-" not in str(args.mlp_layers))
		and ("," not in str(args.lr))
		and ("," not in str(args.activation))
	)

	# Build a timestamped run directory and place metrics inside it
	ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
	# Separate the provided metrics path into directory and filename
	metrics_dir = os.path.dirname(args.metrics_file)
	metrics_base = os.path.basename(args.metrics_file)
	if not metrics_base:
		metrics_base = "training_metrics.csv"
	# Create run subdirectory: <metrics_dir>/<timestamp>
	base_run_dir = os.path.join(metrics_dir if metrics_dir else "", ts)
	# Ensure the run directory exists now so we can write parameters file early
	os.makedirs(base_run_dir, exist_ok=True)
	# Informative prints so the user can find outputs
	print(f"Run directory: {base_run_dir}")

	# We will iterate over combinations of prior_days and mlp_layers
	# Each combination writes its own outputs with identifying suffixes

	# Support both torch.Tensor (train mode) and numpy.ndarray (memest mode)

	# Prepare a single provider instance up-front and reuse it across runs
	# to avoid reloading data from the database for each configuration.
	provider = TrainingInstanceProvider(
		use_torch=(args.mode == "train" and TORCH_AVAILABLE),
		limit=args.limit,
		current_day_change=args.change_in_price_today,
		volume_ratio=args.volume_ratio,
		vwap_ratio=args.vwap_ratio,
		tranaction_ratio=args.transaction_ratio,
		day_change_centered=args.day_change_centered,
	)

	# Define a function to run one configuration using the shared provider
	def _run_one(prior_days: int, mlp_layers: int, lr: float, activation: str) -> None:
		# Set the sliding window size for this configuration
		provider.set_how_many_days_in_past_to_use(int(prior_days))

		max_inst = provider.get_maximum_number_of_test_train_instances()
		if max_inst <= 1:
			raise RuntimeError("Not enough instances to train. Consider reducing --prior-days or loading more data.")

		# Determine input/output sizes from the first instance
		x0, y0 = provider.get_feature_and_label_pair(1)
		input_size = count_elements(x0)
		output_size = count_elements(y0)

		# Build datasets (train/val/test)
		n = max_inst
		train_ds, val_ds, test_ds = split_datasets(provider, args, n)

		# Build output directory
		# If no range was provided (single-run), write directly under the timestamp
		# directory. Otherwise, create a per-config subdirectory to avoid collisions.
		# Safe tokens for filenames
		act_tok = str(activation).lower()
		lr_tok = (f"{lr:.6g}").replace(".", "p").replace("-", "m")
		suffix = f"prior{prior_days}_layers{mlp_layers}_lr{lr_tok}_act{act_tok}"
		if single_run_no_range:
			run_dir = base_run_dir
		else:
			run_dir = os.path.join(base_run_dir, suffix)
		os.makedirs(run_dir, exist_ok=True)
		metrics_file = os.path.join(run_dir, f"{suffix}_" + metrics_base)

		# Write per-date label limits for the training split
		#write_label_limits(run_dir, provider, train_ds)

		# Write run parameters for traceability (per-config)
		sim_topks_resolved = _parse_topk(getattr(args, "simulate_top_k", "10"))
		write_run_params(
			run_dir,
			ts,
			args,
			{
				"resolved_prior_days": prior_days,
				"resolved_mlp_layers": mlp_layers,
				"resolved_lr": lr,
				"resolved_activation": activation,
			},
			sim_topks_resolved,
		)

		# Device selection and precision (only needed for train mode)
		if args.mode == "train":
			print(f"Mode:Train")
			device, eff_precision, amp_enabled, amp_dtype = setup_device_and_precision(args)
			feature_mode = "close_ratio_only" if args.close_ratio_only else "full_feature_set"
			print(f"Config: prior_days={prior_days} epochs={args.epochs} batch_size={args.batch_size} lr={lr} activation={activation} feature_mode={feature_mode} mlp_layers={mlp_layers} split_mode={args.split_mode} seed={args.seed} use_dataloader={bool(args.use_dataloader)} shuffle={bool(getattr(args,'shuffle', False))} workers={int(getattr(args,'num_workers',0))} pin_memory={bool(getattr(args,'pin_memory', False))}")
			print(f"Instances: total={n} | splits -> train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}")
			print(f"Tensor sizes: input_size={input_size} output_size={output_size}")

		# If only estimating memory, do calculations and exit early (no CUDA requirement)
		if args.mode == "memest":
			print(f"Mode:Memest")
			estimate_memory(provider, args, input_size, output_size, n, train_ds, val_ds, test_ds)
			return

		# Enforce GPU usage: throw an error if not using CUDA (train mode only)
		if args.mode == "train":
			enforce_cuda_or_fail(device)
		# Model, loss, optimizer (train mode only)
		if args.mode == "train":
			model, optimizer, scaler, use_amp, amp_dtype = create_model_and_optimizer(
				args, input_size, output_size, mlp_layers, activation, device, lr
			)
			if use_amp and amp_dtype == torch.float16:
				print("Precision runtime: autocast=float16 with GradScaler")
			elif use_amp and amp_dtype == torch.bfloat16:
				print("Precision runtime: autocast=bfloat16 (no GradScaler)")
			else:
				print("Precision runtime: float32 (no autocast)")
			param_count = sum(p.numel() for p in model.parameters())
			print(f"Model parameters: {param_count:,}")

		# Ensure metrics directory exists
		os.makedirs(os.path.dirname(metrics_file), exist_ok=True)

		# Build batch providers (DataLoader or simple generator)
		use_dl, train_loader, val_loader, test_loader = build_dataloaders(args, train_ds, val_ds, test_ds)

		# Open CSV and write header
		with open(metrics_file, mode="w", newline="", encoding="utf-8") as f:
			writer = csv.writer(f)
			header = [
				"epoch",
				"train_mse",
				"val_mse",
				"train_top10_mse",
				"val_top10_mse",
				# "train_bottom10_mse",
				# "val_bottom10_mse",
				"train_rank_err",
				"val_rank_err",
			]
			writer.writerow(header)

			# Training loop
			if args.mode == "train":
				#TORCH PROFILE START
				best_val_mse = float("inf")
				# with torch.profiler.profile(
				# 	schedule=torch.profiler.schedule(wait=1, warmup=1, active=3, repeat=1),
				# 	on_trace_ready=torch.profiler.tensorboard_trace_handler(run_dir),
				# 	record_shapes=True,
				# 	profile_memory=True,
				# 	with_stack=True,
				# ) as prof:
				for epoch in range(1, args.epochs + 1):
					print(f"\n=== Starting epoch {epoch}/{args.epochs} ===")
					epoch_start = time.time()

					model.train()
					total = 0
					train_loss_sum = 0.0
					train_top10_sum = 0.0
					#train_bot10_sum = 0.0
					train_rank_err_sum = 0.0

					batch_idx = 0
					if use_dl:
						train_iter = train_loader  # iterate DataLoader anew each epoch
						total_batches = len(train_loader) if hasattr(train_loader, "__len__") else 0  # type: ignore[arg-type]
					else:
						train_iter = batched(iter(train_ds), args.batch_size)
						total_batches = (len(train_ds) + args.batch_size - 1) // args.batch_size
					for xb, yb in train_iter:
						xb = xb.to(device)
						yb = yb.to(device)
						optimizer.zero_grad(set_to_none=True)
						if use_amp:
							with torch.amp.autocast('cuda', dtype=amp_dtype):  # type: ignore[attr-defined]
								xb = torch.nan_to_num(xb, nan=0.0, posinf=0.0, neginf=0.0)
								yb = torch.nan_to_num(yb, nan=0.0, posinf=0.0, neginf=0.0)
								pred = model(xb)
								pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
								if bool(args.use_ranking_error_loss):
									loss = ranking_surrogate_loss(pred, yb.to(pred.dtype), temperature=float(args.rank_temp))
								else:
									loss = torch.mean((pred - yb.to(pred.dtype)) ** 2)
							if not torch.isfinite(loss):
								optimizer.zero_grad(set_to_none=True)
								batch_idx += 1
								continue
							elif amp_dtype == torch.float16:
								scaler.scale(loss).backward()
								scaler.step(optimizer)
								scaler.update()
							else:
								loss.backward()
								optimizer.step()
						else:
							xb = torch.nan_to_num(xb, nan=0.0, posinf=0.0, neginf=0.0)
							yb = torch.nan_to_num(yb, nan=0.0, posinf=0.0, neginf=0.0)
							pred = model(xb)
							pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
							if bool(args.use_ranking_error_loss):
								loss = ranking_surrogate_loss(pred, yb.to(pred.dtype), temperature=float(args.rank_temp))
							else:
								loss = torch.mean((pred - yb.to(pred.dtype)) ** 2)
							if not torch.isfinite(loss):
								optimizer.zero_grad(set_to_none=True)
								batch_idx += 1
								continue
							loss.backward()
							optimizer.step()

					bs = xb.size(0)
					total += bs
					train_loss_sum += loss.item() * bs
					train_top10_sum += mse_restricted(pred.detach(), yb.to(pred.dtype), mode="top", k=10).item() * bs
					#train_bot10_sum += mse_restricted(pred.detach(), yb.to(pred.dtype), mode="bottom", k=10).item() * bs
					if not bool(args.disable_rank_err):
						try:
							pos_list = batch_positions_from_prediction_and_labels(pred.detach().cpu(), yb.detach().cpu())
							batch_rank_err = float(np.mean([ranking_error_from_positions(pos) for pos in pos_list])) if len(pos_list) > 0 else 0.0
						except Exception:
							batch_rank_err = 0.0
					else:
						batch_rank_err = 0.0
					train_rank_err_sum += batch_rank_err * bs

					batch_idx += 1
					if args.log_interval and args.log_interval > 0 and (batch_idx % args.log_interval == 0 or batch_idx == total_batches):
						avg_loss_so_far = train_loss_sum / max(1, total)
						print(f"Epoch {epoch}/{args.epochs} | batch {batch_idx}/{total_batches} | curr_loss={loss.item():.6f} | avg_loss={avg_loss_so_far:.6f}")

					train_mse = train_loss_sum / max(1, total)
					train_top10 = train_top10_sum / max(1, total)
					#train_bot10 = train_bot10_sum / max(1, total)
					train_rank_err = (train_rank_err_sum / max(1, total))

					# Build a fresh iterator for validation each epoch when not using DataLoader
					val_iter = val_loader if use_dl else batched(iter(val_ds), args.batch_size)
					val_mse, val_top10, _val_rde_unused, val_rank_err = eval_epoch(
						model, val_iter, device, amp_enabled=use_amp, amp_dtype=amp_dtype,
						rank_lower=int(args.rank_lower), rank_upper=int(args.rank_upper), compute_rank_err=(not bool(args.disable_rank_err)), compute_rank_disp=False
					)

					epoch_dur = time.time() - epoch_start
					msg = (f"Epoch {epoch:03d} | train_mse={train_mse:.6f} val_mse={val_mse:.6f} "
						  f"train_top10={train_top10:.6f} val_top10={val_top10:.6f} "
#						  f"train_bottom10={train_bot10:.6f} val_bottom10={val_bot10:.6f} "
						  f"train_rank_err={train_rank_err:.4f} val_rank_err={val_rank_err:.4f}"
						  + (" [rank_err:off]" if bool(args.disable_rank_err) else "")
						  + f" | time={epoch_dur:.2f}s")
					print(msg)

					row = [epoch, train_mse, val_mse, train_top10, val_top10,  train_rank_err, val_rank_err]
					writer.writerow(row)
					f.flush()

					if epoch == 1 or val_mse <= best_val_mse:
						best_val_mse = val_mse
						print(f"New best validation MSE: {best_val_mse:.6f} (epoch {epoch})")

		# Final evaluation on test set (train mode only)
		if args.mode == "train":
			# Build a fresh iterator for test as well when not using DataLoader
			test_iter = test_loader if use_dl else batched(iter(test_ds), args.batch_size)
			test_mse, test_top10, test_rde, test_rank_err = eval_epoch(
				model, test_iter, device, amp_enabled=use_amp, amp_dtype=amp_dtype,
				rank_lower=int(args.rank_lower), rank_upper=int(args.rank_upper), compute_rank_err=(not bool(args.disable_rank_err))
			)
			print("Final Test Metrics:")
#			print(f"test_mse={test_mse:.6f} test_top10={test_top10:.6f} test_bottom10={test_bot10:.6f} test_rank_disp={test_rde:.3f} test_rank_err={test_rank_err:.4f}")
			print(f"test_mse={test_mse:.6f} test_top10={test_top10:.6f} test_rank_disp={test_rde:.3f} test_rank_err={test_rank_err:.4f}")

			# Append final test metrics to a sidecar file next to metrics CSV (include suffix)
			test_metrics_path = os.path.splitext(metrics_file)[0] + "_test.csv"
			with open(test_metrics_path, mode="w", newline="", encoding="utf-8") as f_test:
				writer_test = csv.writer(f_test)
				writer_test.writerow(["metric", "value"])
				writer_test.writerow(["test_mse", test_mse])
				writer_test.writerow(["test_top10_mse", test_top10])
				#writer_test.writerow(["test_bottom10_mse", test_bot10])
				writer_test.writerow(["test_rank_disp", test_rde])
				writer_test.writerow(["test_rank_err", test_rank_err])

			print(f"Wrote per-epoch metrics to: {metrics_file}")
			print(f"Wrote final test metrics to: {test_metrics_path}")

			# Save model artifacts with suffix
			try:
				manifest_path = save_model_and_manifest(
					args,
					model,
					optimizer,
					run_dir,
					suffix,
					metrics_file,
					test_metrics_path,
					input_size,
					output_size,
					mlp_layers,
					activation,
					prior_days,
				)
				# Optionally run simulation using the freshly saved model
				if bool(args.simulate_after):
					try:
						# Import locally to avoid overhead when not requested
						import simulate_trading as _sim
						model_ref = manifest_path if os.path.exists(manifest_path) else run_dir
						print("Running simulate_trading on trained model...")
						out_path = _sim.simulate(
							model_path=model_ref,
							percent=float(args.simulate_percent),
							top_k=sim_topks_resolved if isinstance(sim_topks_resolved, list) and len(sim_topks_resolved) > 0 else 10,
							host=os.getenv("MYSQL_HOST", "localhost"),
							database=os.getenv("MYSQL_DB", "stockforecasting"),
							user=os.getenv("MYSQL_USER", "Redbeard"),
							password=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"),
							port=int(os.getenv("MYSQL_PORT", "3306")),
							driver="auto",
							limit=None,
							sp500_ticker_id=(int(args.simulate_sp500_id) if args.simulate_sp500_id is not None else None),
							prefer_prior_only=bool(args.simulate_prior_only),
						)
						print(f"Simulation complete. Results written to: {out_path}")
					except Exception as sim_e:
						print(f"Warning: simulate_trading failed: {sim_e}")
			except Exception as save_e:
				print(f"Warning: failed to save model artifacts: {save_e}")

	# Execute runs for all combinations
	combo_count = (
		len(prior_days_list)
		* len(mlp_layers_list)
		* len(lr_list)
		* len(activation_list)
	)

	# If there is only a single combination and no explicit range usage, run in-process
	if combo_count == 1 and single_run_no_range:
		_run_one(int(prior_days_list[0]), int(mlp_layers_list[0]), float(lr_list[0]), str(activation_list[0]))
		return 0

	# Otherwise, spawn a fresh Python process per combination to avoid in-process
	# memory accumulation (CUDA caches, DataLoader workers, graph state, etc.).
	# We invoke a thin wrapper script (run_one_training_setup.py) with single-valued
	# arguments for each run.
	root_dir = os.path.dirname(os.path.abspath(__file__))
	wrapper_path = os.path.join(root_dir, "run_one_training_setup.py")
	if not os.path.exists(wrapper_path):
		# Fall back to in-process if wrapper is missing
		print("Warning: run_one_training_setup.py not found. Falling back to in-process runs.")
		for pd in prior_days_list:
			for ml in mlp_layers_list:
				for lr in lr_list:
					for act in activation_list:
						_run_one(int(pd), int(ml), float(lr), str(act))
		return 0

	def _flag(name: str) -> str:
		return f"--{name.replace('_','-')}"

	# Iterate combinations and spawn a subprocess per run
	for pd in prior_days_list:
		for ml in mlp_layers_list:
			for lr in lr_list:
				for act in activation_list:
					cmd: List[str] = [sys.executable, wrapper_path]
					# Core single-valued overrides for this run
					cmd += [_flag("mode"), str(args.mode)]
					cmd += [_flag("prior-days"), str(int(pd))]
					cmd += [_flag("mlp-layers"), str(int(ml))]
					cmd += [_flag("lr"), str(float(lr))]
					cmd += [_flag("activation"), str(act)]
					# Pass-through of other options
					cmd += [_flag("epochs"), str(int(args.epochs))]
					cmd += [_flag("batch-size"), str(int(args.batch_size))]
					cmd += [_flag("metrics-file"), str(args.metrics_file)]
					cmd += [_flag("device"), str(args.device)]
					if args.limit is not None:
						cmd += [_flag("limit"), str(int(args.limit))]
					cmd += [_flag("hidden-layers"), str(int(args.hidden_layers))]
					cmd += [_flag("log-interval"), str(int(args.log_interval))]
					if bool(args.change_in_price_today):
						cmd += [_flag("change-in-price-today")]
					if bool(args.volume_ratio):
						cmd += [_flag("volume-ratio")]
					if bool(args.vwap_ratio):
						cmd += [_flag("vwap-ratio")]
					if bool(args.transaction_ratio):
						cmd += [_flag("transaction-ratio")]
					cmd += [_flag("rank-lower"), str(int(args.rank_lower))]
					cmd += [_flag("rank-upper"), str(int(args.rank_upper))]
					if bool(args.use_ranking_error_loss):
						cmd += [_flag("use-ranking-error-loss")]
					cmd += [_flag("rank-temp"), str(float(args.rank_temp))]
					if bool(args.disable_rank_err):
						cmd += [_flag("disable-rank-err")]
					cmd += [_flag("split-mode"), str(args.split_mode)]
					if args.seed is not None:
						cmd += [_flag("seed"), str(int(args.seed))]
					cmd += [_flag("precision"), str(args.precision)]
					cmd += [_flag("arch"), str(args.arch)]
					cmd += [_flag("day-change-centered"), str(args.day_change_centered)]
					if bool(args.use_dataloader):
						cmd += [_flag("use-dataloader")]
					if bool(getattr(args, "shuffle", False)):
						cmd += [_flag("shuffle")]
					if int(getattr(args, "num_workers", 0)):
						cmd += [_flag("num-workers"), str(int(args.num_workers))]
					if bool(getattr(args, "pin_memory", False)):
						cmd += [_flag("pin-memory")]
					if bool(args.simulate_after):
						cmd += [_flag("simulate-after")]
					cmd += [_flag("simulate-percent"), str(float(args.simulate_percent))]
					# simulate_top_k might be comma-separated; pass through original to preserve intent
					cmd += [_flag("simulate-top-k"), str(getattr(args, "simulate_top_k", "10"))]
					if args.simulate_sp500_id is not None:
						cmd += [_flag("simulate-sp500-id"), str(int(args.simulate_sp500_id))]
					if bool(args.simulate_prior_only):
						cmd += [_flag("simulate-prior-only")]

					print(f"Spawning isolated run: prior_days={pd} mlp_layers={ml} lr={lr} activation={act}")
					try:
						completed = subprocess.run(cmd, check=True)
						if completed.returncode != 0:
							print(f"Warning: sub-run exited with code {completed.returncode}")
					except Exception as sub_e:
						print(f"Error running isolated sub-process: {sub_e}")

	return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
	raise SystemExit(main())
