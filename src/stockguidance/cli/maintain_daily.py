import argparse
from datetime import date
import json
from typing import Callable, Sequence

from stockguidance.cli.update_arguments import add_update_policy_arguments


ZSCORE_WINDOWS = (20, 60)
MAINTENANCE_STAGES = ("source", "java", "validation")


def parse_stage(value: str) -> str:
	aliases = {"1": "source", "2": "java", "3": "validation", "download": "source", "update": "java", "validate": "validation"}
	stage = aliases.get(value.lower(), value.lower())
	if stage not in MAINTENANCE_STAGES:
		raise argparse.ArgumentTypeError(f"stage must be one of {', '.join(MAINTENANCE_STAGES)}")
	return stage


def maintenance_range(as_of: date) -> tuple[date, date]:
	from stockguidance.workflows.daily_maintenance import maintenance_range as workflow_range
	return workflow_range(as_of)


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Maintain recent market data with Java and validate independently in Python")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--dbinfo-file", default="dbinfo.id")
	add_update_policy_arguments(arguments, include_schema_initialization=False, include_history_boundary=False)
	arguments.set_defaults(adjustment=None)
	arguments.add_argument("--start-date", type=date.fromisoformat, help="Override the default two-year repair window")
	arguments.add_argument("--end-date", type=date.fromisoformat, help="Optional upper bound; unpublished sessions are excluded")
	arguments.add_argument("--java-checkpoint-directory", default="output/daily-maintenance/java")
	arguments.add_argument("--java-heap", default="4g")
	arguments.add_argument("--workers", type=int, default=3)
	arguments.add_argument("--batch-sessions", type=int, default=20)
	arguments.add_argument("--timeout-seconds", type=float, default=7200)
	arguments.add_argument("--stage", type=parse_stage, metavar="{source,java,validation}", help="Run one stage: download/recovery, Java database updates, or database validation")
	return arguments


def maintain(arguments: argparse.Namespace) -> dict[str, object]:
	from stockguidance.workflows.daily_maintenance import run_daily_maintenance
	return run_daily_maintenance(arguments)


def main(argv: Sequence[str] | None = None, *, run: Callable[[argparse.Namespace], dict[str, object]] = maintain) -> int:
	arguments = parser().parse_args(argv)
	try:
		payload = run(arguments)
	except ValueError as error:
		print(json.dumps({"successful": False, "error": str(error)}, sort_keys=True))
		return 2
	except Exception as error:
		print(json.dumps({"successful": False, "error": str(error)}, sort_keys=True))
		return 1
	print(json.dumps(payload, sort_keys=True))
	return 0 if payload["successful"] else 2


if __name__ == "__main__":
	raise SystemExit(main())
