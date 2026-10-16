import argparse
import json
from pathlib import Path

from stockguidance.training.jobs import execute_job, load_job
from stockguidance.reporting.training import training_payload


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Train previous-close inputs against mature next-session close returns.")
	parser.add_argument("--config", required=True, type=Path, help="Explicit training job JSON; see docs/model_training.md")
	parser.add_argument("--artifact", required=True, type=Path, help="Destination self-contained .sgmodel artifact")
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
	try:
		result = execute_job(job, arguments.artifact, checkpoint_path=arguments.checkpoint, resume_from=arguments.resume)
		print(json.dumps({"successful": True, **training_payload(result)}, sort_keys=True, allow_nan=False))
		return 0
	except Exception:
		print(json.dumps({"successful": False, "failure": "training_or_export_failed"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
