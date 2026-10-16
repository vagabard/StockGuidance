"""
Legacy open-to-close training, isolated until Stages 7-9 migrate its callers.

New application callers use stockguidance.training.service.train_model and
receive an explicit inference artifact reference. This controller cannot
produce artifacts for the new close-to-close workflows.

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

import os
import subprocess
import traceback
from typing import List, Optional
import time

from trainer.legacy_arguments import parse_args
from trainer import train_single_run
# Trainer package imports (split from monolithic script)
from trainer.state.family_run import FamilyRun, VALID_ACTIVATIONS, VALID_DAY_CENTERS
from trainer.train_single_run import TrainSingleRun
from training_instance_provider import TrainingInstanceProvider

# Dataset classes, batching, metrics, and other helpers are provided by trainer.* modules


RUN_FAMILY_PREFIX = "run_family_"


def _main_impl(family_run: FamilyRun) -> int:
	"""Implementation of the original ``main`` logic.

	This function preserves the existing CLI behaviour. It is wrapped by the
	:class:`TrainPytorchNetwork` class and by :func:`main` which simply
	delegates to the class so that other modules can programmatically invoke
	training and discover the created run directory.
	"""

	_providers: dict[str, TrainingInstanceProvider] = {}

	# Execute runs for all combinations


	# All runs (single or grid) are now routed through the run-family
	# controller below so that every invocation is restartable via
	# --resume-directory. The previous in-process-only fast path for the
	# single, no-range case has been removed.

	# Otherwise, spawn a fresh Python process per combination via a simple
	# run-family controller. This avoids in-process memory accumulation and
	# provides a natural place to persist controller state for resuming.

	print("creating sub run options")
	if family_run.current_index == 0:
		print("creating new family run")
		#then this is a new run_family_* directory, so create it
		family_run.create_options_for_individual_runs()
		family_run.save()
	# Controller loop: iterate over combinations, updating current_index before
	# launching each subprocess so that a crash can be resumed later.
	print("starting controller loop")
	print(f"initial conditions are {family_run.current_index}  and {len(family_run.single_runs_for_all_combinations)}")
	while family_run.current_index < len(family_run.single_runs_for_all_combinations):
		family_run.completed = False
		_, save_path_of_family_run  = family_run.save()
		single_run = family_run.single_runs_for_all_combinations[family_run.current_index]
		# Persist the current SingleRun (and embedded FamilyRun) to a JSON file
		# so that child processes or external tools can execute the exact
		# configuration directly via ``trainer.train_single_run``.
		try:
			single_run_json = os.path.join(
				single_run.file_structure.run_group_dir,
				f"single_run_{family_run.current_index}.json",
			)
			single_run.save(single_run_json)
			single_run.last_save_location = single_run_json
			print(f"Saved SingleRun description to {single_run_json}   in {single_run.last_save_location}")
		except Exception as save_single_e:
			print(f"Warning: failed to save SingleRun description before subprocess: {save_single_e}")

		cmd = single_run.to_commandline()
		print(f"Spawning isolated run-family member {family_run.current_index+1}/{len(family_run.single_runs_for_all_combinations)}: {cmd}")
		try:
			print(
				f" train percent 1 {single_run.family_run.train_percent} val percent 1 {single_run.family_run.val_percent}")
			executor = TrainSingleRun(single_run)
			executor.run_one()

			#completed = 	train_single_run.main(args)
			#completed = subprocess.run(cmd, check=False)
			# if completed != 0:
			# 	print(f"Warning: sub-run exited with code {completed}; stopping controller loop.")
			# 	break
		except Exception as sub_e:
			print(f"Error running isolated sub-process: {sub_e}  from {sub_e.args}")
			traceback.print_exception(sub_e)
			break

		family_run.current_index += 1
		if family_run.current_index >= len(family_run.single_runs_for_all_combinations):
			family_run.completed = True
		family_run.save()

	return 0


class TrainPytorchNetwork:
	"""Class wrapper around the training entry point.

	Typical usage from another module::

		from trainer.legacy_training import TrainPytorchNetwork

		runner = TrainPytorchNetwork([
			"--mode=train",
			"--prior-days=4",
			"--epochs=10",
		])
		exit_code = runner.run()
		run_dir = runner.training_dir

	The class simply delegates to the original CLI implementation while
	recording the timestamped run directory that was created.
	"""

	def __init__(self, argv: Optional[List[str]] = None) -> None:
		self._argv = argv
		self._exit_code: Optional[int] = None
		self._training_directory: Optional[str] = None
		self.family_run: Optional[FamilyRun] = None
		self.total_runtime_seconds: Optional[float] = None

		# Parse arguments eagerly so that callers can inspect the associated
		# FamilyRun controller (if any) before executing training.
		args = parse_args(argv)

		# The single_run field mirrors the controller logic from the main
		# implementation:
		#
		# * When --resume-directory is supplied, we load the existing
		#   FamilyRun state from that directory.
		# * Otherwise, when we are not already inside a run_family_* directory,
		#   we construct a fresh FamilyRun from the provided hyper-parameters.
		print(f"args are {args}")
		self.family_run = FamilyRun.from_args(args=args)
		print(f"Created FamilyRun: {self.family_run}\n\n from argss {args}")

	def run(self) -> int:
		"""Execute training with the provided arguments.

		Returns the same integer exit code as the original :func:`main`.
		"""
		start_ts = time.perf_counter()
		self._exit_code = _main_impl(self.family_run)
		self.total_runtime_seconds = float(time.perf_counter() - start_ts)





		self._training_directory = self.family_run.file_structure.family_dir
		# Human-friendly runtime log
		seconds = int(self.total_runtime_seconds)
		hours, rem = divmod(seconds, 3600)
		minutes, secs = divmod(rem, 60)
		runtime_str = f"Total training wall time: {hours:02d}:{minutes:02d}:{secs:02d} ({self.total_runtime_seconds:.2f} s)"
		print(runtime_str)

		if self._training_directory:
			try:
				runtime_file = os.path.join(self._training_directory, "total_runtime.txt")
				with open(runtime_file, "w") as f:
					f.write(runtime_str + "\n")
				print(f"Saved total runtime to {runtime_file}")
			except Exception as e:
				print(f"Warning: could not save total runtime to file: {e}")

		return int(self._exit_code or 0)

	@property
	def training_directory(self) -> Optional[str]:
		return self._training_directory


def main(argv: Optional[List[str]] = None) -> int:
	"""CLI entry point.
	This thin wrapper constructs :class:`TrainPytorchNetwork` and invokes
	:py:meth:`TrainPytorchNetwork.run`, preserving the existing public
	interface for scripts that still call :func:`main` directly.
	"""

	print(f"Running with argv: {argv}")
	runner = TrainPytorchNetwork(argv)
	return runner.run()


if __name__ == "__main__":  # pragma: no cover - script entry point
	raise SystemExit(main())
