"""Legacy open-to-close executor; new fitting lives in stockguidance.training."""

from __future__ import annotations

import argparse
import csv
import gc
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Iterator

import psutil
import torch
from torch import nn, Tensor
from torch.cuda.amp import GradScaler
from torch.nn import Module
from torch.optim import Optimizer
from torch.optim.lr_scheduler import ExponentialLR
from torch.utils.data import DataLoader

from StockRecommenderFromModel import StockRecommenderFromModel
from trainer.ai_type import AIType
from trainer.dataloaders import build_dataloaders
from trainer.datasets import IndexDataset
from trainer.factory import create_model_and_optimizer
from trainer.file_structure_single_run_handler import FileStructureSingleRunHandler
from trainer.loss_functions.losses import ranking_surrogate_loss, mse_restricted
from trainer.metrics import eval_epoch
from trainer.rep_handler import RepHandler, _rep_queue, _rep_worker_logic
from trainer.splits import split_datasets
from trainer.state.family_run import FamilyRun
from trainer.state.single_run import SingleRun
from trainer.torch_interface.compat import TORCH_AVAILABLE
from trainer.torch_interface.device import setup_device_and_precision
from trainer.training_bundle import TrainingBundle
from trainer.util.utils import count_elements
from training_instance_provider import TrainingInstanceProvider

#torch.set_float32_matmul_precision("high")
#import torch_tensorrt
#import apache_tvm
#print(torch.compiler.list_backends())

criterion = nn.L1Loss()
#criterion = nn.MSELoss()


def backprop_loss_non_amp(xb=None, yb=None, model: nn.Module = None, family_run: FamilyRun = None,
                          optimizer: Optimizer = None,
                          batch_idx=None) -> Tuple[torch.Tensor, torch.Tensor, int | None]:
	xb, yb = prep_vars(xb, yb)
	pred = model_and_clean(model, xb)

	loss = set_loss_method(family_run, pred, yb)
	#		loss = torch.mean((pred - yb.to(pred.dtype)) ** 2)
	batch_idx = opt_zero_grad(loss,batch_idx, optimizer)
	if batch_idx is not None:
		return pred, loss, batch_idx
	do_the_back_prop(loss)

	# Optional: log gradient norm if pred was all zeros or periodically
	# if torch.all(pred == 0):
	# 	total_norm = 0.0
	# 	for p in model.parameters():
	# 		if p.grad is not None:
	# 			param_norm = p.grad.data.norm(2)
	# 			total_norm += param_norm.item() ** 2
	# 	total_norm = total_norm ** 0.5
	# 	print(f"  DEBUG: grad_norm={total_norm:.8f}")
	#
	back_non_amp_optim(optimizer)
	return pred, loss, None


def do_the_back_prop(loss: Tensor):
	loss.backward()


def opt_zero_grad(loss,batch_idx, optimizer: Optimizer | None) -> Any:
	if not torch.isfinite(loss):
		optimizer.zero_grad(set_to_none=True)
		batch_idx += 1
		return batch_idx
	else :
		return None


def set_loss_method(family_run: FamilyRun | None, pred: Tensor, yb: Tensor) -> Tensor:
	if "cuda" not in str(pred.device):
		print("not doing loss calc on cuda")
	if bool(family_run.use_ranking_error_loss):
		loss = ranking_surrogate_loss(pred, yb.to(pred.dtype), temperature=float(family_run.rank_temperature))
	else:
		loss = criterion(pred, yb.to(pred.dtype))
	return loss


def model_and_clean(model: Module | None, xb: Tensor) -> Tensor:
	pred = model(xb)
	if not torch.isfinite(pred).all():
		print("  WARNING: Model output contains NaNs or Infs during training!")
	pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
	return pred


def prep_vars(xb, yb) -> tuple[Tensor, Tensor]:
	xb = torch.nan_to_num(xb, nan=0.0, posinf=0.0, neginf=0.0)
	yb = torch.nan_to_num(yb, nan=0.0, posinf=0.0, neginf=0.0)
	return xb, yb

def back_non_amp_optim(optimizer: Optimizer | None):
	optimizer.step()
	optimizer.zero_grad(set_to_none=True)




