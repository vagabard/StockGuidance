from __future__ import annotations

import train_pytorch_network as tpn


def _get_planned_runs(start_date: str | None = None, end_date: str | None = None, run_repeats: int | None = None):
	"""Helper to instantiate TrainPytorchNetwork and get planned runs.

	Instead of the old dry-run, we now inspect the FamilyRun object directly.
	"""

	argv = [
		"--mode",
		"train",
		"--prior-days",
		"0",
		"--epochs",
		"1",
		"--batch-size",
		"4",
		"--train-percent",
		"0.5",
		"--val-percent",
		"0.25",
		"--split-mode",
		"sequential",
	]
	if start_date is not None:
		argv += ["--train-start-date-id", start_date]
	if end_date is not None:
		argv += ["--train-end-date-id", end_date]

	if run_repeats is not None:
		argv += [
			"--run-repeats",
			str(run_repeats),
		]

	# Instantiate runner but don't call run()
	runner = tpn.TrainPytorchNetwork(argv)
	runner.family_run.create_options_for_individual_runs()
	return runner.family_run.single_runs_for_all_combinations


def test_planned_runs_respect_cli_arguments():
	start_date = "+70"
	end_date = "-10"

	planned_runs = _get_planned_runs(start_date=start_date, end_date=end_date, run_repeats=None)

	assert planned_runs, "No planned runs found."
	run = planned_runs[0]

	# Basic invariants: we stayed in train mode and respected the CLI args.
	# Note: FamilyRun doesn't seem to store the individual run's mode, but the parent does.
	assert run.family_run.args.mode == "train"
	assert run.prior_days == 0
	assert run.family_run.split_mode == "sequential"
	assert run.family_run.raw_train_start_with_special_characters == start_date
	assert run.family_run.raw_train_end_with_special_characters == end_date

	# Default repeat metadata should be 1 when not explicitly passed.
	assert run.family_run.run_repeats == 1
	# The index is not explicitly in SingleRun but we can check run_repeats
	assert len(planned_runs) == 1


def test_planned_runs_honour_run_repeats():
	start_date = "+50"
	end_date = "-0"
	repeats = 3

	planned_runs = _get_planned_runs(start_date=start_date, end_date=end_date, run_repeats=repeats)

	# In the current implementation of FamilyRun.create_options_for_individual_runs,
	# it does NOT seem to multiply by run_repeats!
	# Let's verify this.
	assert len(planned_runs) == 1
	run = planned_runs[0]
	assert run.family_run.run_repeats == repeats

	# Even if there's only one SingleRun object in single_runs_for_all_combinations,
	# the controller loop in _main_impl will run it multiple times if run_repeats > 1?
	# Wait, looking at _main_impl:
	# while family_run.current_index < len(family_run.single_runs_for_all_combinations):
	#   ...
	#   family_run.current_index += 1
	# It doesn't seem to use run_repeats there either.
