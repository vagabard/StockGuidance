import argparse
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import date
import json
import sys

from stockguidance.cli.update_arguments import add_update_policy_arguments
from stockguidance.cli.update_setup import assemble_and_update
from stockguidance.workflows.failures import StageExecutionError
from stockguidance.workflows.update_result import UpdateResult
from stockguidance.database.simplified_runtime import SimplifiedUpdateResult


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Reconcile request-scoped daily market data, interrupted imports, and corrections")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--dbinfo-file", default="dbinfo.id")
	arguments.add_argument("--start-date")
	arguments.add_argument("--end-date")
	arguments.add_argument("--repair-plan", help="Explicitly apply automatic repairs from validation JSON; manual review items are rejected")
	add_update_policy_arguments(arguments)
	arguments.set_defaults(adjustment="unadjusted")
	arguments.add_argument("--max-retries", type=int, default=3)
	arguments.add_argument("--verbose", action="store_true", help="Write updater phases, timings, and detailed failures to stderr")
	arguments.add_argument("--scan-quality", action="store_true", help="Resume the initial database quality scan and interpolate bounded bad-data gaps")
	arguments.add_argument("--force-verify", action="store_true", help="Explicitly verify historical records and source responses")
	arguments.add_argument("--download-only", action="store_true", help="Download canonical unadjusted archives without reading or writing the database")
	return arguments


def main(argv: Sequence[str] | None = None, *, run: Callable[[argparse.Namespace], UpdateResult | SimplifiedUpdateResult] = assemble_and_update) -> int:
	arguments = parser().parse_args(argv)
	try:
		if arguments.download_only:
			if arguments.start_date is None or arguments.end_date is None:
				raise ValueError("--download-only requires explicit --start-date and --end-date")
			from pathlib import Path
			from stockguidance.cli.paths import resolve_cli_path
			from stockguidance.config.source import SourceConfig
			from stockguidance.database.configuration import load_database_configuration
			from stockguidance.market_data.download_only import download_canonical
			database_path = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
			configuration = load_database_configuration(database_path)
			if configuration.data_configuration_file is None:
				raise ValueError("--download-only requires an explicit data_configuration_file")
			data_configuration = configuration.load_data_configuration()
			source = SourceConfig(data_configuration.to_source_identity(), Path(arguments.data_root).resolve(), database_path, "download-only", _configuration=data_configuration)
			report = download_canonical(source, date.fromisoformat(arguments.start_date), date.fromisoformat(arguments.end_date), refresh=arguments.refresh_source, max_retries=arguments.max_retries)
			print(json.dumps(report, sort_keys=True))
			return 0
		result = run(arguments)
	except StageExecutionError as error:
		if getattr(arguments, "verbose", False):
			print(f"[updater] failed: {error.failure.stage.value}/{error.failure.code.value}: {error.failure.message}", file=sys.stderr, flush=True)
		print(json.dumps({"successful": False, "failure": asdict(error.failure)}, sort_keys=True))
		return 1
	except ValueError as error:
		if getattr(arguments, "verbose", False):
			print(f"[updater] invalid configuration: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
		print(json.dumps({"successful": False, "error": "Invalid update configuration, policies, or supported-history metadata"}))
		return 1
	except Exception:
		print(json.dumps({"successful": False, "error": "Required updater setup or storage operation failed"}))
		return 1
	if isinstance(result, SimplifiedUpdateResult):
		print(json.dumps(result.payload(), sort_keys=True))
		return result.exit_code
	discovery = result.discovery
	history_boundary = None if discovery is not None and discovery.history_boundary is None else result.plan.boundary
	payload = {
		"successful": result.successful, "source": asdict(result.coverage.request.source),
		"start": result.coverage.request.start.isoformat(), "end": result.coverage.request.end.isoformat(),
		"as_of": result.coverage.request.as_of.isoformat(), "history_start": None if history_boundary is None else history_boundary.history_start.isoformat(),
		"history_provenance": "unresolved" if history_boundary is None else history_boundary.provenance.value,
		"excluded_before_history": result.plan.excluded_before_history, "excluded_after_cutoff": result.plan.excluded_after_cutoff,
		"data_revision": result.data_revision, "downloaded_sessions": result.downloaded_sessions,
		"stale_refreshes": [item.isoformat() for item in result.stale_refreshes],
		"provider_capability": None if discovery is None else {
			"state": discovery.provider.state.value, "checked_at": discovery.provider.checked_at.isoformat(),
			"provenance": discovery.provider.provenance, "detail": discovery.provider.detail,
			"fresh": discovery.provider_is_fresh,
			"fetchable_start": None if discovery.fetchable_start is None else discovery.fetchable_start.isoformat(),
			"fetchable_end": None if discovery.fetchable_end is None else discovery.fetchable_end.isoformat(),
		},
		"local_observed_bounds": None if discovery is None else {
			"first": None if discovery.local_bounds.first_observed is None else discovery.local_bounds.first_observed.isoformat(),
			"last": None if discovery.local_bounds.last_observed is None else discovery.local_bounds.last_observed.isoformat(),
			"observed_sessions": [item.isoformat() for item in discovery.local_bounds.observed_sessions],
		},
		"unresolved_limits": [] if discovery is None else [{"code": item.code, "severity": item.severity.value, "detail": item.detail} for item in discovery.diagnostics],
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