def _maybe_generate_scores_for_rep(
	*,
	family_run: FamilyRun,
	single_run: SingleRun,
	rep_index: int,
	model: nn.Module | str,
	epoch: int,
	test_sim_data_source: IndexDataset,
	input_size: int,
	output_size: int,
) -> None:
	trading_interval = family_run.trading_interval
	if trading_interval > 0 and epoch % trading_interval == 0:
		# Use the same device as the model's current location if possible, or auto
		# But we know the model was just moved TO device before this call in the main loop
		device_to_use = str(model.parameters().__next__().device) if not isinstance(model, str) else "auto"
		stock_recommender = StockRecommenderFromModel(
			single_run,
			[model],
			single_run.file_structure.get_directory_for_rep(rep_index),
			model_dict={"input_size": int(input_size), "output_size": int(output_size),
			            "prior_days": int(single_run.prior_days),
			            "day_change_centered": single_run.day_change_centered},
			device=device_to_use,
		)
		print(
			f"Running periodic score generation at epoch {epoch} "
			f"(interval={trading_interval}) for rep {rep_index} ..."
		)

		from debug_feature_logging import dump_feature_table, dump_label_table
		feature_label_date_gen = test_sim_data_source.get_next_feature_label_date()
		# We use the internal model of stock_recommender to ensure it's loaded if it was a path
		network = stock_recommender._models[0]

		# Get the provider from the dataset to access its internal data
		provider = test_sim_data_source.provider

		# Table printing should probably only happen for the first date in the generator
		# or we can pass a flag. Let's do it once per periodic score generation.
		printed_table = False

		for feat_vec, _, feature_date_id in feature_label_date_gen:
			if not printed_table:
				# Find the index of feature_date_id in provider's dates
				dates = provider.cumulative_dates_for_train_validation_and_test_sets
				matches = torch.where(dates == int(feature_date_id))[0]
				if matches.numel() > 0:
					base_idx = int(matches[0])

					# Reconstruct feature names
					feature_names = ["change_in_price_over_day"]
					if single_run.current_day_change:
						feature_names.append("change_in_price_over_day_current")
					if single_run.volume_ratio:
						feature_names.append("volume_ratio")
					if single_run.vwap_ratio:
						feature_names.append("vwap_ratio")
					if single_run.tranaction_ratio:
						feature_names.append("transaction_ratio")

					# F in provider is ticker_feature_quantity
					F = provider.ticker_feature_quantity
					if len(feature_names) < F:
						for i in range(len(feature_names), F):
							feature_names.append(f"F{i}")
					elif len(feature_names) > F:
						feature_names = feature_names[:F]

					from debug_feature_logging import dump_feature_table, dump_label_table
					dump_feature_table(
						X_flat=provider.ticker_data_list_flat.cpu().numpy(),
						dates=dates.cpu().numpy(),
						d=base_idx,
						num_prior=int(single_run.prior_days),
						feature_names=feature_names,
						header_note=f"Epoch {epoch} Periodic Check"
					)
					dump_label_table(
						labels=provider.labels_for_training.cpu().numpy(),
						dates=dates.cpu().numpy(),
						d=base_idx,
						num_prior=int(single_run.prior_days),
						header_note=f"Epoch {epoch} Periodic Check"
					)
					printed_table = True

			input_tensor = torch.nan_to_num(feat_vec, nan=0.0, posinf=0.0, neginf=0.0).unsqueeze(0).to(device_to_use)
			stock_recommender.generateStockScoresForNetwork(
				network=network,
				inputs=input_tensor,
				save_to_file=True,
				epoch_name=str(epoch),
				date_id=feature_date_id,
				network_index=rep_index - 1
			)
			del input_tensor
		del feature_label_date_gen
		del network
		stock_recommender.to_cpu()
		del stock_recommender
		gc.collect()
		if str(device_to_use).startswith("cuda"):
			torch.cuda.empty_cache()

def _prepare_data(provider: TrainingInstanceProvider) -> tuple[int, int]:
	"""Return (input_size, output_size) derived from the first instance."""
	x0, y0, date_id_extra = provider.get_feature_and_label_pair(1)
	input_size = count_elements(x0)
	output_size = count_elements(y0)
	return input_size, output_size


