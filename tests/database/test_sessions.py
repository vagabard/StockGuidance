from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from stockguidance.calendar.date_bounds import DateBoundResolver, DateBoundType
from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.configuration import DatabaseConfiguration
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


def test_connector_does_not_substitute_default_database_configuration(source_config, monkeypatch):
	observed_paths, parameters = [], []
	connection = FakeConnection(FakeCursor(()))

	def load_configuration(path):
		observed_paths.append(path)
		return DatabaseConfiguration("fixture", "fixture", "fixture", "")

	def connect(**values):
		parameters.append(values)
		return connection

	monkeypatch.setattr("stockguidance.database.connections.load_database_configuration", load_configuration)
	monkeypatch.setattr("stockguidance.database.connections.import_module", lambda name: SimpleNamespace(connect=connect))
	assert MySQLConnectionFactory()(source_config.database_config) is connection
	assert observed_paths == [source_config.database_config]
	assert parameters[0]["autocommit"] is False


def test_missing_explicit_configuration_does_not_search_parent_directories(tmp_path, monkeypatch):
	monkeypatch.setattr("stockguidance.database.connections.import_module", lambda name: pytest.fail("Missing config loaded a driver"))
	with pytest.raises(ValueError, match="explicit database JSON"):
		MySQLConnectionFactory()(tmp_path / "missing" / "dbinfo.id")
