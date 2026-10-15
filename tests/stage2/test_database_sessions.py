from datetime import date, datetime
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace

import pytest

from stockguidance.calendar.date_bounds import DateBoundResolver, DateBoundType
from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.legacy_connections import LegacyMySQLConnectionFactory
from stockguidance.database.repositories.mysql_sessions import MySQLSessionRepository
from stockguidance.database.repositories.sessions import SessionQuery, StoredSession
from stockguidance.validation.coverage import CoverageState


class FakeCursor:
	def __init__(self, rows):
		self.rows = rows
		self.calls = []
		self.closed = False
		self.failure = None

	def execute(self, operation, parameters):
		self.calls.append((operation, parameters))
		if self.failure is not None:
			raise self.failure

	def fetchall(self):
		return self.rows

	def close(self):
		self.closed = True


class FakeConnection:
	def __init__(self, cursor):
		self.reader = cursor
		self.closed = False

	def cursor(self):
		return self.reader

	def close(self):
		self.closed = True


@pytest.mark.parametrize("value", [date(2024, 7, 1), datetime(2024, 7, 1), "2024-07-01", b"2024-07-01", "2024-07-01T00:00:00"])
def test_driver_specific_dates_are_normalized_at_repository_boundary(value):
	assert normalize_session_date(value) == date(2024, 7, 1)


@pytest.mark.parametrize("value", [None, 20240701, "not-a-date", b"\xff"])
def test_invalid_driver_dates_are_rejected(value):
	with pytest.raises(ValueError):
		normalize_session_date(value)


@pytest.mark.parametrize("value", [7, "7", b"7"])
def test_driver_specific_ids_are_exact_integers(value):
	assert normalize_date_id(value) == 7


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1.5", None])
def test_invalid_driver_ids_are_not_silently_truncated(value):
	with pytest.raises(ValueError):
		normalize_date_id(value)


def test_repository_uses_configured_source_and_calendar_dates_not_numeric_ids(source_config, boundary, calendar, clock):
	cursor = FakeCursor(((901, b"2024-07-01", 3), (11, datetime(2024, 7, 2), 1), (450, date(2024, 7, 3), 0)))
	connection = FakeConnection(cursor)
	config_paths = []

	def connect(database_config):
		config_paths.append(database_config)
		return connection

	query = SessionQuery(boundary, calendar.sessions(date(2024, 7, 1), date(2024, 7, 3)), clock.now())
	rows = MySQLSessionRepository(source_config, connect).read(query)
	assert [row.date_id for row in rows] == [901, 11, 450]
	assert [row.state for row in rows] == [CoverageState.UNVERIFIED, CoverageState.UNVERIFIED, CoverageState.MISSING]
	assert config_paths == [source_config.database_config]
	assert cursor.calls[0][1] == tuple(session.session_date for session in query.sessions)
	assert cursor.calls[0][0].startswith("SELECT ")
	assert "MAX(" not in cursor.calls[0][0]
	assert cursor.closed and connection.closed


@pytest.mark.parametrize("rows", [
	((1, "2024-07-01"),), ((1, "2024-07-01", -1),), ((1, "2024-07-08", 1),),
	((1, "2024-07-01", 1), (1, "2024-07-02", 1)),
	((1, "2024-07-01", 1), (2, "2024-07-01", 1)),
])
def test_repository_rejects_invalid_out_of_scope_or_duplicate_rows(source_config, boundary, calendar, clock, rows):
	cursor = FakeCursor(rows)
	connection = FakeConnection(cursor)
	query = SessionQuery(boundary, calendar.sessions(date(2024, 7, 1), date(2024, 7, 3)), clock.now())
	with pytest.raises(ValueError):
		MySQLSessionRepository(source_config, lambda database_config: connection).read(query)
	assert cursor.closed and connection.closed


def test_failed_database_read_propagates_and_closes_resources(source_config, boundary, calendar, clock):
	cursor = FakeCursor(())
	cursor.failure = RuntimeError("Fixture read failure")
	connection = FakeConnection(cursor)
	query = SessionQuery(boundary, (calendar.session(boundary.history_start),), clock.now())
	with pytest.raises(RuntimeError, match="Fixture read failure"):
		MySQLSessionRepository(source_config, lambda database_config: connection).read(query)
	assert cursor.closed and connection.closed