def _resolve_train_window_and_split(
	provider: TrainingInstanceProvider,
	max_training_instances: int,
	single_run: SingleRun,
) -> tuple[IndexDataset, IndexDataset, IndexDataset, int, int]:
	"""Resolve train window (start/end date_id) and split datasets.

	Returns (train_ds, val_ds, test_ds, train_validation_test_sets_start_date_id, train_validation_test_sets_end_date_id).
	"""
	print("train start date ", single_run.train_validation_test_sets_start_date_id)
	rnd_duration = getattr(single_run.family_run, "random_training_duration", None)
	if rnd_duration is not None:
		raise ValueError("Random duration isn't working, fix it later")
	# 	try:
	# 		dur = int(rnd_duration)
	# 	except Exception:
	# 		dur = None
	# 	if dur is not None and dur >= 0:
	# 		# Compute max allowed start index based on duration
	# 		max_start_index = database_date_id_queries.get_offset_date_id(base_value=single_run.train_validation_test_sets_end_date_id, offset=dur)
	#
	# 		if max_start_index < single_run.train_validation_test_sets_start_date_id:
	# 			raise ValueError(
	# 				f"--random-training-duration={dur} is too large for available dates ({single_run.train_validation_test_sets_end_date_id}); "
	# 				"requires at least duration+1 dates."
	# 			)
	# 		# Use provided seed for determinism across runs, if given
	# 		import random as _random
	# 		seed_val = getattr(single_run.family_run, "random_seed_splits", None)
	# 		_rng = _random.Random(int(seed_val)) if seed_val is not None else _random
	# 		start_idx = _rng.randint(0, max_start_index)
	# 		end_idx = start_idx + dur
	# 		#single_run.train_validation_test_sets_start_date_id =
	# 		#single_run.train_validation_test_sets_end_date_id =
	# 		print(
	# 			f"Random training window selected: start_index={start_idx} end_index={end_idx} "
	# 			f"start_date_id={single_run.train_validation_test_sets_start_date_id} end_date_id={single_run.train_validation_test_sets_end_date_id} duration={dur}"
	# 		)

	train_ds, val_ds, test_ds = split_datasets(
		provider,
		single_run.family_run,
		max_training_instances,
		train_validation_and_test_set_start_date_id=single_run.train_validation_test_sets_start_date_id,
		train_validation_and_test_set_end_date_id=single_run.train_validation_test_sets_end_date_id,
		train_percent=single_run.family_run.train_percent,
		validation_percent=single_run.family_run.val_percent,
	)
	print(
		f"Datasets: train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}  "
		f"train start date_id={single_run.train_validation_test_sets_start_date_id} train end date_id={single_run.train_validation_test_sets_end_date_id}"
	)
	return train_ds, val_ds, test_ds, (
		int(single_run.train_validation_test_sets_start_date_id) if single_run.train_validation_test_sets_start_date_id is not None else 0), (
		int(single_run.train_validation_test_sets_end_date_id) if single_run.train_validation_test_sets_end_date_id is not None else 0)


def _save_bundle_to_cache(bundle: TrainingBundle, m, opt, sc, cache_path: str):
	"""Background task to save model/optimizer/scaler to disk."""
	checkpoint = {
		'model_state_dict': m.state_dict(),
		'optimizer_state_dict': opt.state_dict(),
		'scaler_state_dict': sc.state_dict() if sc is not None else None,
	}
	torch.save(checkpoint, cache_path)


# After saving, we can't easily null out the bundle fields here because
# it might be currently in use or the main thread might want to do it.
# We'll let the main thread handle the reference clearing.


def _build_training_bundles_and_loaders(
	family_run: FamilyRun,
	single_run: SingleRun,
	input_size: int,
	output_size: int,
	train_ds,
	val_ds,
	test_ds,
	device,
) -> tuple[List[TrainingBundle], List[FileStructureSingleRunHandler], bool, Optional[torch.dtype], int]:
	"""Create models/optimizers/loaders for each repeat and return bookkeeping."""

	# Pre-build loaders to share them across all bundles if they are identical
	train_loader_shared = build_dataloaders(family_run, train_ds)
	val_loader_shared = build_dataloaders(family_run, val_ds)

	training_bundle_list: List[TrainingBundle] = []
	param_count = 0
	use_amp = False
	amp_dtype = None
	repeat_count_cfg = family_run.run_repeats

	# Determine if we should use caching based on memory usage
	total_mem = psutil.virtual_memory().total

	for run_idx in range(repeat_count_cfg):
		m, opt, sc, inner_use_amp, inner_amp_dtype = create_model_and_optimizer(
			family_run,
			single_run,
			input_size,
			output_size,
			single_run.mlp_layers,
			single_run.activation,
			device,
			single_run.lr,
			to_device=False,
			prior_days=single_run.prior_days,
			optimizer_name=single_run.optimizer,
			final_activation=single_run.final_activation,
		)
		use_amp = inner_use_amp
		amp_dtype = inner_amp_dtype
		one_training_bundle = TrainingBundle(model=m, optimizer=opt, scaler=sc, use_amp=inner_use_amp,
		                                     amp_dtype=inner_amp_dtype)
		training_bundle_list.append(one_training_bundle)
		param_count = sum(p.numel() for p in m.parameters())

		# Check memory after creation
		current_rss = psutil.Process(os.getpid()).memory_info().rss
		percent_of_memory_to_allow = 0.2
		print(f"\n\n\nMemory check for caching switch {current_rss / (1024 ** 3):.2f}GB\n\n\n")
		cache_dir = os.path.join(family_run.file_structure.family_dir, "cache", "models")
		os.makedirs(cache_dir, exist_ok=True)
		cache_path = os.path.join(cache_dir, f"model_rep_{run_idx + 1}.pt")
		one_training_bundle.cache_path = cache_path

		# Submit background save
		_save_bundle_to_cache(one_training_bundle, m, opt, sc, cache_path)

		# Clear references to save memory immediately in main thread
		one_training_bundle.model = None
		one_training_bundle.optimizer = None
		one_training_bundle.scaler = None
		del m, opt, sc
		gc.collect()

		# Build batch providers (DataLoader or simple generator).
		one_training_bundle.training_instance_provider = train_loader_shared
		one_training_bundle.validation_instance_provider = val_loader_shared
		one_training_bundle.testing_instance_provider = test_ds

		# Use per-run learning rate adjustment parameter (scheduler gamma), defaulting to 0.998
		gamma: float = getattr(single_run, "learning_rate_adjustment_parameter", 0.998)

		# If cached, we'll need to instantiate optimizer later or handle scheduler carefully.
		if one_training_bundle.optimizer is not None:
			one_training_bundle.scheduler = ExponentialLR(one_training_bundle.optimizer, gamma=float(gamma))
		else:
			one_training_bundle.scheduler = None  # Will be created on load

	if use_amp and amp_dtype == torch.float16:
		print("Precision runtime: autocast=float16 with GradScaler")
	elif use_amp and amp_dtype == torch.bfloat16:
		print("Precision runtime: autocast=bfloat16 (no GradScaler)")
	else:
		print("Precision runtime: float32 (no autocast)")
	print(f"Model parameters: {param_count:,}")

	rep_files_list: List[FileStructureSingleRunHandler] = []
	for run_idx in range(repeat_count_cfg):
		fs = single_run.file_structure
		training_bundle_list[run_idx].suffix = fs.suffix
		rep_files_list.append(fs)
	return training_bundle_list, rep_files_list, use_amp, amp_dtype, param_count




