import argparse
import json
from pathlib import Path

from stockguidance.datasets.training import build_training_dataset
from stockguidance.diagnostics.correlations import target_correlations
from stockguidance.diagnostics.features import inspect_features
from stockguidance.targets.specification import TargetSpec
from stockguidance.training.jobs import load_job, open_job_history


def main(argv: list[str] | None = None, *, default_kind: str = "features") -> int:
	parser = argparse.ArgumentParser(description="Read-only previous-close feature inspection or mature close-to-close correlations from an explicit training job.")
	parser.add_argument("--config", type=Path, required=True)
	parser.add_argument("--kind", choices=("features", "correlations"), default=default_kind)
	parser.add_argument("--sample-count", type=int, default=5)
	parser.add_argument("--seed", type=int, default=0)
	arguments = parser.parse_args(argv)
	try:
		job = load_job(arguments.config)
		if arguments.sample_count <= 0 or arguments.seed < 0:
			raise ValueError("Invalid diagnostic sampling")
	except Exception:
		print(json.dumps({"successful": False, "failure": "invalid_diagnostic_configuration"}))
		return 2
	try:
		history = open_job_history(job)
		dataset = build_training_dataset(history.decisions, job.cutoff, job.features, TargetSpec(), history.boundary, history.calendar, job.publication, history.repository)
		payload = inspect_features(dataset, arguments.sample_count, arguments.seed) if arguments.kind == "features" else target_correlations(dataset)
		print(json.dumps({"successful": True, **payload}, sort_keys=True, allow_nan=False))
		return 0
	except Exception:
		print(json.dumps({"successful": False, "failure": "Required historical diagnostic data is unavailable"}))
		return 1


def correlation_main(argv: list[str] | None = None) -> int:
	return main(argv, default_kind="correlations")


if __name__ == "__main__":
	raise SystemExit(main())
