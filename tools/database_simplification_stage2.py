from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
	sys.path.insert(0, str(SRC))

from stockguidance.performance.database_simplification_stage2 import (  # noqa: E402
	DEFAULT_BATCH_SIZE,
	DEFAULT_REPETITIONS,
	DEFAULT_SCHEMA,
	DEFAULT_SEED,
	DEFAULT_SESSION_COUNT,
	DEFAULT_TICKER_COUNT,
	DEFAULT_WRITE_ROWS,
	incomplete_report,
	run_benchmark,
	write_report,
	write_summary,
)


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Run the disposable-MySQL Stage 2 raw-state placement benchmark")
	parser.add_argument("--dbinfo-file", type=Path, help="Explicit MySQL credentials/configuration for a disposable benchmark server")
	parser.add_argument("--schema", default=DEFAULT_SCHEMA, help="Disposable schema; must match stockguidance_disposable_*")
	parser.add_argument("--output", type=Path, default=Path("output/database-simplification/benchmark/stage2-measurements.json"))
	parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
	parser.add_argument("--sessions", type=int, default=DEFAULT_SESSION_COUNT)
	parser.add_argument("--tickers", type=int, default=DEFAULT_TICKER_COUNT)
	parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
	parser.add_argument("--write-rows", type=int, default=DEFAULT_WRITE_ROWS)
	parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
	parser.add_argument("--keep-schema", action="store_true", help="Keep the validated disposable schema for inspection")
	arguments = parser.parse_args(argv)
	if arguments.dbinfo_file is None:
		report = incomplete_report("No disposable MySQL configuration was supplied; no database was contacted.", schema=arguments.schema, seed=arguments.seed, session_count=arguments.sessions, ticker_count=arguments.tickers, repetitions=arguments.repetitions)
	else:
		report = run_benchmark(arguments.dbinfo_file, arguments.schema, seed=arguments.seed, session_count=arguments.sessions, ticker_count=arguments.tickers, repetitions=arguments.repetitions, write_rows=arguments.write_rows, batch_size=arguments.batch_size, keep_schema=arguments.keep_schema)
	write_report(report, arguments.output)
	write_summary(report, arguments.output.with_name("stage2-report.md"))
	print(json.dumps(report, indent=2, sort_keys=True))
	return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
	raise SystemExit(main())
