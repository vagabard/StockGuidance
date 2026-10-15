from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.repositories.sessions import SessionQuery, StoredSession
from stockguidance.market_data.history import HistoryProvenance, establish_history_boundary
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.requests import MarketDataRequest


@dataclass
class FrozenClock:
	value: datetime

	def now(self) -> datetime:
		return self.value


class MemorySessionRepository:
	def __init__(self) -> None:
		self.rows: tuple[StoredSession, ...] = ()
		self.queries: list[SessionQuery] = []

	def read(self, query: SessionQuery) -> tuple[StoredSession, ...]:
		self.queries.append(query)
		dates = {session.session_date for session in query.sessions}
		return tuple(row for row in self.rows if row.session_date in dates)


@pytest.fixture(scope="session")
def calendar() -> ExchangeCalendar:
	return ExchangeCalendar("XNYS", date(2023, 12, 1), date(2026, 12, 31))


@pytest.fixture
def clock() -> FrozenClock:
	return FrozenClock(datetime(2024, 7, 8, 19, 30, tzinfo=timezone.utc))


@pytest.fixture
def source() -> SourceIdentity:
	return SourceIdentity("fixture-provider", "grouped-daily", "us-stocks", "all-us-stocks", PriceAdjustment.ADJUSTED)


@pytest.fixture
def source_config(source: SourceIdentity, tmp_path: Path) -> SourceConfig:
	return SourceConfig(source, tmp_path / "custom-data", tmp_path / "custom-database.id", "key-reference")


@pytest.fixture
def boundary(source, calendar, clock):
	return establish_history_boundary(source, calendar, date(2024, 7, 1), clock, provenance=HistoryProvenance.EXPLICIT_BOUNDARY, evidence="Explicit fixture-only source limit")


@pytest.fixture
def publication() -> DailyPublicationPolicy:
	return DailyPublicationPolicy(timedelta(minutes=15), "Explicit fixture-only publication policy")


@pytest.fixture
def data_request(source, clock) -> MarketDataRequest:
	return MarketDataRequest(source, date(2024, 7, 1), date(2024, 7, 8), clock.now())


@pytest.fixture
def repository() -> MemorySessionRepository:
	return MemorySessionRepository()
