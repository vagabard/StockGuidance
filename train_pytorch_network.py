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

import csv
import math
import os
import re
import subprocess
import sys
import time
from typing import Iterable, List, Optional

import numpy as np
from torch.profiler import tensorboard_trace_handler

from train_pytorch_network_cli import parse_args
from trainer.argparse_utils import (
	general_arg_parser_understands_lists,
	parse_float_list,
	parse_int_list,
)
# Trainer package imports (split from monolithic script)
from trainer.compat import TORCH_AVAILABLE, torch
from trainer.dataloaders import build_dataloaders
from trainer.datasets import batched
from trainer.device import setup_device_and_precision, enforce_cuda_or_fail
from trainer.factory import create_model_and_optimizer
from trainer.io_run import write_run_params, save_model_and_manifest, create_manifest_dictionary
from trainer.losses import ranking_surrogate_loss
from trainer.memory import estimate_memory
from trainer.metrics import mse_restricted, eval_epoch
from trainer.ranking import (
	ranking_score,
	ranking_max_score,
	ranking_error_from_positions,
	batch_positions_from_prediction_and_labels,
)
from trainer.splits import split_datasets
from trainer.utils import count_elements
from training_instance_provider import TrainingInstanceProvider

VALID_ACTIVATIONS = {"relu", "gelu", "tanh", "sigmoid", "silu", "swish", "leaky_relu", "elu", "tanshrink", "softmax",
			  "logsoftmax", "none"}

VALID_DAY_CENTERS = {"one", "half", "zero", "gapped01", "binary_labels", "top_100_binary_labels", "gappednegpos", "binary_negpos", "top_100_binary_negpos"}


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


# Global hook used by the class wrapper to expose the most recent base run directory
LAST_RUN_BASE_DIR: Optional[str] = None


