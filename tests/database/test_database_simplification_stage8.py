from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from contextlib import nullcontext
from pathlib import Path

import pytest

from support.ingestion import FakeDatabase

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.dependency_planner import DependencyPlanner
from stockguidance.database.repositories.simplified import PendingUpdateRepository
from stockguidance.database.simplified_contract import ClearScope, OperationKind, PendingScope, WorkUnit
from stockguidance.database.simplified_runtime import (
	SimplifiedRuntimeError,
	SimplifiedValidationResult,
	run_simplified_update,
	target_schema_state,
	validate_simplified_database,
)
from stockguidance.database.numerical_rebuild import SimplifiedNumericalRebuildRunner
from stockguidance.database.simplified_schema import SchemaVersionError, initialize_simplified_schema
from stockguidance.database.table_names import load_table_names
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
from stockguidance.market_data.records import DailyBar


NOW = datetime(2024, 7, 10, 23, tzinfo=timezone.utc)


@dataclass
class Calendar:
	sessions_by_date: dict[date, TradingSession]

	calendar_id = "XNYS"
	version = "stage8-fixture"
	timezone = timezone.utc
	start = date(2024, 7, 1)
	end = date(2024, 7, 10)

	def session(self, session_date: date) -> TradingSession:
		return self.sessions_by_date[session_date]

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		return tuple(item for item in self.sessions_by_date.values() if start <= item.session_date <= end)

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		ordered = tuple(sorted(self.sessions_by_date.values(), key=lambda item: item.sequence))
		return ordered[ordered.index(session) + offset]


def _calendar() -> Calendar:
	return Calendar({
		day: TradingSession("XNYS", day, index, datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc), datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc))
		for index, day in enumerate((date(2024, 7, 1), date(2024, 7, 2,)))
	})


def _source(tmp_path: Path) -> SourceConfig:
	identity = SourceIdentity("fixture", "fixture-endpoint", "us", "all", PriceAdjustment.UNADJUSTED)
	return SourceConfig(identity, tmp_path / "data", tmp_path / "db.id", "fixture")


def _seed(source: SourceConfig, database: FakeDatabase, calendar: Calendar, *, complete: bool) -> None:
	initialize_simplified_schema(source, database, windows=(2,))
	for session in calendar.sessions_by_date.values():
		database.sql("INSERT INTO transaction_dates (transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision, delta_complete, delta_completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (session.session_date.isoformat(), int(complete), NOW.isoformat() if complete else None, NOW.isoformat() if complete else None, "a" * 64 if complete else None, int(complete), NOW.isoformat() if complete else None))
	if complete:
		database.sql("INSERT INTO ticker_symbol (label, exchange) VALUES (?, ?)", ("AAA", "XNYS"))


def test_fileless_completed_target_history_does_not_call_provider(tmp_path: Path) -> None:
	source, calendar, database = _source(tmp_path), _calendar(), FakeDatabase(tmp_path / "complete.sqlite")
	_seed(source, database, calendar, complete=True)
	class UnexpectedProvider:
		def fetch(self, *_args):
			raise AssertionError("completed target history must not call the provider")

	result = run_simplified_update(source, database, calendar, tuple(calendar.sessions_by_date.values()), provider=UnexpectedProvider(), clock=type("Clock", (), {"now": lambda self: NOW})(), lock_factory=lambda: nullcontext())
	assert result.successful and result.downloaded_sessions == 0
	assert database.sql("SELECT COUNT(*) FROM stockguidance_pending_updates") == [(0,)]


def test_initial_target_import_prepares_provider_payload_and_rebuilds_derived(tmp_path: Path) -> None:
	source, calendar, database = _source(tmp_path), _calendar(), FakeDatabase(tmp_path / "import.sqlite")
	_seed(source, database, calendar, complete=False)
	class Provider:
		calls: list[date] = []

		def fetch(self, _source, session):
			self.calls.append(session.session_date)
			bar = DailyBar("AAA", Decimal("10"), Decimal("11"), Decimal("9"), Decimal(str(10 + session.sequence)), Decimal("100"), Decimal("10"), 1)
			return ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1),), source_timestamps=(("AAA", int(session.closes_at.timestamp() * 1000)),))

	provider = Provider()
	result = run_simplified_update(source, database, calendar, tuple(calendar.sessions_by_date.values()), provider=provider, clock=type("Clock", (), {"now": lambda self: NOW})(), lock_factory=lambda: nullcontext())
	assert result.successful and set(provider.calls) == set(calendar.sessions_by_date)
	assert database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(2,)]
	assert database.sql("SELECT COUNT(*) FROM daily_delta_stock_data") == [(2,)]
	assert database.sql("SELECT COUNT(*) FROM stockguidance_pending_updates") == [(0,)]