def _log_gpu_usage(device, epoch: int, single_run: SingleRun) -> None:
	try:
		if torch.cuda.is_available():
			free_mem, total_mem = torch.cuda.mem_get_info(device)
			used_mem = torch.cuda.memory_allocated(device)

			run_dir_log = getattr(single_run.file_structure, "run_directory", None)
			if not run_dir_log:
				run_dir_log = single_run.file_structure.run_group_dir

			if run_dir_log:
				log_path = os.path.join(run_dir_log, "gpu_usage.csv")
				file_exists = os.path.exists(log_path)
				with open(log_path, "a", newline="") as f:
					writer = csv.writer(f)
					if not file_exists:
						writer.writerow(["timestamp", "epoch", "allocated_bytes", "used_total_bytes", "free_bytes", "total_bytes", "usage_percent"])

					used_total_bytes = total_mem - free_mem
					usage_percent = (used_total_bytes / total_mem) * 100
					writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), epoch, used_mem, used_total_bytes, free_mem, total_mem, f"{usage_percent:.2f}%"])
	except Exception as log_e:
		print(f"Warning: GPU logging failed: {log_e}")


def backprop_loss_for_amp(xb=None, yb=None, model: nn.Module = None, family_run: FamilyRun = None,
                          optimizer: Optimizer = None,
                          batch_idx=None, amp_dtype=None, scaler=None):
	with torch.amp.autocast("cuda", dtype=amp_dtype):  # type: ignore[attr-defined]
		xb = torch.nan_to_num(xb, nan=0.0, posinf=0.0, neginf=0.0)
		yb = torch.nan_to_num(yb, nan=0.0, posinf=0.0, neginf=0.0)
		pred = model(xb)
		if not torch.isfinite(pred).all():
			print(f"  WARNING: Model output contains NaNs or Infs at batch {batch_idx}!")
		pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
		if bool(family_run.use_ranking_error_loss):
			loss = ranking_surrogate_loss(pred, yb.to(pred.dtype), temperature=float(family_run.rank_temperature))
		else:
			loss = criterion(pred, yb.to(pred.dtype))

	if not torch.isfinite(loss):
		optimizer.zero_grad(set_to_none=True)
		batch_idx += 1
		return pred, loss, batch_idx
	elif amp_dtype == torch.float16:
		scaler.scale(loss).backward()

		scaler.step(optimizer)
		scaler.update()
		optimizer.zero_grad(set_to_none=True)
	else:
		loss.backward()

		optimizer.step()
		optimizer.zero_grad(set_to_none=True)
	return pred, loss, None


