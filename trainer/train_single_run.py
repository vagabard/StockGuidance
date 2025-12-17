from __future__ import annotations

import argparse
import csv
import json
import os
import time
import traceback
from typing import Any, Dict, List, Optional

import line_profiler
import torch
from torch import nn
from torch.optim.lr_scheduler import ExponentialLR

from StockRecommenderFromModel import StockRecommenderFromModel
from trainer.ai_type import AIType
from trainer.dataloaders import build_dataloaders
from trainer.datasets import IndexDataset, RangeDataset
from trainer.factory import create_model_and_optimizer
from trainer.file_structure_single_run_handler import FileStructureSingleRunHandler
from trainer.io_run import save_model_and_manifest
from trainer.loss_functions.losses import ranking_surrogate_loss, mse_restricted
from trainer.metrics import eval_epoch
from trainer.simulation_dates import build_dates_to_handle_for_simulation
from trainer.splits import split_datasets
from trainer.state.family_run import FamilyRun
from trainer.state.single_run import SingleRun
from trainer.torch_interface.compat import TORCH_AVAILABLE
from trainer.torch_interface.device import setup_device_and_precision, enforce_cuda_or_fail
from trainer.training_bundle import TrainingBundle
from trainer.util.date_bounds import DateBoundResolver
from trainer.util.utils import count_elements
from training_instance_provider import TrainingInstanceProvider

torch.set_float32_matmul_precision("high")
#import torch_tensorrt
import apache_tvm
print(torch.compiler.list_backends())


def _update_per_epoch_scores(per_epoch_csv_path: str, epoch: int, top_k: List[int]) -> None:
	"""Append a summary row for ``epoch`` into ``per_epoch_scores.csv``.

	The summary is derived from the per-day stock simulation CSV that lives in
	``per_epoch_csv_path``.  For each K in ``top_k`` we compute the maximum
	portfolio value observed across all simulated days for that epoch, and we
	also compute the average of the ``top1_market_percentile`` column as a
	compact CDF-like score.
	"""
	try:
		if not os.path.exists(per_epoch_csv_path):
			return
		# Normalise and deduplicate K values so that column ordering is stable.
		unique_top_k = sorted({int(k) for k in top_k if int(k) > 0})
		if not unique_top_k:
			return
		max_by_k: Dict[int, float] = {k: float("-inf") for k in unique_top_k}
		cdf_sum = 0.0
		cdf_count = 0
		with open(per_epoch_csv_path, "r", newline="", encoding="utf-8") as f_in:
			reader = csv.reader(f_in)
			header = next(reader, None)
			if not header:
				return
			# Column indices
			try:
				cdf_idx = header.index("top1_market_percentile")
			except ValueError:
				cdf_idx = -1
			# Portfolio columns
			if len(unique_top_k) == 1:
				# Single-K simulations use a column named ``portfolio_value``.
				try:
					col_idx = header.index("portfolio_value")
				except ValueError:
					col_idx = -1
				col_idx_by_k: Dict[int, int] = {unique_top_k[0]: col_idx}
			else:
				col_idx_by_k = {}
				for k in unique_top_k:
					col_name = f"portfolio_top{k}"
					try:
						col_idx_by_k[k] = header.index(col_name)
					except ValueError:
						col_idx_by_k[k] = -1

			for row in reader:
				if not row:
					continue
				# CDF / percentile average
				if 0 <= cdf_idx < len(row):
					try:
						v = float(row[cdf_idx])
					except ValueError:
						v = None
					if v is not None:
						cdf_sum += v
						cdf_count += 1
				# Max portfolio per K
				for k, idx in col_idx_by_k.items():
					if idx < 0 or idx >= len(row):
						continue
					try:
						pv = float(row[idx])
					except ValueError:
						continue
					if pv > max_by_k[k]:
						max_by_k[k] = pv

		# Guard against empty/invalid data
		if cdf_count == 0:
			return
		avg_cdf = cdf_sum / float(cdf_count)
		# Replace -inf with 0.0 for Ks that had no valid data.
		for k, v in list(max_by_k.items()):
			if v == float("-inf"):
				max_by_k[k] = 0.0

		out_dir = os.path.dirname(per_epoch_csv_path)
		out_path = os.path.join(out_dir, "per_epoch_scores.csv")
		file_exists = os.path.exists(out_path)
		with open(out_path, "a", newline="", encoding="utf-8") as f_out:
			writer = csv.writer(f_out)
			if not file_exists:
				# ``avg_top1_market_percentile_over_days`` makes it explicit that this
				# value is the average top-1 market percentile across all simulated
				# days for the given epoch, rather than a single-day percentile.
				header_cols = ["epoch", "average_over_time_of_cdf_top_stock"]
				for k in unique_top_k:
					header_cols.append(f"max_portfolio_top{k}")
				writer.writerow(header_cols)
			row_vals = [epoch, avg_cdf]
			for k in unique_top_k:
				row_vals.append(max_by_k.get(k, 0.0))
			writer.writerow(row_vals)
	except Exception as e:
		print(f"Warning: failed to update per_epoch_scores for {per_epoch_csv_path}: {e}")
		traceback.print_exception(e)