def test_query_itself_rejects_pre_history_and_future_sessions(boundary, calendar, clock):
	with pytest.raises(ValueError, match="history boundary"):
		SessionQuery(boundary, (calendar.session(date(2024, 6, 28)),), clock.now())
	with pytest.raises(ValueError, match="information cutoff"):
		SessionQuery(boundary, (calendar.session(date(2024, 7, 8)),), clock.now())


def test_stored_complete_requires_actual_publication_evidence():
	with pytest.raises(ValueError, match="availability evidence"):
		StoredSession(1, date(2024, 7, 1), CoverageState.COMPLETE, "No actual publication evidence")


def test_bound_resolution_and_offsets_follow_stored_chronology_not_id_arithmetic():
	rows = tuple(StoredSession(date_id, date(2024, 7, day), CoverageState.UNVERIFIED, "Fixture") for day, date_id in ((1, 901), (2, 11), (3, 450)))
	resolver = DateBoundResolver(rows)
	assert resolver.resolve_bound(None, DateBoundType.START) == 901
	assert resolver.resolve_bound(None, DateBoundType.END) == 450
	assert resolver.resolve_bound("+1", DateBoundType.START) == 11
	assert resolver.resolve_bound("-1", DateBoundType.END) == 450
	assert resolver.resolve_bound("-2", DateBoundType.END) == 11
	assert resolver.resolve_bound("-0", DateBoundType.END) == 450
	assert resolver.resolve_bound("901", DateBoundType.START) == 901
	assert resolver.offset_date_id(901, 1) == 11
	assert resolver.offset_date_id(450, -1) == 11
	assert resolver.offset_date_id(11, 0) == 11
	for bound in ("+3", "-4", "9999", ""):
		with pytest.raises(ValueError):
			resolver.resolve_bound(bound, DateBoundType.START)
	with pytest.raises(ValueError):
		resolver.offset_date_id(450, 1)
	with pytest.raises(ValueError):
		DateBoundResolver(tuple(reversed(rows)))


def test_legacy_connector_does_not_substitute_default_database_configuration(source_config, monkeypatch):
	observed_paths = []
	connection = FakeConnection(FakeCursor(()))

	def get_connection(*, dbinfo_file):
		observed_paths.append(dbinfo_file)
		return connection

	monkeypatch.setattr("stockguidance.database.legacy_connections.import_module", lambda name: SimpleNamespace(get_connection=get_connection))
	assert LegacyMySQLConnectionFactory()(source_config.database_config) is connection
	assert observed_paths == [str(source_config.database_config)]


def test_legacy_resolver_forwards_explicit_configuration_to_every_query(source_config, monkeypatch):
	calls = []

	def load_date_id_range(*, dbinfo_file, feature_vector_day_size_buffer):
		calls.append((dbinfo_file, feature_vector_day_size_buffer))
		return 901, 450

	def get_offset_date_id(*, dbinfo_file, base_value, offset):
		calls.append((dbinfo_file, base_value, offset))
		return 11

	queries = ModuleType("database.database_date_id_queries")
	queries.load_date_id_range = load_date_id_range
	queries.get_offset_date_id = get_offset_date_id
	database = ModuleType("database")
	database.database_date_id_queries = queries
	monkeypatch.setitem(sys.modules, "database", database)
	monkeypatch.setitem(sys.modules, "database.database_date_id_queries", queries)
	legacy = runpy.run_path(str(Path(__file__).resolve().parents[2] / "trainer" / "util" / "date_bounds.py"))
	resolver_type = legacy["DateBoundResolver"]
	bound_type = legacy["DateBoundType"]
	config_path = str(source_config.database_config)
	resolver = resolver_type(2, dbinfo_file=config_path)
	assert resolver.resolve_bound("+1", bound_type.START) == 11
	assert resolver.resolve_bound("-1", bound_type.END) == 11
	assert calls == [(config_path, 2), (config_path, 901, 1), (config_path, 450, -1)]
	assert resolver.resolve_bound("+0", bound_type.START) == 901
	assert resolver.resolve_bound("-0", bound_type.END) == 450
	assert len(calls) == 3
	with pytest.raises(TypeError):
		resolver_type()
	with pytest.raises(ValueError, match="explicit database configuration"):
		resolver_type(dbinfo_file="")
