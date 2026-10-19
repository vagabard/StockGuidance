from datetime import date, datetime, timezone
import re

import pytest

from stage3.conftest import FakeConnection, FakeCursor
from stockguidance.database.repositories.mysql_zscores import MySQLZScoreRepository, ZScoreDependencyChanged
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.zscores import ZScoreInputQuery
from stockguidance.database.table_names import load_table_names
from stockguidance.database.update_schema import initialize_zscore_schema, zscore_schema_statements
from stockguidance.market_data.identity import source_key
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.specification import ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.storage import ZScoreSeriesDefinition


class LockingCursor(FakeCursor):
	def execute(self, operation, parameters):
		return super().execute(re.sub(r" FOR UPDATE(?=\s|$)", "", operation), parameters)


class LockingConnection(FakeConnection):
	def cursor(self):
		return LockingCursor(self.connection.cursor(), self.database)


class LockingDatabase:
	def __init__(self, database):
		self.database = database

	def __call__(self, database_config):
		self.database.configs.append(database_config)
		return LockingConnection(self.database)


def _query(harness, calendar, *days):
	sessions = tuple(calendar.session(date(2024, 7, day)) for day in days)
	return SessionQuery(harness.boundary, sessions, harness.clock.now())


def test_zscore_schema_is_explicit_and_keeps_updater_checks_independent(harness):
	registry = load_table_names()
	statements = zscore_schema_statements(registry, (2, 20))
	assert len(statements) == 6
	assert all("FLOAT NULL" in statement or "status_counts" in statement for statement in statements[2:])
	assert all("PRIMARY KEY (date_id, ticker_id)" in statement for statement in statements[2:])

	initialize_zscore_schema(harness.source, harness.database, (2, 20), registry)
	tables = {row[0] for row in harness.database.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
	assert {"stockguidance_zscore_series", "stockguidance_zscore_sessions", "zscores_price_2", "zscores_price_delta_2", "zscores_price_20", "zscores_price_delta_20"} <= tables


def test_bounded_loader_verifies_manifest_and_reads_only_selected_delta(harness, calendar):
	assert harness.run().successful
	repository = MySQLZScoreRepository(harness.source, harness.database)
	harness.database.statements.clear()
	query = ZScoreInputQuery(_query(harness, calendar, 1, 2), ZScoreFamily.PRICE_DELTA, (1,))
	loaded = repository.load_inputs(query)

	assert loaded.input.family is ZScoreFamily.PRICE_DELTA
	assert len(loaded.dependencies) == 2
	assert loaded.input.tickers[0].observations[0].state.value == "insufficient_history"
	price = repository.load_inputs(ZScoreInputQuery(_query(harness, calendar, 1, 2), ZScoreFamily.PRICE, (1,)))
	assert loaded.input.tickers[0].observations[1].state.value == "available", ([item.state.value for item in loaded.input.tickers[0].observations], [item.state.value for item in price.input.tickers[0].observations])
	assert all("close_ratio_to_prior" in statement for statement, _ in harness.database.statements if "daily_delta_stock_data" in statement and statement.startswith("SELECT"))
	assert all("next_day_change_in_price" not in statement for statement, _ in harness.database.statements if "daily_delta_stock_data" in statement and statement.startswith("SELECT"))


def test_future_label_edit_does_not_change_delta_dependency(harness, calendar):
	assert harness.run().successful
	repository = MySQLZScoreRepository(harness.source, harness.database)
	query = ZScoreInputQuery(_query(harness, calendar, 1, 2), ZScoreFamily.PRICE_DELTA, (1,))
	before = repository.load_inputs(query)
	harness.database.sql("UPDATE daily_delta_stock_data SET next_day_change_in_price = 999 WHERE date_id = 2")
	after = repository.load_inputs(query)
	assert tuple(item.dependency_digest for item in before.dependencies) == tuple(item.dependency_digest for item in after.dependencies)


def test_publish_is_idempotent_and_verified_read_rejects_current_staleness(harness, calendar):
	assert harness.run().successful
	locking_database = LockingDatabase(harness.database)
	repository = MySQLZScoreRepository(harness.source, locking_database)
	query = ZScoreInputQuery(_query(harness, calendar, 1, 2), ZScoreFamily.PRICE, (1,))
	loaded = repository.load_inputs(query)
	specification = ZScoreSpecification(ZScoreFamily.PRICE, (2,))
	result = calculate_zscores(specification, loaded.input)
	definition = ZScoreSeriesDefinition(source_key(harness.source.identity), calendar.calendar_id, ZScoreFamily.PRICE, 2, specification.calculation_version, specification.precision, specification.delta_definition)
	computed_at = datetime(2024, 7, 11, tzinfo=timezone.utc)
	initialize_zscore_schema(harness.source, harness.database, (2,))
	repository.publish(definition, result, loaded.dependencies, computed_at)
	repository.publish(definition, result, loaded.dependencies, computed_at)

	read = repository.read_verified(definition, loaded.dependencies[1])
	assert read.metadata.row_count == 1
	assert read.rows[0].status.value == 5
	harness.database.sql("UPDATE raw_stock_data SET close = 12 WHERE date_id = 2")
	with pytest.raises(ZScoreDependencyChanged):
		repository.read_verified(definition, loaded.dependencies[1])