def _main_impl(argv: Optional[List[str]] = None) -> int:
	"""Implementation of the original ``main`` logic.

	This function preserves the existing CLI behaviour. It is wrapped by the
	:class:`TrainPytorchNetwork` class and by :func:`main` which simply
	delegates to the class so that other modules can programmatically invoke
	training and discover the created run directory.
	"""
	global LAST_RUN_BASE_DIR
	print("main args are ", argv, " ")
	args = parse_args(argv)

	# Derive per-feature boolean flags from the unified --include-features option
	# (and later from include-one-feature when iterating compound runs).
	#
	# The CLI exposes a single argument that accepts a comma-separated list of
	# optional feature names. Internally we still work with boolean attributes
	# (change_in_price_today, volume_ratio, vwap_ratio, transaction_ratio) so that
	# existing code paths and manifests remain compatible.
	feature_key_to_attr = {
		"price-change-today": "change_in_price_today",
		"volume-ratio": "volume_ratio",
		"vwap-ratio": "vwap_ratio",
		"transaction-ratio": "transaction_ratio",
	}

	def _set_feature_flags(selected: set[str]) -> None:
		"""Populate args.* boolean feature flags from a set of feature names."""
		for key, attr_name in feature_key_to_attr.items():
			value = key in selected
			setattr(args, attr_name, value)
		# Maintain the historical misspelled attribute name expected by some code.
		setattr(args, "tranaction_ratio", getattr(args, "transaction_ratio", False))

	# Base always-on optional features from --include-features
	raw_include = str(getattr(args, "include_features", "") or "")
	if raw_include.strip():
		base_optional_features = set(
			general_arg_parser_understands_lists(
				raw_include,
				"include_features",
				valid_values=list(feature_key_to_attr.keys()),
			)
		)
	else:
		base_optional_features = set()

	# Optional list of "exactly one per run" features from --include-one-feature.
	raw_include_one = str(getattr(args, "include_one_feature", "") or "")
	if raw_include_one.strip():
		include_one_feature_list = general_arg_parser_understands_lists(
			raw_include_one,
			"include-one-feature",
			valid_values=list(feature_key_to_attr.keys()),
		)
	else:
		include_one_feature_list = []

	# Default feature flags for simple (non-compound) runs come from
	# --include-features only. Compound run logic will override these later per
	# sub-run when iterating include_one_feature_list.
	_set_feature_flags(base_optional_features)

	def _build_dates_to_handle_for_simulation() -> Optional[List[int]]:
		"""Convert ``args.simulate_percent`` into a concrete list of date_ids.

		When ``simulate_percent`` is within (0, 100), we interpret it as
		"last N percent" of available trading dates, based on the global
		date_id range in the database.
		"""

		try:
			percent = float(getattr(args, "simulate_percent", 0.0) or 0.0)
		except Exception:
			percent = 0.0

		# Percent <= 0 means "use all dates" -> let simulate_trading decide.
		if percent <= 0.0:
			return None

		# Clamp excessively large values to 100% which is equivalent to None.
		if percent >= 100.0:
			return None

		try:
			from load_date_id_range import load_date_id_range as _load_date_id_range
		except Exception as exc:
			print(f"Warning: could not import load_date_id_range; running simulation on all dates instead: {exc}")
			return None

		host = os.getenv("MYSQL_HOST", "localhost")
		database = os.getenv("MYSQL_DB", "stockforecasting")
		user = os.getenv("MYSQL_USER", "Redbeard")
		password = os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea")
		port = int(os.getenv("MYSQL_PORT", "3306"))
		driver = "auto"

		try:
			min_date_id, max_date_id = _load_date_id_range(
				host=host,
				database=database,
				user=user,
				password=password,
				port=port,
				driver=driver,
			)
		except Exception as exc:
			print(f"Warning: failed to query date_id range for simulate_percent; running on all dates instead: {exc}")
			return None

		try:
			total_days = max_date_id - min_date_id + 1
			if total_days <= 0:
				return None
			count = max(1, int(math.ceil(total_days * (percent / 100.0))))
			start_id = max_date_id - count + 1
			if start_id < min_date_id:
				start_id = min_date_id
			return list(range(int(start_id), int(max_date_id) + 1))
		except Exception as exc:
			print(f"Warning: failed to derive dates_to_handle from simulate_percent; running on all dates instead: {exc}")
			return None

	prior_days_list = general_arg_parser_understands_lists(str(args.prior_days), "prior_days")
	mlp_layers_list = general_arg_parser_understands_lists(str(args.mlp_layers), "mlp_layers")
	lr_list = parse_float_list(getattr(args, "lr"), "lr")
	activation_list = general_arg_parser_understands_lists(
		getattr(args, "activation", "relu"), "activation", VALID_ACTIVATIONS
	)
	print("final_activation is ", getattr(args, "final_activation", "None"), "  ", args.final_activation)
	final_activation_list = general_arg_parser_understands_lists(
		getattr(args, "final_activation", "None"), "final_activation", VALID_ACTIVATIONS
	)
	print("final activation list is ", final_activation_list)
	day_change_centered_list = general_arg_parser_understands_lists(
		getattr(args, "day_change_centered", "one"),
		"day_change_centered",
		VALID_DAY_CENTERS,
	)
	print("day_change_centered raw arg:", getattr(args, "day_change_centered", None))
	print("day_change_centered_list (parsed):", day_change_centered_list)

	# Determine if this is a single run explicitly specified without range syntax
	# We only consider it a "no-range" run when all sweepable options are single
	# values and the original argument strings did not contain range/list syntax.
	# all the lengths will only equal 1 if each length is 1
	single_run_no_range = ((len(prior_days_list) *len(mlp_layers_list) * len(lr_list)*len(activation_list)*len(day_change_centered_list)*len(mlp_layers_list)*len(final_activation_list)  )==1)

	# Normalise repeat count early so later code can rely on it.
	repeat_count = int(getattr(args, "run_repeats", 1) or 1)
	if repeat_count <= 0:
		repeat_count = 1
	setattr(args, "run_repeats", repeat_count)

	# Build a timestamped run directory and place metrics inside it
	ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
	# Establish an "initial run" identifier for this top-level invocation.
	#
	# If the caller supplied --initial-run (typically a higher-level harness
	# or batch script), we reuse that value so that all related runs can be
	# grouped together. Otherwise we default to this invocation's timestamp.
	initial_run_id = str(getattr(args, "initial_run", "") or ts)
	# Separate the provided metrics path into directory and filename
	metrics_dir = os.path.dirname(args.metrics_file)
	metrics_base = os.path.basename(args.metrics_file)
	if not metrics_base:
		metrics_base = "training_metrics.csv"
	# Create run subdirectory: <metrics_dir>/<timestamp>
	base_run_dir = os.path.join(metrics_dir if metrics_dir else "", ts)
	# Ensure the run directory exists now so we can write parameters file early
	os.makedirs(base_run_dir, exist_ok=True)
	# Record the base run directory so callers can query it later
	LAST_RUN_BASE_DIR = base_run_dir
	# Informative prints so the user can find outputs
	print(f"Run directory: {base_run_dir}")

	# We will iterate over combinations of prior_days and mlp_layers
	# Each combination writes its own outputs with identifying suffixes

	# Support both torch.Tensor (train mode) and numpy.ndarray (memest mode)

	# Cache providers per day_change_centered value so that we only load data
	# from the database once for each distinct choice. This keeps the original
	# optimisation (single provider) in the common single-value case, while
	# still allowing a comma-separated sweep across day_change_centered.
	_providers: dict[str, TrainingInstanceProvider] = {}

	def _get_provider(day_centered: str) -> TrainingInstanceProvider:
		if day_centered not in _providers:
			_providers[day_centered] = TrainingInstanceProvider(
				use_torch=(args.mode == "train" and TORCH_AVAILABLE),
				limit=args.limit,
				current_day_change=args.change_in_price_today,
				volume_ratio=args.volume_ratio,
				vwap_ratio=args.vwap_ratio,
				tranaction_ratio=args.transaction_ratio,
				day_change_centered=day_centered,
			)
		return _providers[day_centered]

	# Define a function to run one configuration using the appropriate provider
	def _run_one(day_centered: str, prior_days: int, mlp_layers: int, lr: float, activation: str, final_activation: str) -> None:
		print("day_centered ", day_centered, "prior_days ", prior_days, "mlp_layers ", mlp_layers, "lr ", lr, "activation ", activation, "final_activation ", final_activation)
		provider = _get_provider(day_centered)
		print(f"Provider: {provider}")
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
		# Determine the date_id window for train/validation/test splits.
		#
		# New behaviour: if explicit train_start_date_id / train_end_date_id are
		# provided on the CLI, they may be absolute date_ids or relative string
		# specifications that are resolved against the provider's available
		# dates. If either bound is omitted we default to the first/last available
		# date_id within the current configuration.
		raw_train_start = getattr(args, "train_start_date_id", None)
		raw_train_end = getattr(args, "train_end_date_id", None)
		try:
			all_dates = [int(d) for d in getattr(provider, "_dates")]
			prior = int(getattr(provider, "_num_prior"))
		except Exception:
			raise RuntimeError(
				"Provider does not expose _dates/_num_prior required for deriving "
				"train_start_date_id/train_end_date_id from available instances"
			)
		# Instances 1..n correspond to dates at indices prior..prior + n - 1.
		if prior < 0 or prior + n - 1 >= len(all_dates):
			raise RuntimeError(
				"Inconsistent provider _dates/_num_prior for deriving train date_id range"
			)
		usable_dates = all_dates[prior : prior + n]
		if not usable_dates:
			raise RuntimeError("No usable dates available for training window computation")

		def _resolve_bound(spec, which: str) -> int:
			"""Resolve a user-specified bound to an absolute date_id.

			``spec`` may be ``None`` (use first/last), an absolute integer
			date_id, or a relative index in string form:
			  * "+N" -> date at index N from the *start* (0-based) of usable_dates
			  * "-N" -> date at index len(usable_dates) - N from the *start*,
					   i.e. N steps back from the last available date.
			"""

			if spec is None:
				return int(usable_dates[0] if which == "start" else usable_dates[-1])

			if isinstance(spec, int):
				return int(spec)

			# Normalise to string for parsing
			text = str(spec).strip()
			if not text:
				return int(usable_dates[0] if which == "start" else usable_dates[-1])

			# Relative from start or end
			if text[0] in "+-" and len(text) > 1:
				try:
					offset = int(text)
				except ValueError:
					raise ValueError(
						f"Invalid {which} date specifier '{spec}': expected integer, '+N' or '-N'"
					)
				length = len(usable_dates)
				if offset >= 0:
					# '+N' (or 'N' with leading '+'): index from start, 0-based
					idx = offset
				else:
					# '-N': N steps back from the end
					idx = length + offset
				if idx < 0 or idx >= length:
					raise ValueError(
						f"Resolved {which} index {idx} from spec '{spec}' is outside the "
						f"available date_id range [0, {length - 1}]"
					)
				return int(usable_dates[idx])

			# Fallback: treat as absolute date_id
			try:
				return int(text)
			except ValueError:
				raise ValueError(
					f"Invalid {which} date specifier '{spec}': expected integer, '+N' or '-N'"
				)

		train_start_date_id = _resolve_bound(raw_train_start, "start")
		train_end_date_id = _resolve_bound(raw_train_end, "end")

		train_ds, val_ds, test_ds = split_datasets(
			provider,
			args,
			n,
			train_validation_and_test_set_start_date_id=int(train_start_date_id),
			train_validation_and_test_set_end_date_id=int(train_end_date_id),
			train_percent=args.train_percent,
			validation_percent=args.val_percent,
		)
		print(f"Datasets: train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}  train start date_id={train_start_date_id} train end date_id={train_end_date_id}")

		# Build output directory
		# If no range was provided (single-run), all configurations share the
		# timestamp directory. When ranges are used, each distinct configuration
		# gets its own subdirectory to avoid collisions.
		#
		# For repeated runs of the *same* configuration we now place outputs in
		# per-repeat subdirectories (rep1, rep2, ...) instead of only encoding the
		# repeat index in the filename. This keeps configurations grouped together
		# while making it easy to inspect or delete individual repeats.
		# Safe tokens for filenames
		act_tok = str(activation).lower()
		lr_tok = (f"{lr:.6g}").replace(".", "p").replace("-", "m")
		repeat_index = int(getattr(args, "run_repeat_index", 1) or 1)
		# Base suffix that identifies the hyper-parameter configuration only
		config_suffix = f"prior{prior_days}_layers{mlp_layers}_lr{lr_tok}_act{act_tok}"
		# Configuration directory (shared by all repeats of this configuration)
		if single_run_no_range:
			config_dir = base_run_dir
		else:
			config_dir = os.path.join(base_run_dir, config_suffix)
		# Decide whether this run is part of a repeated set
		repeat_count = int(getattr(args, "run_repeats", 1) or 1)
		if repeat_count > 1 or repeat_index > 1:
			# Place each repeat in its own subdirectory: rep1, rep2, ...
			rep_dir_name = f"rep{repeat_index}"
			run_dir = os.path.join(config_dir, rep_dir_name)
			# Keep the historical suffix (including _repN) so filenames remain
			# recognisable and any downstream tooling that expects it continues to
			# work.
			suffix = f"{config_suffix}_rep{repeat_index}"
		else:
			# No repeats in use: write directly into the configuration directory
			run_dir = config_dir
			suffix = config_suffix
		os.makedirs(run_dir, exist_ok=True)
		metrics_file = os.path.join(run_dir, f"{suffix}_" + metrics_base)

		# Write per-date label limits for the training split
		#write_label_limits(run_dir, provider, train_ds)

		# Write run parameters for traceability (per-config)
		sim_topks_resolved = parse_int_list(getattr(args, "simulate_top_k", "10"), "simulate_top_k")
		write_run_params(
			run_dir,
			ts,
			args,
			{
				"resolved_prior_days": prior_days,
				"resolved_mlp_layers": mlp_layers,
				"resolved_lr": lr,
				"resolved_activation": activation,
				"resolved_day_change_centered": day_centered,
			},
			sim_topks_resolved,
			initial_run=initial_run_id,
		)

		# Device selection and precision (only needed for train mode)
		if args.mode == "train":
			print(f"Mode:Train")
			device, eff_precision, amp_enabled, amp_dtype = setup_device_and_precision(args)
			print(f"Config: prior_days={prior_days} epochs={args.epochs} batch_size={args.batch_size} lr={lr} activation={activation} mlp_layers={mlp_layers} split_mode={args.split_mode} seed={args.seed} dont_use_dataloader={bool(args.dont_use_dataloader)} shuffle={bool(getattr(args,'shuffle', False))} workers={int(getattr(args,'num_workers',0))} pin_memory={bool(getattr(args,'pin_memory', False))}")
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
				#"train_rank_err",
				#"val_rank_err",
			]
			writer.writerow(header)

			# Training loop
			if args.mode == "train":
				#TORCH PROFILE START
				best_val_mse = float("inf")
				for epoch in range(1, args.epochs + 1):
					should_print_message = epoch % args.log_interval == 0 and epoch != 0
					if should_print_message:
						print(f"\n=== Starting epoch {epoch}/{args.epochs} ===")
					epoch_start = time.time()
					with torch.profiler.profile(
						schedule=torch.profiler.schedule(
							wait=2,
							warmup=2,
							active=6,
							repeat=1),
						on_trace_ready=tensorboard_trace_handler,
						with_stack=True
					) as profiler:

						model.train()
						total = 0
						train_loss_sum = 0.0
						train_top10_sum = 0.0
						#train_bot10_sum = 0.0
						#train_rank_err_sum = 0.0

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
								profiler.step()

					bs = xb.size(0)
					total += bs
					train_loss_sum += loss.item() * bs
					train_top10_sum += mse_restricted(pred.detach(), yb.to(pred.dtype), mode="top", k=10).item() * bs
					#train_bot10_sum += mse_restricted(pred.detach(), yb.to(pred.dtype), mode="bottom", k=10).item() * bs
					if bool(args.enable_rank_err):
						try:
							pos_list = batch_positions_from_prediction_and_labels(pred.detach().cpu(), yb.detach().cpu())
							batch_rank_err = float(np.mean([ranking_error_from_positions(pos) for pos in pos_list])) if len(pos_list) > 0 else 0.0
						except Exception:
							batch_rank_err = 0.0
					else:
						batch_rank_err = 0.0
					#train_rank_err_sum += batch_rank_err * bs

					batch_idx += 1
					if args.log_interval and args.log_interval > 0 and (batch_idx % args.log_interval == 0 or batch_idx == total_batches):
						avg_loss_so_far = train_loss_sum / max(1, total)
						print(f"Epoch {epoch}/{args.epochs} | batch {batch_idx}/{total_batches} | curr_loss={loss.item():.6f} | avg_loss={avg_loss_so_far:.6f}")

					train_mse = train_loss_sum / max(1, total)
					train_top10 = train_top10_sum / max(1, total)
					#train_bot10 = train_bot10_sum / max(1, total)
					#train_rank_err = (train_rank_err_sum / max(1, total))

					# Build a fresh iterator for validation each epoch when not using DataLoader
					val_iter = val_loader if use_dl else batched(iter(val_ds), args.batch_size)
					val_mse, val_top10, _val_rde_unused= eval_epoch(   #, val_rank_err
						model, val_iter, device, amp_enabled=use_amp, amp_dtype=amp_dtype,
						rank_lower=int(args.rank_lower), rank_upper=int(args.rank_upper), compute_rank_err=(bool(args.enable_rank_err)), compute_rank_disp=False
					)

					epoch_dur = time.time() - epoch_start
					if should_print_message:
						msg = (f"Epoch {epoch:03d} | train_mse={train_mse:.6f} val_mse={val_mse:.6f} "
							  f"train_top10={train_top10:.6f} val_top10={val_top10:.6f} "
	#						  f"train_bottom10={train_bot10:.6f} val_bottom10={val_bot10:.6f} "
	#						  f"train_rank_err={train_rank_err:.4f} val_rank_err={val_rank_err:.4f}"
							  + (" [rank_err:on]" if bool(args.enable_rank_err) else "")
							  + f" | time={epoch_dur:.2f}s")
						print(msg)

					row = [epoch, train_mse, val_mse, train_top10, val_top10] #,  train_rank_err, val_rank_err]
					writer.writerow(row)
					f.flush()

					if should_print_message:
#						if epoch == 1 or val_mse <= best_val_mse:
						best_val_mse = val_mse
						print(f"Best validation MSE: {best_val_mse:.6f} (epoch {epoch})")

					# Periodic simulation during training, if requested
					trading_interval = int(getattr(args, "trading_interval", 0) or 0)
					if trading_interval > 0 and epoch % trading_interval == 0:
						try:
							import simulate_trading as _sim  # Local import to avoid overhead when unused
							model_ref = run_dir
							print(
								f"Running periodic simulate_trading at epoch {epoch} "
								f"(interval={trading_interval}) on run_dir={model_ref}..."
							)
							model_dict = create_manifest_dictionary(args,None,None, metrics_file, None, input_size, output_size, mlp_layers, activation, final_activation, prior_days)

							out_path = _sim.simulate(
								model_path=model_ref,
								top_k=sim_topks_resolved
								if isinstance(sim_topks_resolved, list) and len(sim_topks_resolved) > 0
								else 10,
								host=os.getenv("MYSQL_HOST", "localhost"),
								database=os.getenv("MYSQL_DB", "stockforecasting"),
								user=os.getenv("MYSQL_USER", "Redbeard"),
								password=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"),
								port=int(os.getenv("MYSQL_PORT", "3306")),
								driver="auto",
								loaded_model=model,
								model_dict=model_dict,
								limit=None,
								dates_to_handle=_build_dates_to_handle_for_simulation(),
								prefer_prior_only=bool(args.simulate_prior_only),
							)
							print(f"Periodic simulation complete. Raw results written to: {out_path}")
							try:
								# Derive set size and optional variant suffix from the
								# original filename when renaming.
								# We expect filenames such as:
								#   stock_simulation_10.csv
								#   stock_simulation_10d.csv
								#   stock_simulation_10p.csv
								# and we carry both the numeric portion ("10") and the
								# optional d/p suffix into the epoch-based filename, e.g.:
								#   stock_simulation_10d_epoch_<epoch>.csv
								orig_name = os.path.basename(out_path)
								m = re.match(r"stock_simulation_(\d+)([dp])?\.csv$", orig_name)
								if m:
									sim_set_size = int(m.group(1))
									variant_suffix = m.group(2) or ""
								else:
									# Fallback to simulate_percent if the filename doesn't
									# match the expected pattern.
									sim_set_size = int(float(args.simulate_percent))
									variant_suffix = ""
								new_name = f"stock_simulation_{sim_set_size}{variant_suffix}_epoch_{epoch}.csv"
								new_path = os.path.join(os.path.dirname(out_path), new_name)
								os.replace(out_path, new_path)
								print(
									f"Renamed periodic simulation output to: {new_path}"
								)
							except Exception as rename_e:
								print(
									f"Warning: failed to rename periodic simulation output '{out_path}': {rename_e}"
								)
						except Exception as sim_e:
							print(f"Warning: periodic simulate_trading failed at epoch {epoch}: {sim_e}")

		# Final evaluation on test set (train mode only)
		if args.mode == "train":
			# Build a fresh iterator for test as well when not using DataLoader
			test_iter = test_loader if use_dl else batched(iter(test_ds), args.batch_size)
			test_mse, test_top10, test_rde = eval_epoch(
				model, test_iter, device, amp_enabled=use_amp, amp_dtype=amp_dtype,
				rank_lower=int(args.rank_lower), rank_upper=int(args.rank_upper), compute_rank_err=(bool(args.enable_rank_err))
			)
			print("Final Test Metrics:")