@line_profiler.profile
def simulate_trading_with_trained_model(model_type_to_use: AIType, dbinfo_file: str, epoch: int, family_run: FamilyRun,
										recommenders: list[StockRecommenderFromModel],
										rep_index: int, sim_topks_resolved: list[int] | None,
										single_run: SingleRun) -> None:
	"""Run a trading simulation for the given epoch and update per-epoch scores.

	This function wraps :func:`simulate_trading.simulate` to:

	* decide whether we are simulating an individual repeat or an ensemble,
	* normalise database connection parameters, and
	* after the per-day CSV is written and renamed to include the epoch
	  suffix, append a summary row into ``per_epoch_scores.csv`` within the
	  same directory.
	"""
	try:
		import simulate_trading as _sim
		if model_type_to_use is AIType.ENSEMBLE:
			print(f"Simulating with ensemble model at epoch {epoch}")
		elif model_type_to_use is AIType.SINGLE:
			print(f"Simulating with single model at epoch {epoch}")
		else:
			raise ValueError(f"Unknown model_type_to_use: {model_type_to_use}")

		first_output = _sim.simulate(
			single_run,
			recommenders,
			top_k=sim_topks_resolved,
			dbinfo_file=dbinfo_file,
			limit=None,
			dates_to_handle=build_dates_to_handle_for_simulation(family_run),
			prefer_prior_only=bool(family_run.simulate_prior_only),
			out_filename_suffix=f"_epoch_{epoch}",
			#ensemble_logic=(model_type_to_use == AIType.ENSEMBLE),
		)
		# out_path = os.path.join(recommenders[0].output_directory)
		# print(f"Periodic simulation complete. Raw results written to: {out_path}")
		# final_path = out_path
		# try:
		# 	orig_name = os.path.basename(out_path)
		# 	m = re.match(r"stock_simulation_(\d+)(days|percent)?\.csv$", orig_name)
		# 	if m:
		# 		sim_set_size = int(m.group(1))
		# 		variant_suffix = m.group(2) or ""
		# 	else:
		# 		# Fallback when filename does not match the standard pattern
		# 		sim_set_size = int(float(family_run.simulate_percent))
		# 		variant_suffix = ""
		# 	new_name = f"stock_simulation_{sim_set_size}{variant_suffix}_epoch_{epoch}.csv"
		# 	new_path = os.path.join(os.path.dirname(out_path), new_name)
		# 	os.replace(out_path, new_path)
		# 	final_path = new_path
		# 	print(f"Renamed {out_path} periodic simulation output to: {new_path}")
		# except Exception as rename_e:
		# 	print(f"Warning: failed to rename periodic simulation output '{out_path}': {rename_e}")
		# Update per-epoch summary scores for this simulation.
		if sim_topks_resolved:
			_update_per_epoch_scores(first_output, epoch, sim_topks_resolved)
	except Exception as sim_e:
		traceback.print_exception(sim_e)


def backprop_loss_non_amp(xb=None, yb=None, model : nn.Module=None, family_run:FamilyRun=None, optimizer:nn.Module=None,
                          batch_idx=None):
	xb = torch.nan_to_num(xb, nan=0.0, posinf=0.0, neginf=0.0)
	yb = torch.nan_to_num(yb, nan=0.0, posinf=0.0, neginf=0.0)
	pred = model(xb)
	pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
	if bool(family_run.use_ranking_error_loss):
		loss = ranking_surrogate_loss(pred, yb.to(pred.dtype), temperature=float(family_run.rank_temperature))
	else:
		loss = torch.mean((pred - yb.to(pred.dtype)) ** 2)
	if not torch.isfinite(loss):
		optimizer.zero_grad(set_to_none=True)
		batch_idx += 1
		return pred,loss,batch_idx
	loss.backward()
	optimizer.step()
	return pred,loss,None


