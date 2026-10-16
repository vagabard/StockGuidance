import argparse
import json
from pathlib import Path

from stockguidance.simulation.jobs import load_configuration
from stockguidance.workflows.signal_comparison_runtime import assemble_signal_comparison


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Matched-cohort chronological comparison of previous-close and full-universe entry-opening signals; identical capital, schedule, seed and training budget.")
	parser.add_argument("--config", required=True, type=Path, help="Simulation JSON with chronological retraining settings")
	parser.add_argument("--artifacts-directory", required=True, type=Path)
	arguments = parser.parse_args(argv)
	try:
		configuration = load_configuration(arguments.config)
		if configuration.retraining is None:
			raise ValueError("Missing retraining configuration")
	except Exception:
		print(json.dumps({"successful": False, "failure": "Invalid chronological signal-comparison configuration"}))
		return 2
	try:
		comparison = assemble_signal_comparison(configuration, arguments.artifacts_directory)
		print(json.dumps(comparison.payload(), sort_keys=True, allow_nan=False))
		return 0 if comparison.previous_close.complete and comparison.entry_open.complete else 2 if comparison.previous_close.successful and comparison.entry_open.successful else 1
	except Exception:
		print(json.dumps({"successful": False, "failure": "Required paired historical signal comparison failed"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