#			print(f"test_mse={test_mse:.6f} test_top10={test_top10:.6f} test_bottom10={test_bot10:.6f} test_rank_disp={test_rde:.3f} test_rank_err={test_rank_err:.4f}")
			print(f"test_mse={test_mse:.6f} test_top10={test_top10:.6f} test_rank_disp={test_rde:.3f}") # test_rank_err={test_rank_err:.4f}")

			# Append final test metrics to a sidecar file next to metrics CSV (include suffix)
			test_metrics_path = os.path.splitext(metrics_file)[0] + "_test.csv"
			with open(test_metrics_path, mode="w", newline="", encoding="utf-8") as f_test:
				writer_test = csv.writer(f_test)
				writer_test.writerow(["metric", "value"])
				writer_test.writerow(["test_mse", test_mse])
				writer_test.writerow(["test_top10_mse", test_top10])
				#writer_test.writerow(["test_bottom10_mse", test_bot10])
				writer_test.writerow(["test_rank_disp", test_rde])
				#writer_test.writerow(["test_rank_err", test_rank_err])

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
					final_activation,
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
#							percent=float(args.simulate_percent),
							top_k=sim_topks_resolved if isinstance(sim_topks_resolved, list) and len(sim_topks_resolved) > 0 else 10,
							host=os.getenv("MYSQL_HOST", "localhost"),
							database=os.getenv("MYSQL_DB", "stockforecasting"),
							user=os.getenv("MYSQL_USER", "Redbeard"),
							password=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"),
							port=int(os.getenv("MYSQL_PORT", "3306")),
							driver="auto",
							loaded_model=model,
							limit=None,
							dates_to_handle=_build_dates_to_handle_for_simulation(),
							prefer_prior_only=bool(args.simulate_prior_only),
						)
						print(f"Simulation complete. Results written to: {out_path}")
					except Exception as sim_e:
						print(f"Warning: simulate_trading failed: {sim_e}")
			except Exception as save_e:
				print(f"Warning: failed to save model artifacts: {save_e}")

	# Execute runs for all combinations
	#
	# When --include-one-feature is supplied with multiple entries, it behaves as
	# an additional sweep dimension: for each hyperparameter combination we run
	# once per selected feature. Treat this as another axis in combo_count so
	# that multi-valued include-one-feature is not mistaken for a single run.
	one_feature_dim = max(1, len(include_one_feature_list))
	combo_count = (
		len(prior_days_list)
		* len(mlp_layers_list)
		* len(lr_list)
		* len(activation_list)
		* len(final_activation_list)
		* len(day_change_centered_list)
		* one_feature_dim
	)

	# In dry-run mode we do not actually execute any training or memory
	# estimation. Instead we iterate over the same combination space that
	# would normally be run and print the resolved arguments for each planned
	# run, including repeats when --run-repeats > 1. This keeps the CLI wiring
	# and combination logic testable without touching the database or creating
	# providers.
	if bool(getattr(args, "dry_run", False)):
		one_feature_values = include_one_feature_list or [None]
		for dc in day_change_centered_list:
			for pd in prior_days_list:
				for ml in mlp_layers_list:
					for lr in lr_list:
						for act in activation_list:
							for f_act in final_activation_list:
								for one_feat in one_feature_values:
									selected_features = set(base_optional_features)
									if one_feat is not None:
										selected_features.add(one_feat)
									_set_feature_flags(selected_features)
									for rep in range(1, int(getattr(args, "run_repeats", 1) or 1) + 1):
										print(
											"DRY-RUN: "
											f"mode={args.mode} "
											f"day_change_centered={str(dc)} "
											f"prior_days={int(pd)} "
											f"mlp_layers={int(ml)} "
											f"lr={float(lr)} "
											f"activation={str(act)} "
											f"final_activation={str(f_act)} "
											f"epochs={int(args.epochs)} "
											f"batch_size={int(args.batch_size)} "
											f"train_percent={float(args.train_percent)} "
											f"val_percent={float(args.val_percent)} "
											f"split_mode={str(args.split_mode)} "
											f"data_head_ignore_percent={float(getattr(args, 'data_head_ignore_percent', 0.0))} "
											f"data_tail_ignore_percent={float(getattr(args, 'data_tail_ignore_percent', 0.0))} "
											f"train_start_date_id={getattr(args, 'train_start_date_id', None)} "
											f"train_end_date_id={getattr(args, 'train_end_date_id', None)} "
											f"run_repeats={int(getattr(args, 'run_repeats', 1) or 1)} "
											f"run_repeat_index={int(rep)} "
											f"include_features={','.join(sorted(selected_features)) if selected_features else ''} "
											f"include_one_feature={one_feat}"
										)
		return 0

	# If there is only a single combination and no explicit range usage, run in-process.
	# Honour --run-repeats by executing the same configuration multiple times.
	if combo_count == 1 and single_run_no_range:
		for rep in range(1, repeat_count + 1):
			setattr(args, "run_repeat_index", int(rep))
			_run_one(
				str(day_change_centered_list[0]),
				int(prior_days_list[0]),
				int(mlp_layers_list[0]),
				float(lr_list[0]),
				str(activation_list[0]),
				str(final_activation_list[0] or None)
			)

		# After the final repeat, if requested, run one additional simulation using
		# ensemble logic across all rep* directories written under the single
		# configuration directory. This mirrors the CLI behaviour in
		# ``simulate_trading.py`` where --ensemble-logic triggers an extra
		# aggregate simulation over all repetitions.
		if (
			bool(getattr(args, "simulate_after", False))
			and repeat_count > 1
			and str(getattr(args, "mode", "train")) == "train"
		):
			try:
				import simulate_trading as _sim
				from StockRecommenderFromModel import StockRecommenderFromModel

				# In the single-run/no-range case, all repeats for this configuration
				# are placed directly under ``base_run_dir`` as ``rep1``, ``rep2``, ...
				config_dir = base_run_dir
				rep_dirs = [
					os.path.join(config_dir, f"rep{idx}")
					for idx in range(1, repeat_count + 1)
					if os.path.isdir(os.path.join(config_dir, f"rep{idx}"))
				]
				if len(rep_dirs) > 1:
					print(
						f"Detected {len(rep_dirs)} rep* directories under {config_dir}; "
						"running additional ensemble simulation with averaged scores "
						"after individual rep simulations."
					)
					# Build recommenders for each rep directory, matching the logic in
					# simulate_trading's CLI entry point.
					ensemble_recs = [StockRecommenderFromModel(d, device=str(args.device)) for d in rep_dirs]
					# Reuse the same top-k parsing as in the per-run simulation.
					sim_topks_resolved = parse_int_list(getattr(args, "simulate_top_k", "10"), "simulate_top_k")
					model_ref = config_dir
					print("Running ensemble simulate_trading on trained repeats...")
					out_path = _sim.simulate(
						model_path=model_ref,
						top_k=sim_topks_resolved if isinstance(sim_topks_resolved, list) and len(sim_topks_resolved) > 0 else 10,
						host=os.getenv("MYSQL_HOST", "localhost"),
						database=os.getenv("MYSQL_DB", "stockforecasting"),
						user=os.getenv("MYSQL_USER", "Redbeard"),
						password=os.getenv("MYSQL_PASSWORD", "3YZs#z5GOJKdea"),
						port=int(os.getenv("MYSQL_PORT", "3306")),
						driver="auto",
						loaded_model=None,
						limit=None,
						prefer_prior_only=bool(args.simulate_prior_only),
						dates_to_handle=_build_dates_to_handle_for_simulation(),
						ensemble_logic=True,
						ensemble_recs=ensemble_recs,
					)
					print(f"Ensemble simulation complete. Results written to: {out_path}")
			except Exception as sim_e:
				print(f"Warning: ensemble simulate_trading failed: {sim_e}")
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
		for dc in day_change_centered_list:
			for pd in prior_days_list:
				for ml in mlp_layers_list:
					for lr in lr_list:
						for act in activation_list:
							for final_act in final_activation_list:
								for rep in range(1, repeat_count + 1):
									setattr(args, "run_repeat_index", int(rep))
									print("I SHOULDNT BE HERE!!!!!\n\n\n")
									_run_one(str(dc), int(pd), int(ml), float(lr), str(act), str(final_act))
		return 0

	def _flag(name: str) -> str:
		return f"--{name.replace('_','-')}"

	# Iterate combinations and spawn a subprocess per *configuration*. The child
	# process is responsible for honouring --run-repeats by executing repeated
	# runs in-process, so we do not loop over repeats here. This ensures that a
	# single timestamped directory is created per configuration, containing
	# rep1/rep2/... subdirectories, matching the in-process behaviour.
	#
	# When --include-one-feature is provided, we further iterate over the parsed
	# list of feature names and enable exactly one of them per sub-run (in
	# addition to any always-on options from --include-features).
	one_feature_values = include_one_feature_list or [None]
	for dc in day_change_centered_list:
		for pd in prior_days_list:
			for ml in mlp_layers_list:
				for lr in lr_list:
					for final_act in final_activation_list:
						for act in activation_list:
							for one_feat in one_feature_values:
								# Compute the per-run feature set: base optional features plus
								# exactly one additional feature (if requested).
								selected_features = set(base_optional_features)
								if one_feat is not None:
									selected_features.add(one_feat)
								_set_feature_flags(selected_features)

								cmd: List[str] = [sys.executable, wrapper_path]
								# Core single-valued overrides for this run
								cmd += [_flag("mode"), str(args.mode)]
								cmd += [_flag("prior-days"), str(int(pd))]
								cmd += [_flag("mlp-layers"), str(int(ml))]
								cmd += [_flag("lr"), str(float(lr))]
								cmd += [_flag("activation"), str(act)]
								cmd += [_flag("final-activation"), str(final_act)]
								# Preserve any explicit training date window so that child
								# processes operate on the same range. When date IDs are not
								# provided, split_datasets will derive them from the
								# head/tail ignore fractions as before.
								if getattr(args, "train_start_date_id", None) is not None:
									cmd += [
										_flag("train-start-date-id"),
										str(getattr(args, "train_start_date_id")),
									]
								if getattr(args, "train_end_date_id", None) is not None:
									cmd += [
										_flag("train-end-date-id"),
										str(getattr(args, "train_end_date_id")),
									]
								# Communicate the total repeat count to the child so it can
								# create rep* subdirectories appropriately.
								cmd += [_flag("run-repeats"), str(int(repeat_count))]
								# Pass-through of other options
								cmd += [_flag("epochs"), str(int(args.epochs))]
								cmd += [_flag("batch-size"), str(int(args.batch_size))]
								cmd += [_flag("metrics-file"), str(args.metrics_file)]
								cmd += [_flag("device"), str(args.device)]
								if args.limit is not None:
									cmd += [_flag("limit"), str(int(args.limit))]
								cmd += [_flag("hidden-layers"), str(int(args.hidden_layers))]
								cmd += [_flag("log-interval"), str(int(args.log_interval))]
								# Preserve the unified include-features argument so that child
								# processes reconstruct the same feature configuration. We
								# reconstruct the CLI string from the selected features for this
								# sub-run so that each child sees only its own feature set.
								if selected_features:
									run_include_str = ",".join(sorted(selected_features))
									cmd += [
										_flag("include_features"),
										run_include_str,
									]
								cmd += [_flag("rank-lower"), str(int(args.rank_lower))]
								cmd += [_flag("rank-upper"), str(int(args.rank_upper))]
								if bool(args.use_ranking_error_loss):
									cmd += [_flag("use-ranking-error-loss")]
									cmd += [_flag("rank-temp"), str(float(args.rank_temp))]
								if bool(args.enable_rank_err):
									cmd += [_flag("enable-rank-err")]
								cmd += [_flag("split-mode"), str(args.split_mode)]
								if args.seed is not None:
									cmd += [_flag("seed"), str(int(args.seed))]
								cmd += [_flag("precision"), str(args.precision)]
								cmd += [_flag("arch"), str(args.arch)]
								# Pass the specific day_change_centered value for this run so that
								# the child process sees a single choice (no commas).
								# NOTE: Use the argparse *destination* name (day_change_centered)
								# here so that _flag() correctly converts it to the CLI option
								# "--day-change-centered". Passing the dashed form directly would
								# result in an invalid flag like "---day-change-centered" and the
								# subprocess would never receive the intended value.
								cmd += [_flag("day_change_centered"), str(dc)]
								# Ensure all subprocess runs spawned from this compound invocation
								# share the same initial_run identifier so that they can be grouped
								# together in comparison tools.
								cmd += [_flag("initial_run"), str(initial_run_id)]
								if bool(args.dont_use_dataloader):
									cmd += [_flag("dont-use-dataloader")]
								if bool(getattr(args, "shuffle", False)):
									cmd += [_flag("shuffle")]
								if int(getattr(args, "num_workers", 0)):
									cmd += [_flag("num-workers"), str(int(args.num_workers))]
							if bool(getattr(args, "pin_memory", False)):
								cmd += [_flag("pin-memory")]
							if bool(args.simulate_after):
								cmd += [_flag("simulate-after")]
							cmd += [_flag("simulate-percent"), str(float(args.simulate_percent))]
							# Ensure the child process sees the desired repeat count so that it
							# can create rep1/rep2/... subdirectories when run_repeats > 1.
							repeat_count_child = int(getattr(args, "run_repeats", 1) or 1)
							if repeat_count_child > 1:
								cmd += [_flag("run_repeats"), str(repeat_count_child)]
							# Periodic trading interval for in-training simulation (if any)
							if int(getattr(args, "trading_interval", 0) or 0) > 0:
								cmd += [
									# NOTE: use the argparse destination name "trading_interval" here so
									# that _flag() correctly converts it to the CLI option
									# "--trading-interval". Passing "trading-interval" directly would
									# result in the invalid flag "---trading-interval" and the
									# subprocess would never see the intended trading interval.
									_flag("trading_interval"),
									str(int(getattr(args, "trading_interval", 0) or 0)),
								]
								# simulate_top_k might be comma-separated; pass through original to preserve intent
								cmd += [_flag("simulate-top-k"), str(getattr(args, "simulate_top_k", "10"))]
								if bool(args.simulate_prior_only):
									cmd += [_flag("simulate-prior-only")]

								print(
									f"Spawning isolated run: day_change_centered={dc} prior_days={pd} "
									f"mlp_layers={ml} lr={lr} activation={act}",
								)
								try:
									completed = subprocess.run(cmd, check=True)
									if completed.returncode != 0:
										print(
											f"Warning: sub-run exited with code {completed.returncode}",
										)
								except Exception as sub_e:
									print(f"Error running isolated sub-process: {sub_e}")

						return 0


