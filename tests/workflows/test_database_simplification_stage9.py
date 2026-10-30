from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from support.ingestion import FakeDatabase

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.simplified_schema import initialize_simplified_schema
from stockguidance.database.repositories.simplified import PendingUpdateRepository
from stockguidance.database.simplified_contract import ClearScope, OperationKind, PendingScope, WorkUnit
from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
from stockguidance.market_data.records import DailyBar
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.database.repositories.closes import CloseQuery
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.workflows.simplified_readiness import (
	SimplifiedCloseRepository,
	SimplifiedCoverageError,
	SimplifiedReadinessWorkflow,
	required_session_scope,
)
from stockguidance.features.specification import FeatureSpec
from stockguidance.targets.specification import TargetKind, TargetSpec, TargetSpecV2


NOW = datetime(2024, 7, 11, 23, tzinfo=timezone.utc)


@dataclass
class Calendar:
	sessions_by_date: dict[date, TradingSession]

	calendar_id = "XNYS"
	version = "stage9-fixture"
	timezone = timezone.utc

	def session(self, session_date: date) -> TradingSession:
		return self.sessions_by_date[session_date]

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		return tuple(item for item in sorted(self.sessions_by_date.values(), key=lambda value: value.sequence) if start <= item.session_date <= end)

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		ordered = tuple(sorted(self.sessions_by_date.values(), key=lambda value: value.sequence))
		return ordered[ordered.index(session) + offset]


def _calendar() -> Calendar:
	dates = (date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3), date(2024, 7, 5), date(2024, 7, 8), date(2024, 7, 9))
	return Calendar({
		day: TradingSession("XNYS", day, index, datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc), datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc))
		for index, day in enumerate(dates)
	})


def _source(tmp_path: Path) -> SourceConfig:
	return SourceConfig(SourceIdentity("fixture", "fixture-endpoint", "us", "all", PriceAdjustment.UNADJUSTED), tmp_path / "data", tmp_path / "database.id", "fixture")


class Provider:
	def __init__(self, close: str = "11") -> None:
		self.close = close
		self.calls: list[date] = []

	def fetch(self, _source: SourceConfig, session: TradingSession) -> ProviderResponse:
		self.calls.append(session.session_date)
		bar = DailyBar("AAA", Decimal("10"), Decimal("15"), Decimal("9"), Decimal(self.close), Decimal("100"), Decimal("10"), 500)
		return ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1),), source_timestamps=(("AAA", int(session.closes_at.timestamp() * 1000)),))


def _decision(calendar: Calendar) -> DecisionTiming:
	return DecisionTiming(calendar.session(date(2024, 7, 3)), calendar.session(date(2024, 7, 5)), calendar.session(date(2024, 7, 8)), datetime(2024, 7, 5, 19, tzinfo=timezone.utc))


def test_required_scope_expands_exchange_lookback_labels_and_normalization() -> None:
	calendar = _calendar()
	scope = required_session_scope(calendar, (_decision(calendar),), FeatureSpec((1,), 2), TargetSpec(), include_labels=True, normalization_warmup=1)
	assert tuple(item.session_date for item in scope.feature_sessions) == (date(2024, 7, 2), date(2024, 7, 3))
	assert tuple(item.session_date for item in scope.label_sessions) == (date(2024, 7, 5), date(2024, 7, 8))
	assert tuple(item.session_date for item in scope.sessions) == (date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3), date(2024, 7, 5), date(2024, 7, 8)
	)


def test_required_scope_carries_explicit_cross_sectional_target_dependencies() -> None:
	calendar = _calendar()
	target = TargetSpecV2(kind=TargetKind.RETURN_ZSCORE_CROSS_SECTIONAL)
	scope = required_session_scope(calendar, (_decision(calendar),), FeatureSpec((1,), 2), target, include_labels=True)
	assert tuple(item.session_date for item in scope.target_dependency_sessions) == (date(2024, 7, 8),)


def test_readiness_reports_insufficient_exchange_history(tmp_path: Path) -> None:
	source, calendar = _source(tmp_path), _calendar()
	workflow = SimplifiedReadinessWorkflow(source, FakeDatabase(tmp_path / "insufficient.sqlite"), calendar, DailyPublicationPolicy(timedelta(minutes=15), "fixture"), None, type("Clock", (), {"now": lambda self: NOW})())
	with pytest.raises(SimplifiedCoverageError, match="Insufficient exchange-session history"):
		workflow.ensure((_decision(calendar),), FeatureSpec((1,), 10), TargetSpec(), label_cutoff=NOW)


