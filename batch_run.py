from __future__ import annotations

from typing import List, Optional

from incremental_training_harness import main as incremental_main
from train_pytorch_network import main as train_main
from choose_next_day_stocks import main as choose_main


# ---------------------------------------------------------------------------
# Configuration section
# ---------------------------------------------------------------------------
#
# You can edit the two lists below to control the arguments passed to each
# underlying script. This lets you keep a *single* run configuration (or
# command) while still using different options for:
#
#   1. The main training run (``train_pytorch_network.main``)
#   2. The incremental harness (``incremental_training_harness.main``)
#
# The strings are exactly what you would type on the command line, but
# without the program name. For example::
#
#   TRAIN_MAIN_ARGS = [
#       "--mode=train",
#       "--prior-days=4",
#       "--epochs=50",
#   ]
#
#   INCREMENTAL_MAIN_ARGS = [
#       "--training-days=200",
#       "--simulation-days=40",
#       "--prior-days=4",
#       "--epochs=10",
#   ]
#
# Feel free to adjust these defaults to whatever you normally use.

TRAIN_MAIN_ARGS: List[str] = [
	"--shuffle",
	"--precision fp32",
	"--epochs 5000",
	"--lr 0.0000001",
	"--hidden-layers 6000",
	"--mlp-layers 4",
	"--simulate-after",
	"--day-change-centered zero",
	"--activation leaky_relu",
	"--final-activation sigmoid",
	"--run-repeats 1",
	"--trading-interval 50",
]
"""Arguments for :func:`train_pytorch_network.main`.

Populate this list with the CLI flags you want the *initial* training run to
see. Leave it empty if you prefer to call ``train_pytorch_network.py``
directly instead of through this batch wrapper.
"""

TRAIN_REPEATS: int = 1
"""How many times to rerun the main training step with the same arguments.

Use a positive integer. Values less than 1 are treated as 1 when running.
"""

INCREMENTAL_MAIN_ARGS: List[str] = [
	"--training-days 1000",
	"--simulation-days 50",
	"--mlp-layers 4",
	"--lr 0.0000003",
	"--epochs 150",
]
"""Arguments for :func:`incremental_training_harness.main`.

Populate this list with the CLI flags you want the incremental harness to
use. These can be completely different from :data:`TRAIN_MAIN_ARGS`.
"""

INCREMENTAL_REPEATS: int = 1
"""How many times to rerun the incremental simulation step.

Use a positive integer. Values less than 1 are treated as 1 when running.
"""

CHOOSE_MAIN_ARGS: List[str] = [
	"--ensemble 5",
	"--ensemble-normalize"
	]

CHOOSE_MAIN_ARGS2: List[str] = [
	"--ensemble 5",
	]
"""Arguments for :func:`choose_next_day_stocks.main`.

Populate this list with the CLI flags you want the recommendations step to
see. Leave it empty to rely on that script's own defaults and environment
variables (for example ``POLYGON_API_KEY``).
"""

CHOOSE_REPEATS: int = 3
"""How many times to rerun the choose_next_day_stocks step.

Use a positive integer. Values less than 1 are treated as 1 when running.
"""

def _normalize_argv(arg_chunks: List[str]) -> List[str]:
	"""Expand a "human-friendly" argument list into a real ``argv``.

	Each element of ``arg_chunks`` may contain either a single CLI token
	(e.g. ``"--shuffle"``) *or* a short space-separated fragment such as
	``"--precision fp32"`` or ``"--epochs 500"``.  This helper splits each
	string on ASCII whitespace and flattens the result so that the final
	list matches what :mod:`argparse` expects, for example::

		["--precision fp32", "--epochs 500"]
		-> ["--precision", "fp32", "--epochs", "500"]

	This keeps the configuration convenient to edit while avoiding
	"unrecognized arguments" errors from the underlying scripts.
	"""

	normalized: List[str] = []
	for chunk in arg_chunks:
		# ``split()`` with no arguments splits on any whitespace and ignores
		# repeats, which is exactly what we want here.
		for token in str(chunk).split():
			if token:
				normalized.append(token)
	return normalized


def main(argv: Optional[List[str]] = None) -> int:
	"""Run a full batch: training, incremental simulation, then recommendations.

	Unlike the initial implementation (which forwarded the *same* CLI
	arguments to both scripts), this version is driven by two in‑file
	configuration lists for the training-related steps:

	* :data:`TRAIN_MAIN_ARGS` for :func:`train_pytorch_network.main`
	* :data:`INCREMENTAL_MAIN_ARGS` for :func:`incremental_training_harness.main`
	* :data:`CHOOSE_MAIN_ARGS` for :func:`choose_next_day_stocks.main`

	``argv`` is currently ignored; the intention is that you configure the
	per‑script arguments directly in this file so they can differ.
	The final recommendations step calls :func:`choose_next_day_stocks.main`
	using whatever CLI/environment configuration that script expects.
	"""
	# Finally, run the end‑to‑end "choose next day stocks" pipeline to
	# produce fresh recommendations (and a recommendations-<timestamp>.txt
	# file as configured in that script).
	choose_argv = _normalize_argv(CHOOSE_MAIN_ARGS)
	choose_argv2 = _normalize_argv(CHOOSE_MAIN_ARGS2)
	repeats = CHOOSE_REPEATS if CHOOSE_REPEATS > 0 else 1
	for i in range(1, repeats + 1):
		print(f"=== choose_next_day_stocks run {i}/{repeats} ===")
		# Pass any configured arguments through; if none are configured, call
		# with an empty list so that choose_next_day_stocks.main relies on its
		# own default values and environment variables.
		choose_code = int(choose_main(choose_argv))
		print("choose code" + str(choose_code))
		choose_code = int(choose_main(choose_argv2))
	#		if choose_code != 0:
	#			return choose_code

	# First run the main training script (if configured).
	train_argv = _normalize_argv(TRAIN_MAIN_ARGS)
	if train_argv:
		repeats = TRAIN_REPEATS if TRAIN_REPEATS > 0 else 1
		for i in range(1, repeats + 1):
			print(f"=== Training run {i}/{repeats} ===")
			train_code = int(train_main(train_argv))
			# if train_code != 0:
			# 	# Propagate non‑zero exit so that failures are visible to the caller
			# 	# (e.g. IDE run configuration, CI job, shell script, etc.).
			# 	return train_code

	# Then run the incremental harness (if configured).
	inc_argv = _normalize_argv(INCREMENTAL_MAIN_ARGS)
	if inc_argv:
		repeats = INCREMENTAL_REPEATS if INCREMENTAL_REPEATS > 0 else 1
		for i in range(1, repeats + 1):
			print(f"=== Incremental simulation run {i}/{repeats} ===")
			inc_code = int(incremental_main(inc_argv))
			if inc_code != 0:
				return inc_code


	return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation
	raise SystemExit(main())
