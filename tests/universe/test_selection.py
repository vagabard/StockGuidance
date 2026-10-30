from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stockguidance.calendar.sessions import TradingSession
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.calendar.ranges import AbsoluteDate, DatePlanSpec, DateRangeResolver, DependencySpec, ExplicitRange
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.market_data.intervals import ObservationInterval
from stockguidance.universe.selection import (
	AverageVolumeFilter,
	EmptyUniverseError,
	ExclusionReason,
	RawObservationState,
	SourceSessionEvidence,
	TickerIdentity,
	UniverseFilters,
	UniverseObservation,
	UniverseReadRequest,
	UniverseSelectionRequest,
	UniverseSourceData,
	UniverseCoverageError,
	VolatilityFilter,
	assess_universe,
	persist_universe_evidence,
	persist_universe_failure_evidence,
	select_universe,
)
from stockguidance.validation.coverage import CoverageState


def sessions(count: int = 4) -> tuple[TradingSession, ...]:
	base = datetime(2026, 1, 5, 14, tzinfo=timezone.utc)
	return tuple(TradingSession("FIXTURE", date(2026, 1, 5) + timedelta(days=index), index, base + timedelta(days=index), base + timedelta(days=index, hours=6)) for index in range(count))


def source(sessions_value: tuple[TradingSession, ...], *, missing: set[tuple[int, int]] = set(), volume: tuple[Decimal, ...] | None = None, closes: tuple[Decimal, ...] | None = None, transactions: tuple[int, ...] | None = None) -> UniverseSourceData:
	identities = (TickerIdentity(7, "ZZZ"), TickerIdentity(2, "AAA"))
	coverage = tuple(SourceSessionEvidence(item, CoverageState.COMPLETE, "fixture completion", "fixture-revision") for item in sessions_value)
	observations = []
	for ticker_index, identity in enumerate(identities):
		for index, item in enumerate(sessions_value):
			if (identity.ticker_id, index) in missing:
				observations.append(UniverseObservation(identity.ticker_id, item, RawObservationState.MISSING))
				continue
			observations.append(UniverseObservation(
				identity.ticker_id,
				item,
				RawObservationState.AVAILABLE,
				(closes[index] if closes is not None else Decimal(10) + Decimal(ticker_index) + Decimal(index)),
				None if volume is None else volume[index],
				None if transactions is None else transactions[index],
				"fixture-row",
			))
	return UniverseSourceData(identities, coverage, tuple(observations), "fixture-source-revision")


def request(sessions_value: tuple[TradingSession, ...], filters: UniverseFilters = UniverseFilters(), ordered: tuple[int, ...] | None = None) -> UniverseSelectionRequest:
	return UniverseSelectionRequest(
		membership_sessions=sessions_value,
		dependency_sessions=(),
		filter_sessions=sessions_value if filters.enabled else (),
		filters=filters,
		ordered_ticker_ids=ordered,
		filter_cutoff_session=sessions_value[-1] if filters.enabled else None,
	)


def test_interior_gap_is_excluded_even_when_endpoints_exist() -> None:
	scope = sessions(3)
	result = assess_universe(request(scope), source(scope, missing={(7, 1)}))
	assert result.ordered_ticker_ids == (2,)
	exclusion = result.exclusions[0]
	assert exclusion.ticker_id == 7
	assert exclusion.reason is ExclusionReason.MISSING_OBSERVATION
	assert "2026-01-06" in exclusion.details[0]


def test_unknown_whole_session_blocks_selection_instead_of_reducing_universe() -> None:
	scope = sessions(2)
	data = source(scope)
	data = UniverseSourceData(data.tickers, (data.coverage[0], SourceSessionEvidence(scope[1], CoverageState.UNVERIFIED, "provider coverage unknown")), data.observations)
	with pytest.raises(UniverseCoverageError, match="unknown"):
		assess_universe(request(scope), data)


def test_verified_missing_whole_session_is_explicitly_explained() -> None:
	scope = sessions(2)
	data = source(scope)
	data = UniverseSourceData(data.tickers, (data.coverage[0], SourceSessionEvidence(scope[1], CoverageState.MISSING, "verified source absence")), data.observations)
	result = assess_universe(request(scope), data)
	assert result.selected == ()
	assert all(ExclusionReason.MISSING_SESSION_COVERAGE in item.reasons for item in result.exclusions)
	with pytest.raises(EmptyUniverseError) as error:
		select_universe(request(scope), data)
	assert error.value.result.to_mapping()["status"] == "empty"


