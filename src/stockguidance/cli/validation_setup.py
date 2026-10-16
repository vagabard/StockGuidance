import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.history import HistoryProvenance, UnknownHistoryBoundary, establish_history_boundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.validation.result import ValidationResult
from stockguidance.workflows.validate_database import ValidationSettings, validate_database


def assemble_and_validate(arguments: argparse.Namespace) -> ValidationResult:
	clock = SystemClock()
	identity = SourceIdentity("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, PriceAdjustment(arguments.adjustment))
	source = SourceConfig(identity, Path(arguments.data_root), Path(arguments.dbinfo_file), "unused-by-read-only-validation")
	if (arguments.ticker_ids is None) != (arguments.lookback_sessions is None):
		raise ValueError("Feature validation requires both ticker IDs and lookback")
	features = None if arguments.ticker_ids is None else FeatureSpec(tuple(int(value.strip()) for value in arguments.ticker_ids.split(",")), arguments.lookback_sessions, adjustment=identity.adjustment)
	boundary = JsonHistoryStore(source.data_root / "history").load(identity)
	if boundary is None and arguments.history_start is None:
		raise UnknownHistoryBoundary("Supported history is unknown")
	first = date.fromisoformat(arguments.history_start) if arguments.history_start else boundary.history_start if boundary is not None else None
	if first is None:
		raise UnknownHistoryBoundary("Supported history is unknown")
	cutoff = datetime.fromisoformat(arguments.as_of) if arguments.as_of else clock.now()
	end = date.fromisoformat(arguments.end_date) if arguments.end_date else cutoff.date()
	start = date.fromisoformat(arguments.start_date) if arguments.start_date else first
	calendar = ExchangeCalendar("XNYS", first - timedelta(days=14), max(end, first) + timedelta(days=14))
	if arguments.history_start:
		if not arguments.history_evidence:
			raise ValueError("Explicit history requires evidence")
		candidate = establish_history_boundary(identity, calendar, first, clock, provenance=HistoryProvenance.EXPLICIT_BOUNDARY, evidence=arguments.history_evidence)
		if boundary is not None and (candidate.history_start != boundary.history_start or candidate.calendar_id != boundary.calendar_id):
			raise ValueError("Explicit and persisted history boundaries conflict")
		boundary = candidate if boundary is None else boundary
	if arguments.publication_delay_minutes is not None and not arguments.publication_evidence:
		raise ValueError("Known publication delay requires provenance")
	publication = DailyPublicationPolicy(None if arguments.publication_delay_minutes is None else timedelta(minutes=arguments.publication_delay_minutes), arguments.publication_evidence or "Publication policy unknown")
	settings = ValidationSettings(start, end, publication, cutoff, features)
	return validate_database(source, settings, boundary, calendar, MySQLValidationRepository(source, MySQLConnectionFactory()), clock)
