"""Target-schema daily maintenance entrypoint.

Daily maintenance, direct market-data updates, and z-score updates all call the
same ``run_simplified_update`` coordinator.  This module only assembles the
daily range and performs the independent read-only validator pass.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.paths import resolve_cli_path
from stockguidance.config.source import PriceAdjustment
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.simplified_contract import OperationKind
from stockguidance.database.simplified_runtime import (
	SimplifiedRuntimeError,
	published_sessions,
	run_simplified_update,
	source_from_database,
	validate_simplified_database,
)
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider


def _date_argument(value: object, name: str) -> date | None:
	if value is None:
		return None
	if isinstance(value, date):
		return value
	if isinstance(value, str):
		try:
			return date.fromisoformat(value)
		except ValueError:
			pass
	raise SimplifiedRuntimeError(f"Invalid {name}")


def _years_before(value: date, years: int) -> date:
	try:
		return value.replace(year=value.year - years)
	except ValueError:
		return value.replace(year=value.year - years, day=28)


def run_simplified_daily_maintenance(arguments: Any) -> dict[str, object]:
	if getattr(arguments, "stage", None) is not None:
		raise ValueError("--stage is retired for the target schema; daily maintenance runs the one coordinator and validator path together")
	if getattr(arguments, "scan_quality", False):
		raise ValueError("--scan-quality is retired for the target schema; derived repairs are planned from database state")
	clock = SystemClock()
	database_path = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
	# Keep database configuration loading explicit; no target path infers or
	# creates schema state from the local data directory.
	from stockguidance.database.configuration import load_database_configuration
	database_configuration = load_database_configuration(database_path)
	connect = MySQLConnectionFactory()
	source, names = source_from_database(database_path, resolve_cli_path(arguments.data_root, "data"), connect, database_configuration.load_table_names())
	if arguments.adjustment is not None and PriceAdjustment(arguments.adjustment) is not source.identity.adjustment:
		raise ValueError("Requested adjustment conflicts with the database-owned source configuration")
	now = clock.now()
	default_start = _years_before(now.date(), 2)
	start = _date_argument(getattr(arguments, "start_date", None), "start date") or default_start
	end = _date_argument(getattr(arguments, "end_date", None), "end date") or now.date()
	calendar = ExchangeCalendar(source.calendar_id, min(date(1970, 1, 1), start), max(end, now.date()) + timedelta(days=14))
	targets = published_sessions(calendar, start, end, now, timedelta(minutes=arguments.publication_delay_minutes))
	first = calendar.shift(targets[0], -61)
	raw_sessions = tuple(session for session in calendar.sessions(first.session_date, targets[-1].session_date) if session.closes_at <= now and session.closes_at + timedelta(minutes=arguments.publication_delay_minutes) <= now)
	forced = frozenset(session.session_date for session in raw_sessions if arguments.refresh_source and start <= session.session_date <= end)
	provider = ConfiguredPolygonProvider()
	update = run_simplified_update(
		source, connect, calendar, raw_sessions, provider=provider,
		operation_kind=OperationKind.FORCED_REPLACEMENT if forced else OperationKind.INITIAL_IMPORT,
		forced_sessions=forced, staging_root=source.data_root / ".database-simplification" / "pending", clock=clock, table_names=names,
	)
	validation = validate_simplified_database(source, connect, calendar, targets, table_names=names)
	successful = update.successful and validation.complete
	return {
		"successful": successful,
		"stage": "all",
		"runtime": "simplified",
		"schema": "stockguidance-simplified-runtime-1",
		"start_date": targets[0].session_date.isoformat(),
		"end_date": targets[-1].session_date.isoformat(),
		"downloaded_sessions": update.downloaded_sessions,
		"java_updates": [{"backend": "python-coordinator", "operation_id": update.operation_id, "contract": "simplified-scope-v1"}],
		"partial_imports_removed": 0,
		"python_validation": validation.payload(),
		"forecast_session": targets[-1].session_date.isoformat(),
		"forecast_input_session": targets[-1].session_date.isoformat(),
		"training_outcomes_through": targets[-1].session_date.isoformat(),
		"update": update.payload(),
	}


__all__ = ["run_simplified_daily_maintenance"]
