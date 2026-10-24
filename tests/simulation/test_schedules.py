from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.simulation.events import EventKind


def test_alternate_schedule_matches_hand_calculated_equity_and_cash_categories(simulation):
	result = simulation.run()
	assert result.successful and result.complete
	assert tuple(item.total_equity for item in result.valuations) == (1000, 2000, 2000, 1000, 1000)
	assert tuple(item.amount for item in result.events if item.kind is EventKind.ORDER) == (1000, 2000, 1000)
	assert tuple(item.session.session_date.day for item in result.events if item.kind is EventKind.BUY) == (5, 9, 11)
	assert tuple(item.exit_session_date for item in result.trades) == ("2024-07-08", "2024-07-10")
	assert tuple(item.net_return for item in result.trades) == (Decimal(1), Decimal("-0.5"))
	assert result.valuations[1].allocations[0].available_cash == 0
	assert result.valuations[1].allocations[0].resting_proceeds == 2000
	assert result.valuations[1].allocations[0].positions_value == 0
	assert result.final.allocations[0].positions[0].exit_session.session_date == date(2024, 7, 12)
	assert not result.benchmarks[-1].complete and result.benchmarks[-1].selected_return is None


def test_staggered_halves_keep_idle_cash_and_diverge_without_rebalancing(simulation):
	request = replace(simulation.request, allocation=replace(simulation.request.allocation, policy=AllocationPolicy.STAGGERED_HALVES))
	result = simulation.run(request)
	assert result.successful and result.complete
	assert tuple(item.total_equity for item in result.valuations) == (1000, 1500, 1250, 750, 1000)
	first = result.valuations[0].allocations
	assert first[0].positions_value == 500 and first[1].available_cash == 500
	assert tuple(item.equity for item in result.valuations[2].allocations) == (1000, 250)
	assert tuple((item.allocation, item.orders[0].maximum_debit) for item in result.plans) == (("A", 500), ("B", 500), ("A", 1000), ("B", 250), ("A", 500))
	assert tuple(item.equity for item in result.valuations[-1].allocations) == (500, 500)
	assert result.valuations[-1].allocations[0].positions_value == 500
	assert result.valuations[-1].allocations[1].resting_proceeds == 500


def test_a_rising_market_cannot_profit_from_illegal_same_close_reuse(simulation):
	for day, price in ((5, 100), (8, 200), (9, 400), (10, 800)):
		simulation.prices.prices[simulation.calendar.session(date(2024, 7, day)), 17] = Decimal(price)
	result = simulation.run(replace(simulation.request, end=date(2024, 7, 10)))
	assert result.valuations[-1].total_equity == 4000
	assert result.valuations[-1].total_equity != 8000
	assert tuple(item.session.session_date.day for item in result.events if item.kind is EventKind.BUY) == (5, 9)
	assert result.valuations[1].allocations[0].resting_proceeds == 2000


def test_both_schedules_share_exact_model_features_scores_and_target(simulation):
	first = simulation.run()
	second = simulation.run(replace(simulation.request, allocation=replace(simulation.request.allocation, policy=AllocationPolicy.STAGGERED_HALVES)))
	by_date = {item.sample.timing.entry_session: item for item in second.forecasts}
	for forecast in first.forecasts:
		other = by_date[forecast.sample.timing.entry_session]
		assert forecast.artifact_id == other.artifact_id == simulation.reference.artifact_id
		assert forecast.sample == other.sample
		assert forecast.prediction == other.prediction
	assert first.request.target == second.request.target == simulation.samples.target


def test_explicit_fees_are_charged_on_each_actual_fill(simulation):
	from stockguidance.portfolio.precision import ExecutionPolicy
	request = replace(simulation.request, end=date(2024, 7, 8), execution=ExecutionPolicy(Decimal("1.00")))
	result = simulation.run(request)
	assert result.valuations[0].total_equity == 999
	assert result.trades[0].net_proceeds == Decimal("1997.00")
	assert result.trades[0].realized_pnl == Decimal("997.00")
	assert result.final.allocations[0].fees_paid == 2
	assert result.final.allocations[0].resting[0].amount == 1997


def test_every_event_is_chronological_and_reports_each_cash_transition(simulation):
	result = simulation.run()
	assert tuple(item.at for item in result.events) == tuple(sorted(item.at for item in result.events))
	for event in result.events:
		if event.kind in (EventKind.AVAILABILITY, EventKind.BUY, EventKind.SALE, EventKind.MARK):
			assert event.portfolio is not None
		if event.kind is EventKind.ORDER:
			assert event.at < event.session.closes_at
	for value in result.valuations:
		assert value.total_equity == sum(item.available_cash + item.resting_proceeds + item.positions_value for item in value.allocations)
	assert len(simulation.prices.calls) == 5


def test_starting_after_anchor_preserves_original_schedule(simulation):
	request = replace(simulation.request, start=date(2024, 7, 8))
	result = simulation.run(request)
	assert tuple(item.session.session_date.day for item in result.events if item.kind is EventKind.BUY) == (9, 11)
	assert result.valuations[0].allocations[0].available_cash == 1000


@pytest.mark.parametrize("change", [{"start": date(2024, 7, 6)}, {"end": date(2024, 7, 13)}])
def test_non_session_evaluation_bounds_are_rejected(simulation, change):
	with pytest.raises(ValueError):
		simulation.run(replace(simulation.request, **change))