def _train_one_epoch_for_rep(
	*,
	model: nn.Module,
	optimizer: Optimizer,
	scaler: torch.cuda.amp.GradScaler,
	use_amp: bool,
	amp_dtype,
	train_loader: torch.utils.data.DataLoader,
	val_loader: torch.utils.data.DataLoader,
	device,
	single_run: SingleRun,
	epoch: int,
) -> tuple[float, float, float, float, dict[str, dict[str, int | float] ]]:
	"""Run training for one epoch for a single repetition and return metrics."""
	model.train()
	total = 0
	train_loss_sum = 0.0
	train_top10_sum = 0.0
	non_blocking = bool(single_run.family_run.pin_GPU_memory)
	counts = {"label_pos": 0, "label_neg": 0, "label_zero": 0, "pred_pos": 0, "pred_neg": 0, "pred_zero": 0}

	running_loss_sum = [0.0]
	total, train_loss_sum, train_top10_sum = _train_one_epoch_for_rep_for_loop(amp_dtype, device,
	                                                                           epoch, single_run, model, non_blocking,
	                                                                           optimizer, running_loss_sum, scaler,
	                                                                           total, train_loader,
	                                                                           train_loss_sum, train_top10_sum, use_amp, counts)

	# Explicitly clear tensors to help GC
	#del xb, yb, pred, loss

	train_mse, train_top10 = _train_one_epoch_for_rep_calc_mse_top10(total, train_loss_sum, train_top10_sum)

	# Force GC and clear CUDA cache at the end of the epoch
	#	if torch.cuda.is_available():
	#		torch.cuda.empty_cache()
	#	gc.collect()
	#time.sleep(0.1) # Small sleep to let OS/drivers catch up

	val_mse = 0.0
	val_top10 = 0.0
	val_counts = {"label_pos": 0, "label_neg": 0, "label_zero": 0, "pred_pos": 0, "pred_neg": 0, "pred_zero": 0}
	if val_loader is not None:
		val_mse, val_top10, _val_rde_unused, val_counts = eval_epoch(
			model,
			val_loader,
			device,
			amp_enabled=use_amp,
			amp_dtype=amp_dtype,
			rank_lower=single_run.family_run.rank_error_lower_bound,
			rank_upper=single_run.family_run.rank_error_upper_bound,
			compute_rank_err=bool(single_run.family_run.enable_rank_error_loss_in_training),
			compute_rank_disp=False,
			pin_memory=non_blocking,
		)
	else:
		val_mse, val_top10 = 0.0, 0.0

	all_counts = {
		"train": counts,
		"val": val_counts
	}
	return train_mse, val_mse, train_top10, val_top10, all_counts


def _get_next_batch(iterator: Iterator) -> tuple[Tensor, Tensor]:
	return next(iterator)

def _train_one_epoch_for_rep_for_loop(amp_dtype, device, epoch: int, single_run: SingleRun,
                                      model: Module, non_blocking: bool, optimizer: Optimizer,
                                      running_loss_sum: list[float], scaler: GradScaler, total: int,
                                      train_loader: DataLoader, train_loss_sum: float, train_top10_sum: float,
                                      use_amp: bool, counts: dict[str, int]) -> tuple[int, float, float]:
	batch_idx = 0
	total_batches = len(train_loader) if hasattr(train_loader, "__len__") else 0
	iterator = iter(train_loader)
	while True:
		try:
			xb, yb = iterator.__next__()
		except StopIteration:
			break
		total, train_loss_sum, train_top10_sum = _train_one_epoch_for_rep_for_loop_body(amp_dtype, batch_idx,
		                                                                                device, epoch,
		                                                                                single_run, model, non_blocking,
		                                                                                optimizer, running_loss_sum,
		                                                                                scaler, total, total_batches,
		                                                                                train_loss_sum, train_top10_sum,
		                                                                                use_amp, xb, yb, counts)
	return total, train_loss_sum, train_top10_sum

def _train_one_epoch_for_rep_for_loop_body(amp_dtype, batch_idx: int | Any, device, epoch: int,
                                           single_run: SingleRun, model: Module, non_blocking: bool,
                                           optimizer: Optimizer, running_loss_sum: list[float], scaler: GradScaler,
                                           total: int, total_batches: int, train_loss_sum: float,
                                           train_top10_sum: float, use_amp: bool, xb, yb, counts: dict[str, int]) -> tuple[int, float, float]:
	xb, yb = prepare_training_inputs(device, non_blocking, xb, yb)
	batch_size = xb.size(0)
	# optimizer.zero_grad(set_to_none=True) # Moved to after step()
	short_circuit_execution = False
	if use_amp:
		pred, loss, batch_index_update = backprop_loss_for_amp(xb=xb, yb=yb, model=model, family_run=single_run.family_run,
		                                                       optimizer=optimizer, batch_idx=batch_idx,
		                                                       amp_dtype=amp_dtype, scaler=scaler)
		if batch_index_update is not None:
			batch_idx = batch_index_update
			# Explicitly clear tensors to help GC
			# del xb, yb, pred, loss
			short_circuit_execution = True
	else:
		pred, loss, batch_index_update = backprop_loss_non_amp(xb=xb, yb=yb, model=model, family_run=single_run.family_run,
		                                                       optimizer=optimizer, batch_idx=batch_idx)
		if batch_index_update is not None:
			batch_idx = batch_index_update
			# Explicitly clear tensors to help GC
			# del xb, yb, pred, loss
			short_circuit_execution = True

	# Detach and move to CPU to avoid keeping GPU memory
	# loss_detached = loss.detach().cpu()
	# pred_detached = pred.detach().cpu()
	# yb_detached = yb.detach().cpu()
	if not short_circuit_execution:
		train_loss_sum, train_top10_sum = calculate_training_statistics(train_loss_sum, train_top10_sum,
		                                                                batch_size, batch_idx + 1, epoch, single_run,
		                                                                loss, pred,
		                                                                total_batches, yb, total, running_loss_sum, counts
		                                                                )
		# train_loss_sum += l_contrib
		# train_top10_sum += t_contrib

		total += batch_size
		batch_idx += 1
	return total, train_loss_sum, train_top10_sum