def _maybe_save_checkpoint(
	*,
	single_run: SingleRun,
	one_file_structure: FileStructureSingleRunHandler,
	rep_index: int,
	model: nn.Module,
	optimizer,
	input_size: int,
	output_size: int,
	epoch: int,
	family_run: FamilyRun,
	save_interval: int,
) -> None:
	if (save_interval > 0 and (epoch % save_interval == 0)) or (epoch == family_run.total_epochs_to_train):
		try:
			checkpoint_model, checkpoint_optimizer = one_file_structure.get_checkpoint_files_for_rep(rep_index)
			save_model_and_manifest(
				single_run,
				model,
				optimizer,
				one_file_structure.run_group_dir,
				one_file_structure.suffix,
				"",
				input_size,
				output_size,
				model_state_path=checkpoint_model,
				optimizer_state_path=checkpoint_optimizer,
			)
			try:
				single_run_path = os.path.join(
					single_run.file_structure.get_directory_for_rep(rep_index),
					f"single_run_epoch.json",
				)
				single_run.save(single_run_path)
			except Exception as save_single_e:
				print(f"Warning: failed to save SingleRun JSON at epoch {epoch}: {save_single_e}")
		except Exception as ckpt_e:
			print(f"Warning: failed to write training_state or checkpoint in {one_file_structure.run_group_dir}: {ckpt_e}")
			traceback.print_exception(ckpt_e)


