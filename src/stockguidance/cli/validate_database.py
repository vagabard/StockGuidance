import argparse
from collections.abc import Callable, Sequence
import json

from stockguidance.cli.validation_setup import assemble_and_validate
from stockguidance.market_data.history import UnknownHistoryBoundary
from stockguidance.validation.reporting import render_text, report_payload
from stockguidance.validation.result import ValidationResult


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Read-only request-scoped database validation; no downloads, writes, or automatic repairs")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--dbinfo-file", default="dbinfo.id")
	arguments.add_argument("--start-date")
	arguments.add_argument("--end-date")
	arguments.add_argument("--history-start")
	arguments.add_argument("--history-evidence", help="Provenance for an explicit earliest provider-history boundary")
	arguments.add_argument("--adjustment", choices=("adjusted", "unadjusted"), default="adjusted")
	arguments.add_argument("--as-of", help="Timezone-aware ISO cutoff, no later than the current clock")
	arguments.add_argument("--publication-delay-minutes", type=float)
	arguments.add_argument("--publication-evidence", help="Provenance for the expected post-close publication delay")
	arguments.add_argument("--format", choices=("text", "json"), default="text")
	arguments.add_argument("--ticker-ids", help="Comma-separated exact ordered model universe; requires --lookback-sessions")
	arguments.add_argument("--lookback-sessions", type=int, help="Exact historical close window; requires --ticker-ids")
	arguments.add_argument("--signal-mode", choices=("previous_close", "entry_open"), default="previous_close", help="entry_open also audits read-only opening receipts for the full universe")
	return arguments


def main(argv: Sequence[str] | None = None, *, run: Callable[[argparse.Namespace], ValidationResult] = assemble_and_validate) -> int:
	arguments = parser().parse_args(argv)
	error = None
	try:
		result = run(arguments)
	except UnknownHistoryBoundary:
		error = "Supported history is unknown; supply --history-start and --history-evidence or establish source-specific history metadata with the updater"
	except ValueError:
		error = "Invalid validation configuration, cutoff, or supported-history metadata"
	except Exception:
		error = "Required validation setup or storage inspection failed"
	if error is not None:
		print(json.dumps({"complete": False, "failed": True, "exit_code": 1, "error": error}) if arguments.format == "json" else f"Database validation failed: {error}")
		return 1
	print(json.dumps(report_payload(result), sort_keys=True) if arguments.format == "json" else render_text(result))
	return result.exit_code


if __name__ == "__main__":
	raise SystemExit(main())