def test_forced_target_refresh_calls_provider_even_when_completion_is_present(tmp_path: Path) -> None:
	source, calendar, database = _source(tmp_path), _calendar(), FakeDatabase(tmp_path / "forced.sqlite")
	_seed(source, database, calendar, complete=True)
	class Provider:
		calls: list[date] = []

		def fetch(self, _source, session):
			self.calls.append(session.session_date)
			bar = DailyBar("AAA", Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), Decimal("100"), Decimal("10"), 1)
			return ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1),), source_timestamps=(("AAA", int(session.closes_at.timestamp() * 1000)),))

	provider = Provider()
	sessions = tuple(calendar.sessions_by_date.values())
	result = run_simplified_update(
		source, database, calendar, sessions, provider=provider, operation_kind=OperationKind.FORCED_REPLACEMENT,
		forced_sessions=frozenset(item.session_date for item in sessions), clock=type("Clock", (), {"now": lambda self: NOW})(), lock_factory=lambda: nullcontext(),
	)
	assert result.successful and set(provider.calls) == set(calendar.sessions_by_date)


def test_target_update_recovers_cleared_pending_scope_without_provider_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	source, calendar, database = _source(tmp_path), _calendar(), FakeDatabase(tmp_path / "recovery.sqlite")
	_seed(source, database, calendar, complete=False)
	class Provider:
		calls = 0

		def fetch(self, _source, session):
			self.calls += 1
			bar = DailyBar("AAA", Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), Decimal("100"), Decimal("10"), 1)
			return ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1),), source_timestamps=(("AAA", int(session.closes_at.timestamp() * 1000)),))

	provider = Provider()
	original = SimplifiedNumericalRebuildRunner.rebuild
	monkeypatch.setattr(SimplifiedNumericalRebuildRunner, "rebuild", lambda self, operation, connection: (_ for _ in ()).throw(RuntimeError("interrupted after clear")))
	with pytest.raises(SimplifiedRuntimeError, match="pending scope"):
		run_simplified_update(source, database, calendar, tuple(calendar.sessions_by_date.values()), provider=provider, clock=type("Clock", (), {"now": lambda self: NOW})(), lock_factory=lambda: nullcontext())
	assert database.sql("SELECT COUNT(*) FROM stockguidance_pending_updates") == [(1,)]
	monkeypatch.setattr(SimplifiedNumericalRebuildRunner, "rebuild", original)
	class UnexpectedProvider:
		def fetch(self, *_args):
			raise AssertionError("recovery must consume the persisted payload")

	result = run_simplified_update(source, database, calendar, tuple(calendar.sessions_by_date.values()), provider=UnexpectedProvider(), clock=type("Clock", (), {"now": lambda self: NOW})(), lock_factory=lambda: nullcontext())
	assert result.successful and result.resumed_operation and database.sql("SELECT COUNT(*) FROM stockguidance_pending_updates") == [(0,)]
	assert provider.calls == len(calendar.sessions_by_date)


def test_validator_distinguishes_unfinished_operation_from_observation_states(tmp_path: Path) -> None:
	source, calendar, database = _source(tmp_path), _calendar(), FakeDatabase(tmp_path / "validator.sqlite")
	_seed(source, database, calendar, complete=True)
	sessions = tuple(calendar.sessions_by_date.values())
	date_id = database.sql("SELECT id FROM transaction_dates WHERE transaction_date = ?", (sessions[0].session_date.isoformat(),))[0][0]
	scope = PendingScope(1, OperationKind.DERIVED_REPAIR, data_configuration_key(source.data_configuration), "XNYS", NOW, NOW, (), ClearScope(deltas=(WorkUnit("delta_session", date_id, 1, sessions[0].session_date),)), (WorkUnit("delta_session", date_id, 1, sessions[0].session_date),), ())
	PendingUpdateRepository(source, database).register(scope, NOW)
	result = validate_simplified_database(source, database, calendar, sessions)
	assert isinstance(result, SimplifiedValidationResult)
	assert result.state == "unfinished_operation"
	assert result.findings[0].phase == "delta_session"


def test_validator_reports_bad_and_expected_missing_observations(tmp_path: Path) -> None:
	source, calendar, database = _source(tmp_path), _calendar(), FakeDatabase(tmp_path / "observations.sqlite")
	_seed(source, database, calendar, complete=True)
	sessions = tuple(calendar.sessions_by_date.values())
	ticker_id = database.sql("SELECT id FROM ticker_symbol WHERE label = ?", ("AAA",))[0][0]
	date_ids = [database.sql("SELECT id FROM transaction_dates WHERE transaction_date = ?", (item.session_date.isoformat(),))[0][0] for item in sessions]
	database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, state_id) VALUES (?, ?, ?)", (ticker_id, date_ids[0], 2))
	database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, state_id) VALUES (?, ?, ?)", (ticker_id, date_ids[1], 3))
	result = validate_simplified_database(source, database, calendar, sessions)
	assert result.state == "verified" and not result.complete
	assert {(finding.phase, finding.reason, finding.blocking) for finding in result.findings} == {
		("raw", "bad_observation", True), ("raw", "expected_missing_observation", False),
	}


def test_incompatible_target_schema_never_falls_back_to_legacy_writer(tmp_path: Path) -> None:
	source, _calendar_value, database = _source(tmp_path), _calendar(), FakeDatabase(tmp_path / "mismatch.sqlite")
	initialize_simplified_schema(source, database, windows=(2,))
	database.sql("UPDATE stockguidance_schema_version SET schema_version = 99 WHERE id = 1")
	with pytest.raises(SchemaVersionError, match="incompatible|version"):
		target_schema_state(database, source.database_config, load_table_names())
