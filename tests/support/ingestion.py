from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3

import pytest

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.update_schema import initialize_update_schema
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.history import HistoryProvenance, establish_history_boundary
from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.records import DailyBar
from stockguidance.workflows.update_market_data import UpdateSettings, update_market_data


@dataclass
class FrozenClock:
	value: datetime

	def now(self):
		return self.value


class FakeCursor:
	def __init__(self, cursor, database):
		self.cursor, self.database = cursor, database
		self.synthetic = None

	def execute(self, operation, parameters):
		self.database.statements.append((operation, parameters))
		self.synthetic = None
		if self.database.fail_on and self.database.fail_on in operation.replace("`", ""):
			if self.database.fail_after_matches:
				self.database.fail_after_matches -= 1
			else:
				self.database.fail_on = None
				raise RuntimeError("fixture secret must not be logged")
		if "information_schema.TABLES" in operation:
			self.synthetic = [(name, self.database.engine) for name in parameters]
			return
		sql = operation.replace("%s", "?").replace("INSERT IGNORE", "INSERT OR IGNORE").replace("START TRANSACTION", "BEGIN")
		sql = re.sub(r" FOR UPDATE(?=\s|$)", "", sql)
		sql = sql.replace("INT AUTO_INCREMENT PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
		sql = re.sub(r" ENGINE=InnoDB$", "", sql)
		values = tuple(str(value) if isinstance(value, Decimal) else value.isoformat() if isinstance(value, (date, datetime)) else value for value in parameters)
		self.cursor.execute(sql, values)

	def fetchall(self):
		return self.cursor.fetchall() if self.synthetic is None else self.synthetic

	def executemany(self, operation, parameters):
		for row in parameters:
			self.execute(operation, row)

	def close(self):
		self.cursor.close()


class FakeConnection:
	def __init__(self, database):
		self.database = database
		self.connection = sqlite3.connect(database.path, isolation_level=None)

	def cursor(self):
		return FakeCursor(self.connection.cursor(), self.database)

	def commit(self):
		self.database.commits += 1
		self.connection.commit()

	def rollback(self):
		self.database.rollbacks += 1
		self.connection.rollback()

	def close(self):
		self.database.closed += 1
		self.connection.close()


class FakeDatabase:
	def __init__(self, path):
		self.path = path
		self.configs = []
		self.statements = []
		self.fail_on = None
		self.fail_after_matches = 0
		self.engine = "InnoDB"
		self.commits = self.rollbacks = self.closed = 0

	def __call__(self, database_config):
		self.configs.append(database_config)
		return FakeConnection(self)

	def sql(self, operation, parameters=()):
		with sqlite3.connect(self.path) as connection:
			return connection.execute(operation, parameters).fetchall()


class FakeProvider:
	def __init__(self):
		self.calls = []
		self.responses = {}
		self.errors = set()

	def fetch(self, source, session):
		self.calls.append((source, session))
		if session.session_date in self.errors:
			raise RuntimeError("fixture secret must not be logged")
		return self.responses.get(session.session_date, complete_response())


def complete_response(close="11", ticker="AAA"):
	bar = DailyBar(ticker, Decimal("10.0000"), max(Decimal("15.0000"), Decimal(close)), Decimal("9.0000"), Decimal(close).quantize(Decimal("0.0001")), 100, Decimal("10.0000"), 5)
	return ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1), ("queryCount", 1)))


@pytest.fixture(scope="session")
def calendar():
	return ExchangeCalendar("XNYS", date(2024, 1, 1), date(2025, 12, 31))


@pytest.fixture
def harness(tmp_path, calendar):
	clock = FrozenClock(datetime(2024, 7, 10, 23, tzinfo=timezone.utc))
	identity = SourceIdentity("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, PriceAdjustment.ADJUSTED)
	source = SourceConfig(identity, tmp_path / "custom-root", tmp_path / "custom-source.id", "fixture-credential-reference")
	source.database_config.write_text(json.dumps({"host": "fixture", "database": "fixture", "user": "fixture", "password": "", "driver": "auto"}), encoding="utf-8")
	boundary = establish_history_boundary(identity, calendar, date(2024, 7, 1), clock, provenance=HistoryProvenance.EXPLICIT_BOUNDARY, evidence="Fixture-only boundary")
	settings = UpdateSettings(date(2024, 7, 1), date(2024, 7, 8), DailyPublicationPolicy(timedelta(minutes=15), "Fixture-only publication"), CorrectionPolicy(timedelta(days=30), timedelta(hours=1), timedelta(days=7), "Fixture-only corrections"))
	database = FakeDatabase(tmp_path / "fixture.sqlite")
	initialize_update_schema(source, database)
	repository = MySQLMarketDataRepository(source, database)
	provider = FakeProvider()

	@dataclass
	class Harness:
		source: SourceConfig
		settings: UpdateSettings
		boundary: object
		clock: FrozenClock
		provider: FakeProvider
		database: FakeDatabase
		repository: MySQLMarketDataRepository

		def run(self, settings_override=None, provider_override=None):
			return update_market_data(self.source, settings_override or self.settings, self.boundary, calendar, provider_override or self.provider, self.repository, self.clock)

		def response(self, close="11", ticker="AAA"):
			return complete_response(close, ticker)

	return Harness(source, settings, boundary, clock, provider, database, repository)
