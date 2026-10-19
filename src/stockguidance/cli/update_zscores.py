import argparse
from datetime import date, timedelta
import json
from typing import Callable, Sequence

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.paths import resolve_cli_path
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.repositories.mysql_sessions import ConnectionFactory, MySQLSessionRepository
from stockguidance.database.repositories.mysql_zscores import MySQLZScoreRepository
from stockguidance.database.table_names import TableNames
from stockguidance.database.update_schema import initialize_zscore_schema
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance, UnknownHistoryBoundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.workflows.update_zscores import ZScoreUpdateResult, ZScoreUpdateSettings, update_zscores
from stockguidance.cli.zscore_args import parse_windows as _parse_windows


def parse_windows(value: str) -> tuple[int, ...]:
	return _parse_windows(value, "--windows")


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Populate local verified rolling z-score materializations without provider access")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--dbinfo-file", default="dbinfo.id")
	arguments.add_argument("--adjustment", choices=("adjusted", "unadjusted"), default="adjusted")
	arguments.add_argument("--windows", required=True, type=parse_windows, help="Distinct rolling windows, for example 20,60")
	arguments.add_argument("--start-date", required=True, type=date.fromisoformat)
	arguments.add_argument("--end-date", required=True, type=date.fromisoformat)
	arguments.add_argument("--history-start")
	arguments.add_argument("--history-evidence")
	arguments.add_argument("--publication-delay-minutes", required=True, type=float)
	arguments.add_argument("--publication-evidence", required=True)
	arguments.add_argument("--initialize-schema", action="store_true", help="Explicitly create only the selected optional z-score tables")
	return arguments


def _calendar(arguments: argparse.Namespace, clock: SystemClock) -> ExchangeCalendar:
	history_start = None if arguments.history_start is None else date.fromisoformat(arguments.history_start)
	start = min(date(1970, 1, 1), *(item for item in (history_start,) if item is not None))
	end = max(arguments.end_date, clock.now().date()) + timedelta(days=14)
	return ExchangeCalendar("XNYS", start, end)


def _boundary(
	arguments: argparse.Namespace,
	source: SourceConfig,
	calendar: ExchangeCalendar,
	clock: SystemClock,
	table_names: TableNames,
	connect: ConnectionFactory,
) -> HistoryBoundary:
	cutoff = clock.now()
	if arguments.history_start is not None:
		if arguments.history_evidence is None:
			raise ValueError("An explicit history start requires --history-evidence")
		return HistoryBoundary(source.identity, calendar.calendar_id, calendar.session(date.fromisoformat(arguments.history_start)).session_date, HistoryProvenance.EXPLICIT_BOUNDARY, arguments.history_evidence, cutoff)
	stored = JsonHistoryStore(source.data_root / "history").load(source.identity)
	if stored is not None:
		stored.validate_for(source.identity, calendar)
		return stored
	manifest_inventory = ManifestStore(source.data_root).inventory(source.identity, calendar, start=calendar.start, end=calendar.end)
	database_inventory_error: Exception | None = None
	try:
		database_inventory = MySQLSessionRepository(source, connect, table_names).inventory(calendar.start, calendar.end, calendar.calendar_id)
	except Exception as error:
		database_inventory = None
		database_inventory_error = error
	local_dates = [item.session_date for item in manifest_inventory.observations]
	if database_inventory is not None:
		local_dates.extend(item.session_date for item in database_inventory.observations)
	if not local_dates:
		if database_inventory_error is not None:
			raise RuntimeError("Required updater schema or local database history could not be inspected") from None
		raise UnknownHistoryBoundary("Supported history is unknown; supply --history-start and --history-evidence")
	first = min(local_dates)
	return HistoryBoundary(source.identity, calendar.calendar_id, calendar.session(first).session_date, HistoryProvenance.SOURCE_HISTORY, "Earliest retained local observation; not a provider coverage claim", cutoff)


def assemble_and_update_zscores(arguments: argparse.Namespace) -> ZScoreUpdateResult:
	clock = SystemClock()
	identity = SourceIdentity("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, PriceAdjustment(arguments.adjustment))
	source = SourceConfig(identity, resolve_cli_path(arguments.data_root, "data"), resolve_cli_path(arguments.dbinfo_file, "dbinfo.id"), "unused-by-local-z-score-update")
	database_configuration = load_database_configuration(source.database_config)
	table_names = database_configuration.load_table_names()
	connect = MySQLConnectionFactory()
	calendar = _calendar(arguments, clock)
	boundary = _boundary(arguments, source, calendar, clock, table_names, connect)
	publication = DailyPublicationPolicy(timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence)
	settings = ZScoreUpdateSettings(arguments.start_date, arguments.end_date, arguments.windows, publication, arguments.initialize_schema)
	if settings.initialize_schema:
		initialize_zscore_schema(source, connect, settings.windows, table_names)
	repository = MySQLZScoreRepository(source, connect, table_names, ManifestStore(source.data_root))
	return update_zscores(source, settings, boundary, calendar, repository, clock, initialized_schema=settings.initialize_schema)


def _payload(result: ZScoreUpdateResult) -> dict[str, object]:
	return {
		"successful": result.exit_code == 0,
		"complete": result.complete,
		"exit_code": result.exit_code,
		"requested_scope": [item.session_date.isoformat() for item in result.requested_sessions],
		"effective_scope": [item.session_date.isoformat() for item in result.effective_sessions],
		"warmup_scope": [item.session_date.isoformat() for item in result.warmup_sessions],
		"windows": result.windows,
		"cutoff": result.cutoff.isoformat(),
		"initialized_tables": result.initialized_schema,
		"written_sessions": [item.session_date.isoformat() for item in result.written_sessions],
		"skipped_sessions": [{"date": item.session.session_date.isoformat(), "reason": item.reason} for item in result.skipped_sessions],
		"skipped_results": [{"family": item.family.value, "window": item.window, "date": item.session.session_date.isoformat()} for item in result.skipped_results],
		"rebuilt_results": [{"family": item.family.value, "window": item.window, "date": item.session.session_date.isoformat()} for item in result.rebuilt_results],
		"ready_count": result.ready_count,
		"unavailable_count": result.unavailable_count,
		"status_counts": [{"family": family.value, "window": window, "status": int(status), "count": count} for family, window, status, count in result.status_counts],
		"failures": result.failures,
	}


def main(argv: Sequence[str] | None = None, *, run: Callable[[argparse.Namespace], ZScoreUpdateResult] = assemble_and_update_zscores) -> int:
	arguments = parser().parse_args(argv)
	try:
		result = run(arguments)
	except UnknownHistoryBoundary as error:
		print(json.dumps({"successful": False, "complete": False, "exit_code": 2, "error": str(error)}))
		return 2
	except ValueError:
		print(json.dumps({"successful": False, "complete": False, "exit_code": 2, "error": "Invalid z-score update configuration, cutoff, or supported-history metadata"}))
		return 2
	except Exception:
		print(json.dumps({"successful": False, "complete": False, "exit_code": 1, "error": "Z-score storage or local input inspection failed"}))
		return 1
	print(json.dumps(_payload(result), sort_keys=True))
	return result.exit_code


if __name__ == "__main__":
	raise SystemExit(main())
