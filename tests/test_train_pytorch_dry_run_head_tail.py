from __future__ import annotations

import train_pytorch_network as tpn


def _run_main_with_args(capsys, head: float, tail: float, run_repeats: int | None = None):
	"""Helper to invoke train_pytorch_network.main under dry-run.

	In the new semantics, dry-run should never construct providers or touch
	the database. It should instead walk the planned combination space and
	print a summary line for each configuration that *would* be run.
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
		"--data-head-ignore-percent",
		str(head),
		"--data-tail-ignore-percent",
		str(tail),
	]
	if run_repeats is not None:
		argv += [
			"--run-repeats",
			str(run_repeats),
		]
	argv += [
		"--dry-run",
	]

	# Call main with our argv and capture output.
	ret = tpn.main(argv)
	assert ret == 0 or ret is None
	out, err = capsys.readouterr()
	return out + err


def test_dry_run_lists_planned_run_arguments(capsys):
	head = 0.70
	tail = 0.10

	output = _run_main_with_args(capsys, head=head, tail=tail, run_repeats=None)

	# Find DRY-RUN lines and parse the first one.
	lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
	dry_lines = [ln for ln in lines if ln.startswith("DRY-RUN:")]
	assert dry_lines, f"No DRY-RUN output found. Got lines: {lines!r}"
	line = dry_lines[0]

	# The format is approximately:
	# DRY-RUN: mode=train day_change_centered=one prior_days=0 mlp_layers=3 lr=0.001 ...
	parts = line.replace("DRY-RUN:", "").strip().split()
	parsed = {}
	for p in parts:
		if "=" not in p:
			continue
		k, v = p.split("=", 1)
		parsed[k] = v

	# Basic invariants: we stayed in train mode and respected the CLI args.
	assert parsed["mode"] == "train"
	assert parsed["prior_days"] == "0"
	assert parsed["split_mode"] == "sequential"
	assert float(parsed["data_head_ignore_percent"]) == head
	assert float(parsed["data_tail_ignore_percent"]) == tail
	# Default repeat metadata should be 1 when not explicitly passed.
	assert int(parsed["run_repeats"]) == 1
	assert int(parsed["run_repeat_index"]) == 1


def test_dry_run_honours_run_repeats_and_indices(capsys):
	head = 0.5
	tail = 0.0
	repeats = 3

	output = _run_main_with_args(capsys, head=head, tail=tail, run_repeats=repeats)

	lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
	dry_lines = [ln for ln in lines if ln.startswith("DRY-RUN:")]
	# With a single combination and run_repeats=3, we expect exactly 3 DRY-RUN lines.
	assert len(dry_lines) == repeats

	seen_indices = set()
	for line in dry_lines:
		parts = line.replace("DRY-RUN:", "").strip().split()
		parsed: dict[str, str] = {}
		for p in parts:
			if "=" not in p:
				continue
			k, v = p.split("=", 1)
			parsed[k] = v

		assert parsed["mode"] == "train"
		assert float(parsed["data_head_ignore_percent"]) == head
		assert float(parsed["data_tail_ignore_percent"]) == tail
		# All repeats should advertise the same run_repeats value.
		assert int(parsed["run_repeats"]) == repeats
		idx = int(parsed["run_repeat_index"])
		assert 1 <= idx <= repeats
		seen_indices.add(idx)

	# Ensure that we saw each repeat index at least once.
	assert seen_indices == set(range(1, repeats + 1))