@pytest.mark.parametrize("value,expected", [(Decimal("100"), (2, 7)), (Decimal("99"), ())])
def test_average_volume_uses_arithmetic_mean_and_inclusive_bounds(value: Decimal, expected: tuple[int, ...]) -> None:
	scope = sessions(2)
	filters = UniverseFilters(average_volume=AverageVolumeFilter(2, lower=value, upper=value))
	result = assess_universe(request(scope, filters), source(scope, volume=(Decimal("50"), Decimal("150"))))
	assert result.ordered_ticker_ids == expected
	assert [item.value for item in result.measurements] == [Decimal("100"), Decimal("100")]


def test_volatility_uses_population_stddev_and_predecessor_close() -> None:
	scope = sessions(3)
	filters = UniverseFilters(volatility=VolatilityFilter(2, lower=Decimal("0.09")))
	data = source(scope, closes=(Decimal("10"), Decimal("11"), Decimal("10")))
	result = assess_universe(request(scope, filters), data)
	assert result.ordered_ticker_ids == (2, 7)
	assert result.measurements[0].value is not None
	assert abs(result.measurements[0].value - Decimal("0.09545454545")) < Decimal("1e-10")


def test_disabled_filters_do_not_request_optional_dependencies() -> None:
	scope = sessions(2)
	read = request(scope).read_request()
	assert read.read_volume is False
	assert read.read_transactions is False
	assert read.sessions == scope


def test_explicit_subset_preserves_order_but_stable_mode_uses_ids() -> None:
	scope = sessions(2)
	data = source(scope)
	assert assess_universe(request(scope), data).ordered_ticker_ids == (2, 7)
	assert assess_universe(request(scope, ordered=(7, 2)), data).ordered_ticker_ids == (7, 2)


def test_run_evidence_uses_the_stage_one_section_without_a_second_store() -> None:
	class Service:
		def __init__(self) -> None:
			self.args = None

		def update_sections(self, run_id, **kwargs):
			self.args = (run_id, kwargs)
			return kwargs

	result = assess_universe(request(sessions(2)), source(sessions(2)))
	service = Service()
	persist_universe_evidence(service, "training-fixture", result)
	assert service.args[0] == "training-fixture"
	assert service.args[1]["sections"]["universe"]["ordered_ticker_ids"] == [2, 7]
	assert service.args[1]["sections"]["results"]["exclusions"] == []


def test_blocked_selection_can_persist_failure_evidence_before_run_failure() -> None:
	class Service:
		def __init__(self) -> None:
			self.sections = None

		def update_sections(self, _run_id, **kwargs):
			self.sections = kwargs["sections"]

	selection_request = request(sessions(2))
	data = source(sessions(2))
	blocked = UniverseSourceData(data.tickers, (data.coverage[0], SourceSessionEvidence(sessions(2)[1], CoverageState.INCOMPLETE, "interrupted import")), data.observations)
	with pytest.raises(UniverseCoverageError) as error:
		assess_universe(selection_request, blocked)
	service = Service()
	persist_universe_failure_evidence(service, "blocked-fixture", selection_request, error.value)
	assert service.sections["universe"]["status"] == "blocked"
	assert service.sections["universe"]["failure"]["code"] == "unknown_or_incomplete_coverage"


def test_dependency_plan_scope_uses_training_dependencies_and_not_evaluation_sessions() -> None:
	calendar = ExchangeCalendar("XNYS", date(2025, 12, 1), date(2026, 3, 31))
	source_identity = SourceIdentity("fixture", "endpoint", "market", "universe", PriceAdjustment.ADJUSTED, ObservationInterval.DAILY)
	verified = tuple(item.session_date for item in calendar.sessions(calendar.start, calendar.end))
	from stockguidance.calendar.ranges import SourceCoverageSnapshot

	coverage = SourceCoverageSnapshot.from_verified_dates(source_identity, calendar.calendar_id, "fixture-coverage", verified)
	plan = DateRangeResolver(calendar, coverage, source_identity, type("Clock", (), {"now": lambda self: datetime(2026, 3, 31, 23, tzinfo=timezone.utc)})()).resolve_plan(
		DatePlanSpec(
			ExplicitRange(AbsoluteDate(date(2026, 1, 20)), AbsoluteDate(date(2026, 1, 22))),
			ExplicitRange(AbsoluteDate(date(2026, 2, 2)), AbsoluteDate(date(2026, 2, 3))),
			DependencySpec(model_lookback_sessions=2, training_end_gap_sessions=1, simulation_horizon_sessions=2),
		)
	)
	selection_request = UniverseSelectionRequest.from_dependency_plan(
		plan,
		calendar,
		filters=UniverseFilters(average_volume=AverageVolumeFilter(3, lower=Decimal("1"))),
	)
	assert all(item.sequence < plan.evaluation.entry_sessions[0].sequence for item in selection_request.required_sessions)
	assert len(selection_request.filter_sessions) >= 3
