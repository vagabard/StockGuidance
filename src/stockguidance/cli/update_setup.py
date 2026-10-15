import argparse
from datetime import date, timedelta
from importlib import import_module
import os
from pathlib import Path

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.update_schema import initialize_update_schema
from stockguidance.database.write_connections import LegacyWriteConnectionFactory
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.history import HistoryProvenance, UnknownHistoryBoundary, establish_history_boundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.providers.http import PolygonHttpTransport
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE, PolygonGroupedProvider
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.workflows.update_market_data import UpdateSettings, update_market_data
from stockguidance.workflows.update_result import UpdateResult


def assemble_and_update(arguments: argparse.Namespace) -> UpdateResult:
	clock = SystemClock()
	identity = SourceIdentity("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, PriceAdjustment(arguments.adjustment))
	source = SourceConfig(identity, Path(arguments.data_root), Path(arguments.dbinfo_file), "polygon_fetch.DEFAULT_API_KEY or POLYGON_API_KEY")
	history_store = JsonHistoryStore(source.data_root / "history")
	boundary = history_store.load(identity)
	if boundary is None and arguments.history_start is None:
		raise UnknownHistoryBoundary("No supported-history boundary; supply --history-start and --history-evidence")
	first = date.fromisoformat(arguments.history_start) if arguments.history_start else boundary.history_start if boundary is not None else None
	if first is None:
		raise UnknownHistoryBoundary("Supported history is unknown")
	end = date.fromisoformat(arguments.end_date) if arguments.end_date else clock.now().date()
	start = date.fromisoformat(arguments.start_date) if arguments.start_date else first
	calendar = ExchangeCalendar("XNYS", first - timedelta(days=14), max(end, first) + timedelta(days=14))
	if arguments.history_start:
		if not arguments.history_evidence:
			raise ValueError("An explicit history start requires --history-evidence")
		candidate = establish_history_boundary(identity, calendar, first, clock, provenance=HistoryProvenance.EXPLICIT_BOUNDARY, evidence=arguments.history_evidence)
		if boundary is not None and (boundary.history_start != first or boundary.calendar_id != candidate.calendar_id):
			raise ValueError("Persisted supported-history boundary differs; inspect its evidence before changing it")
		boundary = candidate if boundary is None else boundary
	if boundary is None:
		raise UnknownHistoryBoundary("Supported history is unknown")
	boundary.validate_for(identity, calendar)
	settings = UpdateSettings(start, end, DailyPublicationPolicy(timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence), CorrectionPolicy(timedelta(days=arguments.recent_window_days), timedelta(hours=arguments.recent_refresh_hours), timedelta(days=arguments.archive_refresh_days), arguments.correction_evidence))
	api_key = getattr(arguments, "api_key", None) or os.environ.get("POLYGON_API_KEY")
	if not api_key:
		value: object = import_module("polygon_fetch").DEFAULT_API_KEY
		if not isinstance(value, str) or not value:
			raise ValueError("Provider credential is unavailable")
		api_key = value
	provider = PolygonGroupedProvider(PolygonHttpTransport(api_key, arguments.max_retries))
	connect = LegacyWriteConnectionFactory()
	if arguments.initialize_schema:
		initialize_update_schema(source, connect)
	history_store.save(boundary)
	return update_market_data(source, settings, boundary, calendar, provider, MySQLMarketDataRepository(source, connect), clock)