def test_target_readiness_fills_missing_rows_then_reuses_fileless_completion(tmp_path: Path) -> None:
	source, calendar = _source(tmp_path), _calendar()
	database = FakeDatabase(tmp_path / "target.sqlite")
	initialize_simplified_schema(source, database, windows=(2,))
	provider = Provider()
	readiness = SimplifiedReadinessWorkflow(source, database, calendar, DailyPublicationPolicy(timedelta(minutes=15), "fixture"), provider, type("Clock", (), {"now": lambda self: NOW})(), lock_factory=lambda: nullcontext())
	first = readiness.ensure((_decision(calendar),), FeatureSpec((1,), 2), TargetSpec(), label_cutoff=NOW)
	assert first.successful and provider.calls
	provider.calls.clear()
	second = readiness.ensure((_decision(calendar),), FeatureSpec((1,), 2), TargetSpec(), label_cutoff=NOW)
	assert second.successful and not provider.calls and first.data_revision == second.data_revision

	query = CloseQuery(SessionQuery(second.boundary, (calendar.session(date(2024, 7, 3)),), NOW), (1,))
	data = SimplifiedCloseRepository(source, database).read(query)
	assert len(data.observations) == 1 and data.observations[0].close == pytest.approx(11.0)


def test_correction_changes_revision_and_pending_scope_blocks_read(tmp_path: Path) -> None:
	source, calendar = _source(tmp_path), _calendar()
	database = FakeDatabase(tmp_path / "revision.sqlite")
	initialize_simplified_schema(source, database, windows=(2,))
	publication = DailyPublicationPolicy(timedelta(minutes=15), "fixture")
	clock = type("Clock", (), {"now": lambda self: NOW})()
	first_provider = Provider("11")
	workflow = SimplifiedReadinessWorkflow(source, database, calendar, publication, first_provider, clock, lock_factory=lambda: nullcontext())
	first = workflow.ensure((_decision(calendar),), FeatureSpec((1,), 2), TargetSpec(), label_cutoff=NOW)
	second_provider = Provider("12")
	from stockguidance.database.simplified_contract import OperationKind

	from stockguidance.database.simplified_runtime import run_simplified_update
	forced = run_simplified_update(source, database, calendar, tuple(calendar.sessions(date(2024, 7, 1), date(2024, 7, 8))), provider=second_provider, operation_kind=OperationKind.FORCED_REPLACEMENT, forced_sessions=frozenset({date(2024, 7, 3)}), clock=clock, lock_factory=lambda: nullcontext())
	assert forced.successful
	second = workflow.ensure((_decision(calendar),), FeatureSpec((1,), 2), TargetSpec(), label_cutoff=NOW)
	assert second.data_revision != first.data_revision

	query = CloseQuery(SessionQuery(second.boundary, (calendar.session(date(2024, 7, 3)),), NOW), (1,))
	data = SimplifiedCloseRepository(source, database).read(query)
	assert data.observations[0].close == pytest.approx(12.0)


def test_read_path_blocks_an_overlapping_pending_operation(tmp_path: Path) -> None:
	source, calendar = _source(tmp_path), _calendar()
	database = FakeDatabase(tmp_path / "pending.sqlite")
	initialize_simplified_schema(source, database, windows=(2,))
	clock = type("Clock", (), {"now": lambda self: NOW})()
	workflow = SimplifiedReadinessWorkflow(source, database, calendar, DailyPublicationPolicy(timedelta(minutes=15), "fixture"), Provider(), clock, lock_factory=lambda: nullcontext())
	result = workflow.ensure((_decision(calendar),), FeatureSpec((1,), 2), TargetSpec(), label_cutoff=NOW)
	date_id = database.sql("SELECT id FROM transaction_dates WHERE transaction_date = ?", (date(2024, 7, 3).isoformat(),))[0][0]
	unit = WorkUnit("delta_session", date_id, 1, date(2024, 7, 3))
	scope = PendingScope(1, OperationKind.DERIVED_REPAIR, data_configuration_key(source.data_configuration), "XNYS", NOW, NOW, (), ClearScope(deltas=(unit,)), (unit,), ())
	PendingUpdateRepository(source, database).register(scope, NOW)
	query = CloseQuery(SessionQuery(result.boundary, (calendar.session(date(2024, 7, 3)),), NOW), (1,))
	with pytest.raises(SimplifiedCoverageError, match="unfinished|operation"):
		SimplifiedCloseRepository(source, database).read(query)