class TrainPytorchNetwork:
	"""Class wrapper around the training entry point.

	Typical usage from another module::

		from train_pytorch_network import TrainPytorchNetwork

		runner = TrainPytorchNetwork([
			"--mode=train",
			"--prior-days=4",
			"--epochs=10",
		])
		exit_code = runner.run()
		run_dir = runner.get_training_directory()

	The class simply delegates to the original CLI implementation while
	recording the timestamped run directory that was created.
	"""

	def __init__(self, argv: Optional[List[str]] = None) -> None:
		self._argv = argv
		self._exit_code: Optional[int] = None
		self._training_directory: Optional[str] = None

	def run(self) -> int:
		"""Execute training with the provided arguments.

		Returns the same integer exit code as the original :func:`main`.
		"""
		global LAST_RUN_BASE_DIR
		LAST_RUN_BASE_DIR = None
		self._exit_code = _main_impl(self._argv)
		self._training_directory = LAST_RUN_BASE_DIR
		return int(self._exit_code or 0)

	def get_training_directory(self) -> Optional[str]:
		"""Return the base run directory created for this invocation.

		In the common single-run case this is the timestamped directory of the
		form ``<metrics_dir>/<timestamp>`` under which all configuration- and
		repeat-specific subdirectories are created.
		"""
		return self._training_directory

	@property
	def training_directory(self) -> Optional[str]:
		"""Convenience property mirroring :meth:`get_training_directory`."""
		return self.get_training_directory()


def main(argv: Optional[List[str]] = None) -> int:
	"""CLI entry point.

	This thin wrapper constructs :class:`TrainPytorchNetwork` and invokes
	:py:meth:`TrainPytorchNetwork.run`, preserving the existing public
	interface for scripts that still call :func:`main` directly.
	"""
	runner = TrainPytorchNetwork(argv)
	return runner.run()


if __name__ == "__main__":  # pragma: no cover - script entry point
	raise SystemExit(main())