def _train_one_epoch_for_rep_calc_mse_top10(total: int, train_loss_sum: int | float, train_top10_sum: int | float) -> \
tuple[float, float]:
	train_mse = train_loss_sum / max(1, total)
	train_top10 = train_top10_sum / max(1, total)
	return train_mse, train_top10


def calculate_training_statistics(train_loss_sum:float, train_top10_sum,   batch_size: float, batch_idx: int, epoch: int, single_run: SingleRun,
                                  loss_tensor: Tensor, pred: Tensor, total_batches: int,
                                  yb: Tensor, current_total: int, running_loss_sum: list[float], counts: dict[str, int]) -> tuple[float, float]:
	bs = batch_size
	loss_contribution, loss_val, top10_contribution, batch_counts = calc_loss_contrib_top10(bs, loss_tensor, pred, yb)

	if single_run.family_run.log_interval and single_run.family_run.log_interval > 0 and (
		epoch % single_run.family_run.log_interval == 0 or epoch == single_run.epochs):
		avg_loss_so_far = (running_loss_sum[0] + loss_contribution) / max(1.0, float(current_total) + bs)
		print(
			f"Epoch {epoch}/{single_run.epochs} | batch {batch_idx}/{total_batches} | curr_loss={loss_val:.6f} | avg_loss={avg_loss_so_far:.6f}")

	train_loss_sum, train_top10_sum = sum_loss_training_stats(loss_contribution, running_loss_sum, top10_contribution,
	                                                          train_loss_sum, train_top10_sum, counts, batch_counts)
	return train_loss_sum, train_top10_sum


def sum_loss_training_stats(loss_contribution: float, running_loss_sum: list[float], top10_contribution: float,
                            train_loss_sum: float, train_top10_sum, counts: dict[str, int], batch_counts: dict[str, int]) -> tuple[float, Any]:
	running_loss_sum[0] += loss_contribution
	train_loss_sum += loss_contribution
	train_top10_sum += top10_contribution
	for k, v in batch_counts.items():
		counts[k] += v
	return train_loss_sum, train_top10_sum

def calc_loss_contrib_top10(bs: float, loss_tensor: Tensor, pred: Tensor, yb: Tensor) -> tuple[
	float, int | float | bool, float, dict[str, int]]:
	loss_val = loss_tensor.item()
	loss_contribution = loss_val * bs
	if "cuda" not in str(pred.device) or "cuda" not in str(yb.device) or "cuda" not in str(loss_tensor.device) :
		print("not doing loss_contrib_top10_calc on cuda")
		print(f"calc_loss_contrib_top10 {loss_tensor.device} {pred.device} {yb.device}")
	top10_contribution = mse_restricted(pred, yb.to(pred.dtype), mode="top", k=10).item() * bs

	with torch.no_grad():
		label_pos = (yb > 0).sum().item()
		label_neg = (yb < 0).sum().item()
		label_zero = (yb == 0).sum().item()
		pred_pos = (pred > 0).sum().item()
		pred_neg = (pred < 0).sum().item()
		pred_zero = (pred == 0).sum().item()

	batch_counts = {
		"label_pos": int(label_pos),
		"label_neg": int(label_neg),
		"label_zero": int(label_zero),
		"pred_pos": int(pred_pos),
		"pred_neg": int(pred_neg),
		"pred_zero": int(pred_zero)
	}

	return loss_contribution, loss_val, top10_contribution, batch_counts

def prepare_training_inputs(device, non_blocking: bool, xb, yb) -> tuple[Tensor, Tensor]:
	if not isinstance(xb, torch.Tensor):
		xb = torch.as_tensor(xb, dtype=torch.float32, device=device)
	else:
		#slow
		xb = xb.to(device=device, dtype=torch.float32, non_blocking=non_blocking)
	if not isinstance(yb, torch.Tensor):
		yb = torch.as_tensor(yb, dtype=torch.float32, device=device)
	else:
		yb = yb.to(device=device, dtype=torch.float32, non_blocking=non_blocking)
	return xb, yb


