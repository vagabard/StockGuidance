"""Inspect saved runs without database, network, Torch, or training imports."""

import argparse
import json
from pathlib import Path

from stockguidance.reporting.inspection import compare_saved_run_ids, inspect_saved_run, list_saved_runs
from stockguidance.runs.contracts import RunOperation, RunStatus
from stockguidance.runs.service import RunDirectoryService


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Read, list, or validate saved StockGuidance runs without executing them.")
	parser.add_argument("--runs-root", type=Path, default=None)
	commands = parser.add_subparsers(dest="command", required=True)
	list_command = commands.add_parser("list")
	list_command.add_argument("--valid-only", action="store_true")
	list_command.add_argument("--operation", choices=[item.value for item in RunOperation], default=None)
	list_command.add_argument("--status", choices=[item.value for item in RunStatus], default=None)
	list_command.add_argument("--parent-run-id", default=None)
	for name in ("read", "open", "validate"):
		command = commands.add_parser(name)
		command.add_argument("run_id")
		command.add_argument("--validate-artifacts", action="store_true")
	compare = commands.add_parser("compare")
	compare.add_argument("run_ids", nargs="+")
	compare.add_argument("--validate-artifacts", action="store_true")
	arguments = parser.parse_args(argv)
	service = RunDirectoryService(arguments.runs_root)
	try:
		if arguments.command == "list":
			results = []
			operation = None if arguments.operation is None else RunOperation(arguments.operation)
			status = None if arguments.status is None else RunStatus(arguments.status)
			for item in list_saved_runs(service, operation=operation, status=status, parent_run_id=arguments.parent_run_id, include_invalid=not arguments.valid_only):
				results.append({"run_id": item.run_id, "path": str(item.path), "valid": item.valid, "status": None if item.manifest is None else item.manifest.status.value, "operation": None if item.manifest is None else item.manifest.operation.value, "error": item.error})
			payload: object = {"successful": True, "results": results}
		elif arguments.command == "compare":
			comparison = compare_saved_run_ids(service, arguments.run_ids, validate=arguments.validate_artifacts)
			payload = {"successful": True, "comparison": comparison.to_mapping()}
		else:
			if arguments.command == "validate":
				manifest = service.validate(arguments.run_id)
				payload = {"successful": True, "manifest": manifest.to_mapping()}
			else:
				inspection = inspect_saved_run(service, arguments.run_id, validate=arguments.validate_artifacts)
				payload = {"successful": True, "manifest": inspection.manifest.to_mapping(), "inspection": inspection.to_mapping()}
		print(json.dumps(payload, sort_keys=True, allow_nan=False))
		return 0
	except Exception:
		print(json.dumps({"successful": False, "failure": "Invalid, incomplete, or unavailable saved run"}, sort_keys=True))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
