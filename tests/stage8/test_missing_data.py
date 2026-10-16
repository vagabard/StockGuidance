from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from stockguidance.portfolio.planning import PlanState
from stockguidance.simulation.events import EventKind
from stockguidance.simulation.score_source import DecisionUnavailable, FixedModelScores


def test_missing_entry_keeps_its_fixed_budget_without_redistributing_to_survivors(simulation):
	simulation.prices.missing.add((simulation.calendar.session(date(2024, 7, 5)), 17))
	request = replace(simulation.request, top_k=2, end=date(2024, 7, 5))
	result = simulation.run(request)
	assert result.successful and not result.complete
	allocation = result.final.allocations[0]
	assert allocation.available_cash == 500
	assert len(allocation.positions) == 1 and allocation.positions[0].ticker_id == 3
	assert allocation.positions[0].quantity == 10 and allocation.positions[0].cost_basis == 500
	assert result.valuations[0].total_equity == 1000
	assert len([item for item in result.events if item.kind is EventKind.UNFILLED_BUY]) == 1
	assert result.benchmarks[0].selected_return is None


def test_missing_exit_retains_losing_position_and_stale_mark_then_retries(simulation):
	simulation.prices.missing.add((simulation.calendar.session(date(2024, 7, 8)), 17))
	simulation.prices.prices[simulation.calendar.session(date(2024, 7, 9)), 17] = Decimal(50)
	result = simulation.run(replace(simulation.request, end=date(2024, 7, 9)))
	assert result.successful and not result.complete
	assert not result.valuations[1].marks_current
	assert result.valuations[1].positions[0].price_session.session_date == date(2024, 7, 5)
	assert result.valuations[1].positions[0].position.quantity == 10
	assert result.plans[-1].state is PlanState.POSITIONS_OPEN
	assert not result.final.allocations[0].positions
	assert result.final.allocations[0].resting[0].amount == 500
	assert result.trades[0].realized_pnl == -500 and not result.trades[0].exited_on_time
	assert result.trades[0].position.exit_session.session_date == date(2024, 7, 8)
	assert result.trades[0].exit_session_date == "2024-07-09"
	assert result.trades[0].close_return is None and result.trades[0].actual_price_return == -0.5


def test_late_exit_proceeds_cannot_fund_profitable_same_close_reentry(simulation):
	simulation.prices.missing.add((simulation.calendar.session(date(2024, 7, 8)), 17))
	simulation.prices.prices[simulation.calendar.session(date(2024, 7, 9)), 17] = Decimal(200)
	simulation.prices.prices[simulation.calendar.session(date(2024, 7, 10)), 17] = Decimal(1000)
	result = simulation.run(replace(simulation.request, end=date(2024, 7, 10)))
	assert result.valuations[-1].total_equity == 2000
	assert result.valuations[-1].total_equity != 10000
	assert tuple(item.session.session_date.day for item in result.events if item.kind is EventKind.BUY) == (5,)
	assert result.plans[-1].state is PlanState.POSITIONS_OPEN and not result.plans[-1].orders


def test_unpriced_end_position_is_retained_with_explicit_uncertain_equity(simulation):
	for day in (8, 9, 10, 11):
		simulation.prices.missing.add((simulation.calendar.session(date(2024, 7, day)), 17))
	result = simulation.run()
	assert result.successful and not result.complete
	assert len(result.final.allocations[0].positions) == 1
	assert result.valuations[-1].positions[0].value == 1000
	assert not result.valuations[-1].marks_current
	assert not result.trades and not result.final.allocations[0].resting
	assert len([item for item in result.events if item.kind is EventKind.UNFILLED_EXIT]) == 4


@pytest.mark.parametrize("cause", ["prediction", "features", "liquidity"])
def test_missing_decision_does_not_shift_alternate_parity(simulation, monkeypatch, cause):
	source = FixedModelScores(simulation.reference, simulation.signals)
	if cause == "prediction":
		original = source.forecast

		def forecast(request, timing, buy_index):
			if timing.entry_session.session_date.day == 5:
				raise DecisionUnavailable("Fixture prediction unavailable")
			return original(request, timing, buy_index)

		monkeypatch.setattr(source, "forecast", forecast)
	elif cause == "features":
		simulation.samples.repository.missing.add((simulation.calendar.session(date(2024, 7, 3)), 17))
	else:
		simulation.liquidity.missing.add((simulation.calendar.session(date(2024, 7, 3)), 17))
	result = simulation.run(source=source)
	assert result.successful and not result.complete
	assert tuple(item.session.session_date.day for item in result.events if item.kind is EventKind.BUY) == (9, 11)
	assert result.plans[0].orders[0].maximum_debit == 1000
	assert any(item.kind is EventKind.SKIP and item.session.session_date.day == 5 for item in result.events)


def test_unexpected_prediction_failure_is_visible_and_cannot_claim_success(simulation, monkeypatch):
	source = FixedModelScores(simulation.reference, simulation.signals)

	def fail(*arguments):
		raise RuntimeError("fixture-password-never-log")

	monkeypatch.setattr(source, "forecast", fail)
	result = simulation.run(source=source)
	assert not result.successful and not result.complete and not result.trades
	assert len([item for item in result.events if item.kind is EventKind.FAILURE]) == 3
	assert "password" not in repr(result.events)


def test_execution_failure_preserves_the_last_ledger_and_stops_without_dropping_positions(simulation, monkeypatch):
	original = simulation.prices.read

	def read(session, ticker_ids):
		if session.session_date.day == 8:
			raise RuntimeError("fixture-secret-never-log")
		return original(session, ticker_ids)

	monkeypatch.setattr(simulation.prices, "read", read)
	result = simulation.run()
	assert not result.successful
	assert len(result.final.allocations[0].positions) == 1
	assert result.final.allocations[0].positions[0].quantity == 10
	assert len(result.valuations) == 1 and not result.trades
	assert "secret" not in repr(result.events)
