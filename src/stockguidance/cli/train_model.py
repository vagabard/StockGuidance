import argparse
import json
from pathlib import Path
from typing import cast

from stockguidance.training.jobs import execute_job, load_job
from stockguidance.reporting.training import training_payload


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Train explicit previous_close or entry_open inputs against mature next-session close returns.")
	parser.add_argument("--config", required=True, type=Path, help="Explicit training job JSON; see docs/model_training.md")
	parser.add_argument("--artifact", type=Path, help="Destination self-contained .sgmodel artifact")
	parser.add_argument("--preview", action="store_true", help="Resolve and validate a v2 job without fitting, loading data, or exporting")
	parser.add_argument("--checkpoint", type=Path, help="Separate resumable training state destination")
	parser.add_argument("--resume", type=Path, help="Explicit compatible checkpoint; epochs is the new total")
	return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
	arguments = parse_args(argv)
	try:
		job = load_job(arguments.config)
	except Exception:
		print(json.dumps({"successful": False, "failure": "invalid_training_configuration"}))
		return 2
	if arguments.preview:
		if not hasattr(job, "date_plan"):
			print(json.dumps({"successful": False, "failure": "preview_requires_training_job_v2"}))
			return 2
		try:
			from stockguidance.training.configured import ConfiguredTrainingJob, resolve_configured_training
			print(json.dumps({"successful": True, **resolve_configured_training(cast(ConfiguredTrainingJob, job)).to_mapping()}, sort_keys=True, allow_nan=False, default=str))
			return 0
		except Exception:
			print(json.dumps({"successful": False, "failure": "training_preview_failed"}))
			return 2
	if arguments.artifact is None:
		print(json.dumps({"successful": False, "failure": "artifact_required_for_training"}))
		return 2
	try:
		result = execute_job(job, arguments.artifact, checkpoint_path=arguments.checkpoint, resume_from=arguments.resume)
		print(json.dumps({"successful": True, **training_payload(result)}, sort_keys=True, allow_nan=False))
		return 0
	except Exception:
		print(json.dumps({"successful": False, "failure": "training_or_export_failed"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
