import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.paths import resolve_cli_path
from stockguidance.config.source import DEFAULT_CALENDAR_ID, DataConfiguration, PriceAdjustment, SourceConfig
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.database.update_schema import assert_source as default_assert_source
from stockguidance.database.update_schema import assert_transactional_tables as default_assert_transactional_tables
from stockguidance.database.update_schema import assert_zscore_tables as default_assert_zscore_tables
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.publication import DailyPublicationPolicy


LOOKBACK_YEARS = 2


@dataclass(frozen=True)
class MaintenanceContext:
	arguments: argparse.Namespace
	clock: Any
	now: datetime
	database_path: Path
	source: SourceConfig
	names: TableNames
	calendar: SessionCalendar
	raw_sessions: tuple[TradingSession, ...]
	targets: tuple[TradingSession, ...]
	publication: DailyPublicationPolicy
	correction: CorrectionPolicy


@dataclass
class MaintenanceSession:
	context: MaintenanceContext
	connection: Any
	cursor: Any
	imports: dict[date, tuple[str, str, int]]
	partial_imports_removed: int

	def assert_lock(self) -> None:
		self.cursor.execute("SELECT IS_USED_LOCK('stockguidance_daily_maintenance') = CONNECTION_ID()", ())
		if self.cursor.fetchall() != [(1,)]:
			raise RuntimeError("The maintenance database lock was lost")
		self.connection.rollback()


def maintenance_range(as_of: date) -> tuple[date, date]:
	try:
		start = as_of.replace(year=as_of.year - LOOKBACK_YEARS)
	except ValueError:
		start = as_of.replace(year=as_of.year - LOOKBACK_YEARS, day=28)
	return start, as_of


def session_workload(calendar: SessionCalendar, start: date, end: date, cutoff: datetime, publication: DailyPublicationPolicy) -> tuple[tuple[TradingSession, ...], tuple[TradingSession, ...]]:
	published = tuple(session for session in calendar.sessions(start, end)
		if (available := publication.expected_at(session)) is not None and available <= cutoff and session.closes_at <= cutoff)
	if not published:
		raise ValueError("The maintenance range contains no published exchange sessions")
	first = calendar.shift(published[0], -61)
	last = published[-1]
	for distance in (1, 2, 3):
		following = calendar.shift(published[-1], distance)
		available = publication.expected_at(following)
		if available is not None and available <= cutoff and following.closes_at <= cutoff:
			last = following
	return tuple(calendar.sessions(first.session_date, last.session_date)), published


def raw_import_state(cursor: Any, names: TableNames, sessions: tuple[TradingSession, ...]) -> dict[date, tuple[str, str, int]]:
	if not sessions:
		return {}
	identifiers = tuple(session.session_date.isoformat() for session in sessions)
	placeholders = ",".join("%s" for _ in identifiers)
	cursor.execute(f"SELECT s.transaction_date, i.raw_id, i.available_at, COUNT(p.ticker_id) FROM {quote_identifier(names.sessions)} s LEFT JOIN {quote_identifier(names.imports)} i ON i.session_date = s.transaction_date LEFT JOIN {quote_identifier(names.prices)} p ON p.date_id = s.id WHERE s.transaction_date IN ({placeholders}) GROUP BY s.transaction_date, i.raw_id, i.available_at", identifiers)
	return {date.fromisoformat(str(row[0])): (str(row[1]), str(row[2]), int(row[3])) for row in cursor.fetchall() if row[1] is not None}


@contextmanager
def open_maintenance(
	arguments: argparse.Namespace,
	*,
	run_java: bool,
	clock: Any,
	connection_factory: Callable[[Path], Any],
	assert_source: Callable[..., Any] = default_assert_source,
	assert_transactional_tables: Callable[..., Any] = default_assert_transactional_tables,
	assert_zscore_tables: Callable[..., Any] = default_assert_zscore_tables,
	prepare_partial_imports: Callable[[Any, Any, TableNames], int] | None = None,
) -> Iterator[MaintenanceSession]:
	now = clock.now()
	default_start, default_end = maintenance_range(now.date())
	start, end = arguments.start_date or default_start, arguments.end_date or default_end
	if start > end or start < date(2006, 11, 16):
		raise ValueError("Maintenance bounds must be ordered and start on or after 2006-11-16")
	publication = DailyPublicationPolicy(timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence)
	correction = CorrectionPolicy(timedelta(days=arguments.recent_window_days), timedelta(hours=arguments.recent_refresh_hours),
		timedelta(days=arguments.archive_refresh_days), arguments.correction_evidence)
	database_path = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
	arguments.dbinfo_file = str(database_path)
	configuration = load_database_configuration(database_path)
	names = configuration.load_table_names()
	data_configuration = configuration.load_data_configuration() if configuration.data_configuration_file is not None else None
	connection = connection_factory(database_path)
	cursor = connection.cursor()
	locked = False
	try:
		cursor.execute("SELECT GET_LOCK('stockguidance_daily_maintenance', 0)", ())
		locked = cursor.fetchall() == [(1,)]
		if not locked:
			raise RuntimeError("Another maintenance process is running on this database server")
		if data_configuration is None:
			cursor.execute(f"SELECT source_family, endpoint, market, universe, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment FROM {quote_identifier(names.source)} WHERE id = 1", ())
			rows = cursor.fetchall()
			if len(rows) != 1:
				raise ValueError("Database data configuration must be initialized before maintenance")
			values = rows[0]
			data_configuration = DataConfiguration(str(values[0]), str(values[1]), str(values[2]), str(values[3]), bool(values[4]), bool(values[5]), None if values[6] is None else str(values[6]))
		if arguments.adjustment is not None and PriceAdjustment(arguments.adjustment) is not data_configuration.adjustment:
			raise ValueError("Requested adjustment conflicts with the database data configuration")
		arguments.adjustment = data_configuration.adjustment.value
		identity = data_configuration.to_source_identity()
		source = SourceConfig(identity, resolve_cli_path(arguments.data_root, "data"), database_path,
			"Configured Massive/Polygon credential", _configuration=data_configuration)
		assert_source(cursor, source, names, write=True)
		assert_transactional_tables(cursor, names)
		assert_zscore_tables(cursor, names, (20, 60))
		cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", ("stockguidance_migration_completion",))
		if cursor.fetchall() != [("InnoDB",)]:
			raise ValueError("The migrated production completion table must exist and use InnoDB")
		calendar = ExchangeCalendar(
			DEFAULT_CALENDAR_ID if data_configuration is None else data_configuration.calendar_id,
			min(start, date(2005, 1, 1)), max(end, now.date()) + timedelta(days=14))
		raw_sessions, targets = session_workload(calendar, start, end, now, publication)
		partial_imports_removed = prepare_partial_imports(cursor, connection, names) if run_java and prepare_partial_imports is not None else 0
		imports = raw_import_state(cursor, names, raw_sessions)
		context = MaintenanceContext(arguments, clock, now, database_path, source, names, calendar, raw_sessions, targets, publication, correction)
		yield MaintenanceSession(context, connection, cursor, imports, partial_imports_removed)
	finally:
		try:
			if locked:
				cursor.execute("SELECT RELEASE_LOCK('stockguidance_daily_maintenance')", ())
				cursor.fetchall()
		finally:
			cursor.close()
			connection.close()
