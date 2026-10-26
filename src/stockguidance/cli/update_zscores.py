import argparse
from datetime import date, timedelta
import json
from pathlib import Path
from typing import Callable, Sequence

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.paths import resolve_cli_path
from stockguidance.config.source import DEFAULT_CALENDAR_ID, DataConfiguration, PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.configuration import DatabaseConfiguration, load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.repositories.mysql_sessions import MySQLSessionRepository
from stockguidance.database.repositories.mysql_zscores import MySQLZScoreRepository
from stockguidance.database.table_names import TableNames
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.database.simplified_contract import OperationKind
from stockguidance.database.simplified_runtime import SimplifiedRuntimeError, target_schema_state
from stockguidance.database.update_schema import initialize_zscore_schema
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance, UnknownHistoryBoundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.canonical_store import archive_store
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.workflows.update_zscores import ZScoreUpdateResult, ZScoreUpdateSettings, update_zscores
from stockguidance.cli.zscore_args import parse_windows as _parse_windows
from stockguidance.market_data.quality import HistoryMode, QualityPolicy


def parse_windows(value: str) -> tuple[int, ...]:
	return _parse_windows(value, "--windows")


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Populate local verified rolling z-score materializations without provider access")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--quality-history", choices=("clean", "interpolated"), default="clean")
	arguments.add_argument("--retrospective-quality", action="store_true", help="Permit future-informed repairs and flag affected results with FUTURE_LEAKAGE")
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


def _calendar(arguments: argparse.Namespace, clock: SystemClock, calendar_id: str = DEFAULT_CALENDAR_ID) -> ExchangeCalendar:
	history_start = None if arguments.history_start is None else date.fromisoformat(arguments.history_start)
	start = min(date(1970, 1, 1), *(item for item in (history_start,) if item is not None))
	end = max(arguments.end_date, clock.now().date()) + timedelta(days=14)
	return ExchangeCalendar(calendar_id, start, end)


def _boundary(
	arguments: argparse.Namespace,
	source: SourceConfig,
	calendar: ExchangeCalendar,
	clock: SystemClock,
	table_names: TableNames,
	connect: WriteConnectionFactory,
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
	manifest_inventory = archive_store(source, connect).inventory(source.identity, calendar, start=calendar.start, end=calendar.end)
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


def assemble_and_update_zscores(arguments: argparse.Namespace) -> ZScoreUpdateResult | dict[str, object]:
	clock = SystemClock()
	database_path = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
	database_configuration = load_database_configuration(database_path)
	table_names = database_configuration.load_table_names()
	if target_schema_state(MySQLConnectionFactory(), database_path, table_names) == "target":
		return assemble_simplified_zscores(arguments, database_path, database_configuration, table_names)
	if database_configuration.data_configuration_file is None:
		data_configuration = DataConfiguration("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, PriceAdjustment(arguments.adjustment) is PriceAdjustment.ADJUSTED, False)
	else:
		data_configuration = database_configuration.load_data_configuration()
	identity = data_configuration.to_source_identity()
	source = SourceConfig(identity, resolve_cli_path(arguments.data_root, "data"), database_path, "unused-by-local-z-score-update", _configuration=data_configuration)
	connect = MySQLConnectionFactory()
	calendar = _calendar(arguments, clock, data_configuration.calendar_id)
	boundary = _boundary(arguments, source, calendar, clock, table_names, connect)
	publication = DailyPublicationPolicy(timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence)
	policy = QualityPolicy(HistoryMode(arguments.quality_history), arguments.retrospective_quality)
	settings = ZScoreUpdateSettings(arguments.start_date, arguments.end_date, arguments.windows, publication, arguments.initialize_schema, policy)
	if settings.initialize_schema:
		initialize_zscore_schema(source, connect, settings.windows, table_names)
	repository = MySQLZScoreRepository(source, connect, table_names, archive_store(source, connect), quality_policy=policy)
	return update_zscores(source, settings, boundary, calendar, repository, clock, initialized_schema=settings.initialize_schema)


def assemble_simplified_zscores(arguments: argparse.Namespace, database_path: Path, database_configuration: DatabaseConfiguration, table_names: TableNames) -> dict[str, object]:
	from stockguidance.database.simplified_runtime import published_sessions, run_simplified_update, source_from_database, validate_simplified_database
	if arguments.initialize_schema:
		raise ValueError("--initialize-schema is retired for the target schema; initialize the versioned schema explicitly before running updates")
	clock = SystemClock()
	connect = MySQLConnectionFactory()
	source, names = source_from_database(database_path, resolve_cli_path(arguments.data_root, "data"), connect, table_names)
	if database_configuration.data_configuration_file is not None and database_configuration.load_data_configuration() != source.data_configuration:
		raise ValueError("The selected data configuration conflicts with the database-owned source configuration")
	calendar = _calendar(arguments, clock, source.calendar_id)
	sessions = published_sessions(calendar, arguments.start_date, arguments.end_date, clock.now(), timedelta(minutes=arguments.publication_delay_minutes))
	update = run_simplified_update(
		source, connect, calendar, sessions, provider=None, operation_kind=OperationKind.DERIVED_REPAIR,
		windows=arguments.windows, clock=clock, table_names=names,
	)
	validation = validate_simplified_database(source, connect, calendar, sessions, table_names=names)
	return {
		"successful": update.successful and validation.complete,
		"complete": validation.complete,
		"exit_code": 0 if update.successful and validation.complete else 2,
		"mode": "simplified",
		"schema": "stockguidance-simplified-runtime-1",
		"requested_scope": [item.session_date.isoformat() for item in sessions],
		"windows": arguments.windows,
		"written_sessions": [item.session_date.isoformat() for item in sessions],
		"provider_calls": update.downloaded_sessions,
		"update": update.payload(),
		"validation": validation.payload(),
	}


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


def main(argv: Sequence[str] | None = None, *, run: Callable[[argparse.Namespace], ZScoreUpdateResult | dict[str, object]] = assemble_and_update_zscores) -> int:
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
	if isinstance(result, dict):
		print(json.dumps(result, sort_keys=True))
		exit_code = result.get("exit_code")
		if type(exit_code) is not int:
			raise ValueError("Simplified z-score result has no integer exit code")
		return exit_code
	print(json.dumps(_payload(result), sort_keys=True))
	return result.exit_code


if __name__ == "__main__":
	raise SystemExit(main())
