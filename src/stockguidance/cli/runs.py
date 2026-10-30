"""Inspect saved runs without database, network, Torch, or training imports."""

import argparse
import json
from pathlib import Path

from stockguidance.runs.service import RunDirectoryService


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Read, list, or validate saved StockGuidance runs without executing them.")
	parser.add_argument("--runs-root", type=Path, default=None)
	commands = parser.add_subparsers(dest="command", required=True)
	list_command = commands.add_parser("list")
	list_command.add_argument("--valid-only", action="store_true")
	for name in ("read", "validate"):
		command = commands.add_parser(name)
		command.add_argument("run_id")
	arguments = parser.parse_args(argv)
	service = RunDirectoryService(arguments.runs_root)
	try:
		if arguments.command == "list":
			results = []
			for item in service.list_runs(include_invalid=not arguments.valid_only):
				results.append({"run_id": item.run_id, "path": str(item.path), "valid": item.valid, "status": None if item.manifest is None else item.manifest.status.value, "operation": None if item.manifest is None else item.manifest.operation.value, "error": item.error})
			payload: object = {"successful": True, "results": results}
		else:
			manifest = service.validate(arguments.run_id) if arguments.command == "validate" else service.read(arguments.run_id)
			payload = {"successful": True, "manifest": manifest.to_mapping()}
		print(json.dumps(payload, sort_keys=True, allow_nan=False))
		return 0
	except Exception:
		print(json.dumps({"successful": False, "failure": "Invalid, incomplete, or unavailable saved run"}, sort_keys=True))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
