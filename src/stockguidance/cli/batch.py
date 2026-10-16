import argparse
import json
from pathlib import Path

from stockguidance.config.batch import load_batch
from stockguidance.workflows.batch import run_batch


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Run explicit typed training, recommendation and chronological simulation steps; stop on failure.")
	parser.add_argument("--config", required=True, type=Path, help="Explicit stockguidance-batch-1 JSON; see docs/frontends.md")
	arguments = parser.parse_args(argv)
	try:
		plan = load_batch(arguments.config)
	except Exception:
		print(json.dumps({"successful": False, "failure": "invalid_batch_configuration"}))
		return 2
	result = run_batch(plan)
	print(json.dumps(result.payload(), sort_keys=True, allow_nan=False))
	return result.exit_code


if __name__ == "__main__":
	raise SystemExit(main())
