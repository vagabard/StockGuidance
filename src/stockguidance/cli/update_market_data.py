import argparse
from collections.abc import Callable, Sequence
from dataclasses import asdict
import json

from stockguidance.cli.update_arguments import add_update_policy_arguments
from stockguidance.cli.update_setup import assemble_and_update
from stockguidance.workflows.failures import StageExecutionError
from stockguidance.workflows.update_result import UpdateResult


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Reconcile request-scoped daily market data, interrupted imports, and corrections")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--dbinfo-file", default="dbinfo.id")
	arguments.add_argument("--start-date")
	arguments.add_argument("--end-date")
	add_update_policy_arguments(arguments)
	arguments.add_argument("--max-retries", type=int, default=3)
	return arguments


def main(argv: Sequence[str] | None = None, *, run: Callable[[argparse.Namespace], UpdateResult] = assemble_and_update) -> int:
	arguments = parser().parse_args(argv)
	try:
		result = run(arguments)
	except StageExecutionError as error:
		print(json.dumps({"successful": False, "failure": asdict(error.failure)}, sort_keys=True))
		return 1
	except ValueError:
		print(json.dumps({"successful": False, "error": "Invalid update configuration, policies, or supported-history metadata"}))
		return 1
	except Exception:
		print(json.dumps({"successful": False, "error": "Required updater setup or storage operation failed"}))
		return 1
	payload = {
		"successful": result.successful, "source": asdict(result.coverage.request.source),
		"start": result.coverage.request.start.isoformat(), "end": result.coverage.request.end.isoformat(),
		"as_of": result.coverage.request.as_of.isoformat(), "history_start": result.plan.boundary.history_start.isoformat(),
		"excluded_before_history": result.plan.excluded_before_history, "excluded_after_cutoff": result.plan.excluded_after_cutoff,
		"data_revision": result.data_revision, "downloaded_sessions": result.downloaded_sessions,
		"sessions": [{"date": item.session.session_date.isoformat(), "state": item.state.value, "detail": item.detail} for item in result.coverage.sessions],
		"market_closures": [session_date.isoformat() for session_date in result.closed_dates],
		"no_trading": [manifest.download.session.session_date.isoformat() for manifest in result.manifests if manifest.download.response.state.value == "no_trading"],
		"source_responses": [{"date": manifest.download.session.session_date.isoformat(), "state": manifest.download.response.state.value, "http_status": manifest.download.response.http_status, "provider_status": manifest.download.response.provider_status, "counts": dict(manifest.download.response.counts), "has_more": manifest.download.response.has_more} for manifest in result.manifests],
		"failures": [asdict(failure) for failure in result.failures],
	}
	print(json.dumps(payload, sort_keys=True))
	return 0 if result.successful else 1 if result.failures else 2


if __name__ == "__main__":
	raise SystemExit(main())