class TrainSingleRun:
	"""Execute a single training configuration.

	This class encapsulates the former `_run_one` logic from
	`train_pytorch_network` so that it can be reused both from the family
	controller and from a dedicated CLI that runs a single saved
	configuration.
	"""

	def __init__(self, single_run: SingleRun, ai_type: AIType = AIType.SINGLE) -> None:
		self.single_run = single_run
		self.ai_type: AIType = ai_type
		self._providers: Dict[str, TrainingInstanceProvider] = {}

	def _get_provider(self, day_centered: str) -> TrainingInstanceProvider:
		family_run = self.single_run.family_run
		if day_centered not in self._providers:
			# Cache directory under the family_run directory
			cache_dir = None
			if family_run.file_structure and family_run.file_structure.family_dir:
				cache_dir = os.path.join(family_run.file_structure.family_dir, "cache")

			self._providers[day_centered] = TrainingInstanceProvider(
				use_torch=TORCH_AVAILABLE,
				limit=family_run.limit,
				ticker_limit=family_run.ticker_limit,
				current_day_change=family_run.current_day_change,
				volume_ratio=family_run.volume_ratio,
				vwap_ratio=family_run.vwap_ratio,
				tranaction_ratio=family_run.transaction_ratio,
				day_change_centered=day_centered,
				start_date_id=self.single_run.train_validation_test_sets_start_date_id,
				end_date_id=self.single_run.train_validation_test_sets_end_date_id,
				device="cpu",
				cache_dir=cache_dir
			)
		return self._providers[day_centered]

	# ----------------------------
	# Small helper extractions
	# ----------------------------

	def run_one(self, save_interval: int = 500, rep_index: int | None = None) -> None:
		"""Core training loop for a single `SingleRun` configuration."""

		family_run = self.single_run.family_run
		single_run = self.single_run
		print(
			"ai_type ",
			self.ai_type.value,
			"day_centered ",
			single_run.day_change_centered,
			"prior_days ",
			single_run.prior_days,
			"mlp_layers ",
			single_run.mlp_layers,
			"lr ",
			single_run.lr,
			"optimizer ",
			single_run.optimizer,
			"activation ",
			single_run.activation,
			"final_activation ",
			single_run.final_activation,
		)
		provider = self._get_provider(single_run.day_change_centered)
		if family_run.ticker_ids is None:
			family_run.ticker_ids = provider.ticker_ids
		if single_run.ticker_ids is None:
			single_run.ticker_ids = provider.ticker_ids
		print(f"Provider: {provider}")
		# Set the sliding window size for this configuration
		provider.set_how_many_days_in_past_to_use(int(single_run.prior_days))

		max_training_instances = provider.get_maximum_number_of_test_train_instances()
		if max_training_instances <= 1:
			raise RuntimeError("Not enough instances to train. Consider reducing --prior-days or loading more data.")

		# Determine input/output sizes from the first instance
		input_size, output_size = _prepare_data(provider)
		single_run.input_size = input_size
		single_run.output_size = output_size

		# Build datasets (train/val/test) and determine train window

		train_ds, val_ds, test_ds, train_start_date_id, train_end_date_id = _resolve_train_window_and_split(
			provider, max_training_instances, single_run
		)

		# ------------------------------------------------------------------
		# Build output directory layout (config + optional rep* subdirs)
		# ------------------------------------------------------------------
		# Safe tokens for filenames
		act_tok = str(single_run.activation).lower()
		lr_tok = f"{single_run.lr:.6g}".replace(".", "p").replace("-", "m")
		single_run.file_structure.suffix = f"prior{single_run.prior_days}_layers{single_run.mlp_layers}_lr{lr_tok}_act{act_tok}"
		# Configuration directory (shared by all repeats of this configuration)
		family_dir = family_run.file_structure.family_dir
		#wrong one family_dir = os.path.join(single_run.base_run_dir, config_suffix)
		print(f"TRAINING SET  DATES  {(train_start_date_id, train_end_date_id)}")

		# Write run parameters for traceability (per-config)
		sim_topks_resolved = family_run.simulate_top_k
		# Device selection and precision (only needed for train mode)
		device, eff_precision, amp_enabled, amp_dtype = setup_device_and_precision(family_run)
		print(
			f"Config: prior_days={single_run.prior_days} epochs={single_run.epochs} "
			f"batch_size={family_run.batch_of_instances_to_train_on_at_once} lr={single_run.lr} "
			f"optimizer={single_run.optimizer} "
			f"activation={single_run.activation} mlp_layers={single_run.mlp_layers} "
			f"split_mode={family_run.split_mode} seed={family_run.random_seed_splits} "
			f"dont_use_dataloader={family_run.dont_use_dataloader} "
			f"pin_memory={bool(getattr(family_run, 'pin_memory', False))} "
			f"shuffle_instances_in_training_set={bool(getattr(family_run, 'shuffle_instances_in_training_set', False))} "
			f"number_of_GPU_workers={getattr(family_run, 'number_of_GPU_workers', 0)} "
			f"pin_GPU_memory={bool(getattr(family_run, 'pin_GPU_memory', False))}"
		)
		print(
			f"Instances: total={max_training_instances} | splits -> train={len(train_ds)} "
			f"val={len(val_ds)} test={len(test_ds)}"
		)
		print(f"Tensor sizes: input_size={input_size} output_size={output_size}")

		training_bundle_list, rep_files_list, use_amp, amp_dtype, param_count = _build_training_bundles_and_loaders(
			family_run, single_run, input_size, output_size, train_ds, val_ds, test_ds, device)
		repeat_count_cfg = family_run.run_repeats
		start_epoch = 1
		cumulative_total_epochs_across_reps = single_run.epochs * repeat_count_cfg
		current_epochs_across_reps_container = [0]
		run_start_time = time.time()
		trading_interval = family_run.trading_interval
		ensemble_parts: List[nn.Module] = []

		# Initialize workers for parallel rep processing
		if rep_index is None:
			RepHandler.initialize_workers(device, input_size, output_size, save_interval, num_workers=4)

		for run_idx in range(repeat_count_cfg):
			if rep_index is not None and run_idx != rep_index:
				continue

			RepHandler.submit_rep_for_processing(
				run_idx=run_idx,
				training_bundle_list=training_bundle_list,
				rep_files_list=rep_files_list,
				family_run=family_run,
				single_run=single_run,
				start_epoch=start_epoch,
				run_start_time=run_start_time,
				cumulative_total_epochs_across_reps=cumulative_total_epochs_across_reps,
				current_epochs_across_reps_container=current_epochs_across_reps_container,
				ensemble_parts=ensemble_parts
			)

		if rep_index is None:
			# Wait for all reps to finish
			RepHandler.wait_for_all_reps()
			# Shutdown workers
			RepHandler.shutdown_workers()

			RepHandler.evaluate_all_reps(
				training_bundle_list=training_bundle_list,
				rep_files_list=rep_files_list,
				family_run=family_run,
				single_run=single_run,
				input_size=input_size,
				output_size=output_size,
				device=device
			)
		else:
			# If we are running a single rep in this process, we just run it synchronously
			# But submit_rep_for_processing puts it in a queue.
			# We need to process it here.
			task = _rep_queue.get()
			_rep_worker_logic(task, device, input_size, output_size, save_interval)
			_rep_queue.task_done()

			# Evaluate just this rep
			RepHandler.evaluate_single_repetition(
				run_idx=rep_index,
				training_bundle_list=training_bundle_list,
				rep_files_list=rep_files_list,
				family_run=family_run,
				single_run=single_run,
				input_size=input_size,
				output_size=output_size,
				device=device
			)

		handle_ensemble_sim = repeat_count_cfg > 1
		#print(f"Running periodic ensemble {handle_ensemble_sim} at epoch {epoch} (interval={trading_interval})...")
		# Cleanup GPU memory after each outer epoch
		for part in ensemble_parts:
			part.to("cpu")

		del ensemble_parts  # Clear this to avoid holding references twice

		gc.collect()
		if str(device).startswith("cuda"):
			torch.cuda.empty_cache()

		_log_gpu_usage(device, single_run.epochs, single_run)

	@staticmethod
	def load_from_file(path: str) -> SingleRun:
		"""Load a FamilyRun and SingleRun pair from a JSON file.

		The JSON structure is expected to be::

		    {"family_run": {...}, "single_run": {...}}
		"""

		return SingleRun.load(path)


def main(argv: Optional[List[str]] = None) -> int:
	"""CLI entry point for running a single saved configuration.

	The CLI accepts a single argument: ``--single-run-file`` pointing to a JSON
	file produced by saving a ``FamilyRun`` + ``SingleRun`` pair.
	"""

	parser = argparse.ArgumentParser(description="Run a single saved training configuration")
	parser.add_argument(
		"--single-run-file",
		required=True,
		type=str,
		help="Path to JSON file containing {single_run: ..., single_run: ...}",
	)
	parser.add_argument(
		"--rep-index",
		type=int,
		default=None,
		help="Index of the repetition to run (0-based). If provided, only this repetition will be run.",
	)
	args = parser.parse_args(argv)

	single_run = TrainSingleRun.load_from_file(args.single_run_file)
	print(f" train percent 2 {single_run.family_run.train_percent} val percent 4 {single_run.family_run.val_percent}")

	executor = TrainSingleRun(single_run)
	executor.run_one(rep_index=args.rep_index)
	return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
	raise SystemExit(main())

__all__ = ["TrainSingleRun", "main"]