def _maybe_simulate_trading_for_rep(
	*,
	family_run: FamilyRun,
	single_run: SingleRun,
	rep_index: int,
	model: nn.Module,
	one_file_structure: FileStructureSingleRunHandler,
	epoch: int,
	sim_topks_resolved: Optional[List[int]],
) -> None:
	trading_interval = family_run.trading_interval
	handle_trading_sim = trading_interval > 0 and epoch % trading_interval == 0
	if handle_trading_sim:
		stock_recommender = StockRecommenderFromModel([model], single_run.file_structure.get_directory_for_rep(rep_index))
		print(
			f"Running periodic simulate_trading at epoch {epoch} "
			f"(interval={trading_interval}) on run_dir={one_file_structure.get_directory_for_rep(rep_index)}..."
		)
		simulate_trading_with_trained_model(
			AIType.SINGLE,
			family_run.dbinfo_file,
			epoch,
			family_run,
			[stock_recommender],
			rep_index,
			sim_topks_resolved,
			single_run,
		)


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
			self._providers[day_centered] = TrainingInstanceProvider(
				use_torch=TORCH_AVAILABLE,
				limit=family_run.limit,
				current_day_change=family_run.current_day_change,
				volume_ratio=family_run.volume_ratio,
				vwap_ratio=family_run.vwap_ratio,
				tranaction_ratio=family_run.transaction_ratio,
				day_change_centered=day_centered,
			)
		return self._providers[day_centered]

	# ----------------------------
	# Small helper extractions
	# ----------------------------
	def _prepare_data(self, provider: TrainingInstanceProvider) -> tuple[int, int]:
		"""Return (input_size, output_size) derived from the first instance."""
		x0, y0 = provider.get_feature_and_label_pair(1)
		input_size = count_elements(x0)
		output_size = count_elements(y0)
		return input_size, output_size

	def _resolve_train_window_and_split(
		self,
		provider: TrainingInstanceProvider,
		max_training_instances: int,
		family_run: FamilyRun,
	) -> tuple[RangeDataset | IndexDataset, RangeDataset | IndexDataset, RangeDataset | IndexDataset, int, int]:
		"""Resolve train window (start/end date_id) and split datasets.

		Returns (train_ds, val_ds, test_ds, train_start_date_id, train_end_date_id).
		"""
		print("train start date ", family_run.train_start_date_id)
		raw_train_start = family_run.train_start_date_id
		raw_train_end = family_run.train_end_date_id
		try:
			all_dates = [int(d) for d in getattr(provider, "_dates")]
			prior = int(getattr(provider, "_num_prior"))
		except Exception:
			raise RuntimeError(
				"Provider does not expose _dates/_num_prior required for deriving "
				"train_start_date_id/train_end_date_id from available instances"
			)
		# Instances 1..max_training_instances correspond to dates at indices prior..prior + max_training_instances - 1.
		if prior < 0 or prior + max_training_instances - 1 >= len(all_dates):
			raise RuntimeError(
				"Inconsistent provider _dates/_num_prior for deriving train date_id range"
			)
		usable_dates = all_dates[prior : prior + max_training_instances]
		if not usable_dates:
			raise RuntimeError("No usable dates available for training window computation")
		resolver = DateBoundResolver(usable_dates)
		# If random_training_duration is provided, pick a random window so that
		# end_index - start_index == duration (in terms of usable_dates indices).
		train_start_date_id: Optional[int] = None
		train_end_date_id: Optional[int] = None
		rnd_duration = getattr(family_run, "random_training_duration", None)
		if rnd_duration is not None:
			try:
				dur = int(rnd_duration)
			except Exception:
				dur = None
			if dur is not None and dur >= 0:
				# Compute max allowed start index based on duration
				length = len(usable_dates)
				max_start_index = length - (dur + 1)
				if max_start_index < 0:
					raise ValueError(
						f"--random-training-duration={dur} is too large for available dates ({length}); "
						"requires at least duration+1 dates."
					)
				# Use provided seed for determinism across runs, if given
				import random as _random
				seed_val = getattr(family_run, "random_seed_splits", None)
				_rng = _random.Random(int(seed_val)) if seed_val is not None else _random
				start_idx = _rng.randint(0, max_start_index)
				end_idx = start_idx + dur
				train_start_date_id = int(usable_dates[start_idx])
				train_end_date_id = int(usable_dates[end_idx])
				print(
					f"Random training window selected: start_index={start_idx} end_index={end_idx} "
					f"start_date_id={train_start_date_id} end_date_id={train_end_date_id} duration={dur}"
				)
		if train_start_date_id is None:
			train_start_date_id = resolver.resolve_bound(raw_train_start, "start")
		if train_end_date_id is None:
			train_end_date_id = resolver.resolve_bound(raw_train_end, "end")

		train_ds, val_ds, test_ds = split_datasets(
			provider,
			family_run,
			max_training_instances,
			train_validation_and_test_set_start_date_id=int(train_start_date_id),
			train_validation_and_test_set_end_date_id=int(train_end_date_id),
			train_percent=family_run.train_percent,
			validation_percent=family_run.val_percent,
		)
		print(
			f"Datasets: train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}  "
			f"train start date_id={train_start_date_id} train end date_id={train_end_date_id}"
		)
		return train_ds, val_ds, test_ds, int(train_start_date_id), int(train_end_date_id)

	def _build_training_bundles_and_loaders(
		self,
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
		training_bundle_list: List[TrainingBundle] = []
		param_count = 0
		use_amp = False
		amp_dtype = None
		repeat_count_cfg = family_run.run_repeats
		for _ in range(repeat_count_cfg):
			m, opt, sc, inner_use_amp, inner_amp_dtype = create_model_and_optimizer(
				family_run, input_size, output_size, single_run.mlp_layers, single_run.activation, device, single_run.lr
			)
			use_amp = inner_use_amp
			amp_dtype = inner_amp_dtype
			one_training_bundle = TrainingBundle(model=m, optimizer=opt, scaler=sc, use_amp=inner_use_amp, amp_dtype=inner_amp_dtype)
			training_bundle_list.append(one_training_bundle)
			param_count = sum(p.numel() for p in m.parameters())
			# Build batch providers (DataLoader or simple generator).
			train_loader_temp, val_loader_temp, test_loader_temp = build_dataloaders(
				family_run, train_ds, val_ds, test_ds
			)
			one_training_bundle.training_instance_provider = train_loader_temp
			one_training_bundle.validation_instance_provider = val_loader_temp
			one_training_bundle.testing_instance_provider = test_loader_temp
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

	def _maybe_resume_checkpoints(
		self,
		training_bundle_list: List[TrainingBundle],
		rep_files_list: List[FileStructureSingleRunHandler],
		family_run: FamilyRun,
		device,
		family_dir: str,
	) -> int:
		"""If checkpoints exist, resume and return starting epoch."""
		start_epoch = 1
		resume_from_current_epoch = True
		for run_idx in range(family_run.run_repeats):
			rep_index = run_idx + 1
			fs = rep_files_list[run_idx]
			ckpt_model, ckpt_optim = fs.get_checkpoint_files_for_rep(rep_index)
			resume_from_current_epoch = resume_from_current_epoch and (
				os.path.exists(fs.parent_file_structure.family_dir)
				and os.path.exists(ckpt_model)
				and os.path.exists(ckpt_optim)
			)
			try:
				with open(fs.parent_file_structure.family_state_path, "r", encoding="utf-8") as sf:
					state = json.load(sf)
				last_completed = int(state.get("last_completed_epoch", 0))
				if 1 <= last_completed < family_run.total_epochs_to_train:
					if (run_idx == 0) and resume_from_current_epoch:
						print(f"Resuming training from epoch {last_completed + 1} (previously completed {last_completed}) in {family_dir}")
					start_epoch = last_completed + 1

					training_bundle_list[run_idx].model.load_state_dict(torch.load(ckpt_model, map_location=device))
					training_bundle_list[run_idx].optimizer.load_state_dict(torch.load(ckpt_optim, map_location=device))
			except Exception as resume_e:
				print(f"Warning: failed to load training_state or checkpoint for resume in {fs.run_group_dir}: {resume_e}")
				start_epoch = 1
		return start_epoch

	def _log_gpu_usage(self, device, epoch: int, single_run: SingleRun) -> None:
		try:
			if torch.cuda.is_available():
				free_mem, total_mem = torch.cuda.mem_get_info(device)
				used_mem = total_mem - free_mem

				run_dir_log = getattr(single_run.file_structure, "run_directory", None)
				if not run_dir_log:
					run_dir_log = single_run.file_structure.run_group_dir

				if run_dir_log:
					log_path = os.path.join(run_dir_log, "gpu_usage.csv")
					file_exists = os.path.exists(log_path)
					with open(log_path, "a", newline="") as f:
						writer = csv.writer(f)
						if not file_exists:
							writer.writerow(["timestamp", "epoch", "used_bytes", "free_bytes", "total_bytes"])
						writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), epoch, used_mem, free_mem, total_mem])
		except Exception as log_e:
			print(f"Warning: GPU logging failed: {log_e}")

	def _train_one_epoch_for_rep(
		self,
		*,
		model: nn.Module,
		optimizer,
		scaler,
		use_amp: bool,
		amp_dtype,
		train_loader,
		val_loader,
		device,
		family_run: FamilyRun,
		epoch: int,
	) -> tuple[float, float, float, float, torch.Tensor, torch.Tensor]:
		"""Run training for one epoch for a single repetition and return metrics."""
		model.train()
		total = 0
		train_loss_sum = 0.0
		train_top10_sum = 0.0
		batch_idx = 0
		train_iter = train_loader
		total_batches = len(train_loader) if hasattr(train_loader, "__len__") else 0
		for xb, yb in train_iter:
			xb = xb.to(device)
			yb = yb.to(device)
			optimizer.zero_grad(set_to_none=True)
			if use_amp:
				pred,loss,batch_index_update = self.backprop_loss_for_amp(xb=xb, yb=yb, model=model, family_run=family_run, optimizer=optimizer, batch_idx=batch_idx, amp_dtype=amp_dtype, scaler=scaler)
				if batch_index_update is not None:
					batch_idx = batch_index_update
					continue
			else:
				pred,loss,batch_index_update = backprop_loss_non_amp(xb=xb, yb=yb, model=model, family_run=family_run, optimizer=optimizer, batch_idx=batch_idx)
				if batch_index_update is not None:
					batch_idx = batch_index_update
					continue
			bs = xb.size(0)
			total += bs
			train_loss_sum += loss.item() * bs
			train_top10_sum += mse_restricted(pred.detach(), yb.to(pred.dtype), mode="top", k=10).item() * bs

			batch_idx += 1
			if family_run.log_interval and family_run.log_interval > 0 and (epoch % family_run.log_interval == 0 or epoch == family_run.total_epochs_to_train):
				avg_loss_so_far = train_loss_sum / max(1, total)
				print(f"Epoch {epoch}/{family_run.total_epochs_to_train} | batch {batch_idx}/{total_batches} | curr_loss={loss.item():.6f} | avg_loss={avg_loss_so_far:.6f}")

		train_mse = train_loss_sum / max(1, total)
		train_top10 = train_top10_sum / max(1, total)

		val_iter = val_loader
		val_mse, val_top10, _val_rde_unused = eval_epoch(
			model,
			val_iter,
			device,
			amp_enabled=use_amp,
			amp_dtype=amp_dtype,
			rank_lower=family_run.rank_error_lower_bound,
			rank_upper=family_run.rank_error_upper_bound,
			compute_rank_err=bool(family_run.enable_rank_error_loss_in_training),
			compute_rank_disp=False,
		)
		return train_mse, val_mse, train_top10, val_top10, pred, yb


	def backprop_loss_for_amp(self, xb=None, yb=None, model : nn.Module=None, family_run:FamilyRun=None, optimizer:nn.Module=None,
	                          batch_idx=None, amp_dtype=None, scaler=None):
		with torch.amp.autocast("cuda", dtype=amp_dtype):  # type: ignore[attr-defined]
			xb = torch.nan_to_num(xb, nan=0.0, posinf=0.0, neginf=0.0)
			yb = torch.nan_to_num(yb, nan=0.0, posinf=0.0, neginf=0.0)
			pred = model(xb)
			pred = torch.nan_to_num(pred, nan=0.0, posinf=0.0, neginf=0.0)
			if bool(family_run.use_ranking_error_loss):
				loss = ranking_surrogate_loss(pred, yb.to(pred.dtype), temperature=float(family_run.rank_temperature))
			else:
				loss = torch.mean((pred - yb.to(pred.dtype)) ** 2)
		if not torch.isfinite(loss):
			optimizer.zero_grad(set_to_none=True)
			batch_idx += 1
			return pred,loss,batch_idx
		elif amp_dtype == torch.float16:
			scaler.scale(loss).backward()
			scaler.step(optimizer)
			scaler.update()
		else:
			loss.backward()
			optimizer.step()
		return pred,loss,None

	@line_profiler.profile
	def run_one(self, save_interval: int = 100) -> None:
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
			"activation ",
			single_run.activation,
			"final_activation ",
			single_run.final_activation,
		)
		provider = self._get_provider(single_run.day_change_centered)
		print(f"Provider: {provider}")
		# Set the sliding window size for this configuration
		provider.set_how_many_days_in_past_to_use(int(single_run.prior_days))

		max_training_instances = provider.get_maximum_number_of_test_train_instances()
		if max_training_instances <= 1:
			raise RuntimeError("Not enough instances to train. Consider reducing --prior-days or loading more data.")

		# Determine input/output sizes from the first instance
		input_size, output_size = self._prepare_data(provider)

		# Build datasets (train/val/test) and determine train window
		train_ds, val_ds, test_ds, train_start_date_id, train_end_date_id = self._resolve_train_window_and_split(
			provider, max_training_instances, family_run
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
		#wrong one family_dir = os.path.join(family_run.base_run_dir, config_suffix)

		# Write run parameters for traceability (per-config)
		sim_topks_resolved = family_run.simulate_top_k
		# Device selection and precision (only needed for train mode)
		device, eff_precision, amp_enabled, amp_dtype = setup_device_and_precision(family_run)
		print(
			f"Config: prior_days={single_run.prior_days} epochs={family_run.total_epochs_to_train} "
			f"batch_size={family_run.batch_of_instances_to_train_on_at_once} lr={single_run.lr} "
			f"activation={single_run.activation} mlp_layers={single_run.mlp_layers} "
			f"split_mode={family_run.split_mode} seed={family_run.random_seed_splits} "
			f"dont_use_dataloader={family_run.dont_use_dataloader} "
			f"pin_memory={bool(getattr(family_run,'pin_memory', False))} "
			f"shuffle_instances_in_training_set={bool(getattr(family_run,'shuffle_instances_in_training_set', False))} "
			f"number_of_GPU_workers={getattr(family_run,'number_of_GPU_workers', 0)} "
			f"pin_GPU_memory={bool(getattr(family_run,'pin_GPU_memory', False))}"
		)
		print(
			f"Instances: total={max_training_instances} | splits -> train={len(train_ds)} "
			f"val={len(val_ds)} test={len(test_ds)}"
		)
		print(f"Tensor sizes: input_size={input_size} output_size={output_size}")

		training_bundle_list: List[TrainingBundle] = []
		# Enforce GPU usage: throw an error if not using CUDA (train mode only)
		enforce_cuda_or_fail(device)
		# Model, loss, optimizer (train mode only).
		repeat_count_cfg = family_run.run_repeats
		use_amp = False
		amp_dtype = None
		param_count = 0
		for rep_idx in range(repeat_count_cfg):
			m, opt, sc, inner_use_amp, inner_amp_dtype = create_model_and_optimizer(
				family_run, input_size, output_size, single_run.mlp_layers, single_run.activation, device, single_run.lr
			)
			m.compile(backend="torch_tensorrt")
			# a hack because they should all be the same within the reps.
			use_amp = inner_use_amp
			amp_dtype = inner_amp_dtype
			one_training_bundle = TrainingBundle(model=m, optimizer=opt, scaler=sc, use_amp=inner_use_amp, amp_dtype=inner_amp_dtype)
			training_bundle_list.append(one_training_bundle)
			param_count = sum(p.numel() for p in m.parameters())
			# Build batch providers (DataLoader or simple generator).
			train_loader_temp, val_loader_temp, test_loader_temp = build_dataloaders(
				family_run, train_ds, val_ds, test_ds
			)
			one_training_bundle.training_instance_provider = train_loader_temp
			one_training_bundle.validation_instance_provider = val_loader_temp
			one_training_bundle.testing_instance_provider = test_loader_temp
		if use_amp and amp_dtype == torch.float16:
			print("Precision runtime: autocast=float16 with GradScaler")
		elif use_amp and amp_dtype == torch.bfloat16:
			print("Precision runtime: autocast=bfloat16 (no GradScaler)")
		else:
			print("Precision runtime: float32 (no autocast)")
		print(f"Model parameters: {param_count:,}")

		rep_files_list: List[FileStructureSingleRunHandler] = []

		start_epoch = 1
		resume_from_current_epoch = True
		for run_idx in range(repeat_count_cfg):
			rep_index = run_idx + 1
			fs = single_run.file_structure

			# Use per-run learning rate adjustment parameter (scheduler gamma), defaulting to 0.998
			gamma: float = getattr(single_run, "learning_rate_adjustment_parameter", 0.998)
			training_bundle_list[run_idx].scheduler = ExponentialLR(training_bundle_list[run_idx].optimizer, gamma=float(gamma))
			training_bundle_list[run_idx].suffix = fs.suffix
			rep_files_list.append(fs)
			ckpt_model, ckpt_optim = fs.get_checkpoint_files_for_rep(rep_index)
			resume_from_current_epoch = resume_from_current_epoch and (
				os.path.exists(fs.parent_file_structure.family_dir)
				and os.path.exists(ckpt_model)
				and os.path.exists(ckpt_optim)
			)
			try:
				with open(fs.parent_file_structure.family_state_path, "r", encoding="utf-8") as sf:
					state = json.load(sf)
				last_completed = int(state.get("last_completed_epoch", 0))
				if 1 <= last_completed < family_run.total_epochs_to_train:
					if (run_idx == 0) and resume_from_current_epoch:
						print(f"Resuming training from epoch {last_completed + 1} (previously completed {last_completed}) in {family_dir}")
					start_epoch = last_completed + 1

					training_bundle_list[run_idx].model.load_state_dict(torch.load(ckpt_model, map_location=device))
					training_bundle_list[run_idx].optimizer.load_state_dict(torch.load(ckpt_optim, map_location=device))
			except Exception as resume_e:
				print(f"Warning: failed to load training_state or checkpoint for resume in {fs.run_group_dir}: {resume_e}")
				start_epoch = 1
		train_loss_sum = 0.0
		train_top10_sum = 0.0
		total = 0
		for epoch in range(start_epoch, family_run.total_epochs_to_train + 1):
			# GPU Memory Logging
			self._log_gpu_usage(device, epoch, single_run)
			should_print_message = False
			if family_run.log_interval and family_run.log_interval > 0 and (epoch % family_run.log_interval == 0 or epoch == family_run.total_epochs_to_train):
				should_print_message = epoch % family_run.log_interval == 0 and epoch != 0
			if should_print_message:
				print(f"\n=== Starting epoch {epoch}/{family_run.total_epochs_to_train} ===")
			ensemble_parts: List[nn.Module] = []
			for run_idx in range(repeat_count_cfg):
				rep_index = run_idx + 1

				one_file_structure = rep_files_list[run_idx]
				model = training_bundle_list[run_idx].model
				optimizer = training_bundle_list[run_idx].optimizer
				scaler = training_bundle_list[run_idx].scaler
				use_amp = training_bundle_list[run_idx].use_amp
				amp_dtype = training_bundle_list[run_idx].amp_dtype
				train_loader = training_bundle_list[run_idx].training_instance_provider
				val_loader = training_bundle_list[run_idx].validation_instance_provider
				if use_amp and amp_dtype == torch.float16:
					print("Precision runtime: autocast=float16 with GradScaler")
				elif use_amp and amp_dtype == torch.bfloat16:
					print("Precision runtime: autocast=bfloat16 (no GradScaler)")
				epoch_start = time.time()

				train_mse, val_mse, train_top10, val_top10, pred, yb = self._train_one_epoch_for_rep(
					model=model,
					optimizer=optimizer,
					scaler=scaler,
					use_amp=use_amp,
					amp_dtype=amp_dtype,
					train_loader=train_loader,
					val_loader=val_loader,
					device=device,
					family_run=family_run,
					epoch=epoch,
				)

				epoch_dur = time.time() - epoch_start
				if should_print_message:
					msg = (
						f"Epoch {epoch:03d} | train_mse={train_mse:.6f} val_mse={val_mse:.6f} "
						f"train_top10={train_top10:.6f} val_top10={val_top10:.6f} "
						+ (" [rank_err:on]" if family_run.enable_rank_error_loss_in_training else "")
						+ f" | time={epoch_dur:.2f}s"
					)
					print(msg)

				row = [epoch, train_mse, val_mse, train_top10, val_top10]
				one_file_structure.get_metrics_file_handler_for_rep(rep_index).write_row(row)
				#create a stock recommender

				# Optionally throttle how often we persist heavy artifacts to speed up training.
				_maybe_save_checkpoint(
					single_run=single_run,
					one_file_structure=one_file_structure,
					rep_index=rep_index,
					model=model,
					optimizer=optimizer,
					input_size=input_size,
					output_size=output_size,
					epoch=epoch,
					family_run=family_run,
					save_interval=save_interval,
				)

				_maybe_simulate_trading_for_rep(
					family_run=family_run,
					single_run=single_run,
					rep_index=rep_index,
					model=model,
					one_file_structure=one_file_structure,
					epoch=epoch,
					sim_topks_resolved=sim_topks_resolved,
				)
				ensemble_parts.append(model)
				scheduler = training_bundle_list[run_idx].scheduler
				scheduler.step()

			ensemble_interval = family_run.ensemble_interval

			handle_ensemble_sim = ensemble_interval > 0 and repeat_count_cfg > 1 and epoch % ensemble_interval == 0
			#print(f"Running periodic ensemble {handle_ensemble_sim} at epoch {epoch} (interval={ensemble_interval})...")
			if handle_ensemble_sim:
				ensemble_recommender = StockRecommenderFromModel(ensemble_parts, single_run.file_structure.run_group_dir)
				#make sure we have saved the existing models
				simulate_trading_with_trained_model(AIType.ENSEMBLE, family_run.dbinfo_file , epoch, family_run,[ensemble_recommender], -1,sim_topks_resolved, single_run)

		for run_idx in range(repeat_count_cfg):
			rep_index = run_idx + 1
			fs = rep_files_list[run_idx]
			metrics_file = fs.get_metrics_file_for_rep( rep_index)
			#suffix = fs.suffix
			model = training_bundle_list[run_idx].model
			#optimizer = training_bundle_list[run_idx].optimizer
			use_amp = training_bundle_list[run_idx].use_amp
			amp_dtype = training_bundle_list[run_idx].amp_dtype
			test_iter = training_bundle_list[run_idx].testing_instance_provider
			# Build a fresh iterator for test as well when not using DataLoader
			test_mse, test_top10, test_rde = eval_epoch(
				model,
				test_iter,
				device,
				amp_enabled=use_amp,
				amp_dtype=amp_dtype,
				rank_lower=int(family_run.rank_error_lower_bound),
				rank_upper=int(family_run.rank_error_upper_bound),
				compute_rank_err=bool(family_run.enable_rank_error_loss_in_training),
			)
			print("Final Test Metrics:")
			print(f"test_mse={test_mse:.6f} test_top10={test_top10:.6f} test_rank_disp={test_rde:.3f}")

			# Append final test metrics to a sidecar file next to metrics CSV (include suffix)
			test_metrics_path = os.path.splitext(metrics_file)[0] + "_test.csv"
			with open(test_metrics_path, mode="w", newline="", encoding="utf-8") as f_test:
				writer_test = csv.writer(f_test)
				writer_test.writerow(["metric", "value"])
				writer_test.writerow(["test_mse", test_mse])
				writer_test.writerow(["test_top10_mse", test_top10])
				writer_test.writerow(["test_rank_disp", test_rde])

			print(f"Wrote per-epoch metrics to: {metrics_file}")
			print(f"Wrote final test metrics to: {test_metrics_path}")

			try:
				# manifest_path = save_model_and_manifest(
				# 	single_run,
				# 	model,
				# 	optimizer,
				# 	rep_dir,
				# 	suffix,
				# 	metrics_file,
				# 	test_metrics_path,
				# 	input_size,
				# 	output_size,
				# 	single_run.mlp_layers,
				# 	single_run.activation,
				# 	single_run.final_activation,
				# 	single_run.prior_days,
				# )
				#
				if bool(family_run.simulate_after_training):
					after_recommender = StockRecommenderFromModel([model],single_run.file_structure.run_group_dir)
					simulate_trading_with_trained_model( AIType.SINGLE,family_run.dbinfo_file,  family_run.total_epochs_to_train, family_run, [after_recommender], rep_index, sim_topks_resolved, single_run)
			except Exception as save_e:
				traceback.print_exception(save_e)

	@staticmethod
	def load_from_file(path: str) -> SingleRun:
		"""Load a FamilyRun and SingleRun pair from a JSON file.

		The JSON structure is expected to be::

		    {"family_run": {...}, "single_run": {...}}
		"""

		with open(path, "r", encoding="utf-8") as f:
			data: Dict[str, Any] = json.load(f)

		fr_dict = data.get("family_run") or {}
		sr_dict = data.get("single_run") or {}

		# Prefer the structured helper that knows how to rebuild
		# ``FileStructureFamilyRunHandler`` and normalise set/list fields.
		if hasattr(FamilyRun, "from_dict"):
			family_run = FamilyRun.from_dict(fr_dict)  # type: ignore[assignment]
		else:  # pragma: no cover - fallback for older code
			family_run = FamilyRun(**fr_dict)  # type: ignore[call-arg]

		single_run = SingleRun(
			timestamp_id=str(sr_dict["timestamp_id"]),
			family_run=family_run,
			day_change_centered=str(sr_dict["day_change_centered"]),
			prior_days=int(sr_dict["prior_days"]),
			mlp_layers=int(sr_dict["mlp_layers"]),
			lr=float(sr_dict["lr"]),
			activation=str(sr_dict["activation"]),
			final_activation=str(sr_dict["final_activation"]),
			include_features=tuple(sr_dict.get("include_features") or []),
			file_structure=FileStructureSingleRunHandler.from_dict(sr_dict["file_structure"]),
		)
		return single_run


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
		help="Path to JSON file containing {family_run: ..., single_run: ...}",
	)
	args = parser.parse_args(argv)

	single_run = TrainSingleRun.load_from_file(args.single_run_file)
	executor = TrainSingleRun(single_run)
	executor.run_one()
	return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
	raise SystemExit(main())


__all__ = ["TrainSingleRun", "main"]
