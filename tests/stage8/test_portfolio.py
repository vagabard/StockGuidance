from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from stockguidance.portfolio.ledger import release_proceeds
from stockguidance.portfolio.planning import CashOrder, PlanState, plan_orders
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.schedule import buy_decision_index, scheduled_allocation
from stockguidance.portfolio.serialization import decode_portfolio, portfolio_payload
from stockguidance.portfolio.state import initial_portfolio
from stockguidance.recommendations.result import RankedTicker


@pytest.mark.parametrize("policy,names,indices", [(AllocationPolicy.ALTERNATE_SESSIONS, ("A", None, "A", None, "A"), (0, 1, 2)), (AllocationPolicy.STAGGERED_HALVES, ("A", "B", "A", "B", "A"), (0, 1, 2, 3, 4))])
def test_anchor_uses_exchange_sessions_through_holiday_and_weekend(simulation, policy, names, indices):
	allocation = replace(simulation.request.allocation, policy=policy)
	sessions = simulation.calendar.sessions(date(2024, 7, 5), date(2024, 7, 11))
	assert tuple(item.session_date.day for item in sessions) == (5, 8, 9, 10, 11)
	assert tuple(scheduled_allocation(allocation, item) for item in sessions) == names
	assert tuple(buy_decision_index(allocation, item) for item in sessions if scheduled_allocation(allocation, item)) == indices
	with pytest.raises(ValueError, match="precedes"):
		scheduled_allocation(allocation, simulation.calendar.session(date(2024, 7, 3)))
	if policy is AllocationPolicy.ALTERNATE_SESSIONS:
		with pytest.raises(ValueError, match="no scheduled"):
			buy_decision_index(allocation, sessions[1])


def test_plan_is_immutable_uses_fixed_budgets_and_does_not_book_fills(simulation):
	timing = simulation.samples.decision(5)
	state = initial_portfolio(simulation.request.allocation, Decimal("1000.01"), timing.decision_cutoff)
	ranking = tuple(RankedTicker(ticker_id, float(4 - index)) for index, ticker_id in enumerate((17, 3, 90)))
	plan = plan_orders(state, state.allocation, timing, ranking)
	assert plan.state is PlanState.READY
	assert tuple(item.maximum_debit for item in plan.orders) == (Decimal("333.33"),) * 3
	assert state.allocations[0].available_cash == Decimal("1000.01")
	assert not state.allocations[0].positions and not state.allocations[0].resting
	assert portfolio_payload(state)["as_of"] == timing.decision_cutoff.isoformat()
	with pytest.raises(FrozenInstanceError):
		plan.state = PlanState.NO_CASH


def test_same_close_proceeds_are_resting_and_release_only_next_open(simulation):
	result = simulation.run(replace(simulation.request, end=date(2024, 7, 8)))
	state = result.final
	assert state.allocations[0].available_cash == 0
	assert state.allocations[0].resting[0].amount == Decimal("2000.00")
	sale = simulation.calendar.session(date(2024, 7, 8))
	assert not state.allocations[0].resting[0].timing.is_available_for(sale)
	with pytest.raises(ValueError):
		release_proceeds(state, sale, sale.opens_at)
	next_session = simulation.calendar.shift(sale, 1)
	with pytest.raises(ValueError):
		release_proceeds(state, next_session, next_session.opens_at - timedelta(microseconds=1))
	released = release_proceeds(state, next_session, next_session.opens_at)
	assert released.allocations[0].available_cash == Decimal("2000.00") and not released.allocations[0].resting
	assert release_proceeds(released, next_session, next_session.opens_at) == released


def test_balance_conservation_rejects_double_counting_and_cross_allocation_transfers(simulation):
	request = replace(simulation.request, allocation=replace(simulation.request.allocation, policy=AllocationPolicy.STAGGERED_HALVES))
	state = simulation.run(request).valuations[2].portfolio
	assert tuple(item.realized_pnl for item in state.allocations) == (Decimal("500.00"), Decimal("-250.00"))
	with pytest.raises(ValueError, match="conserve"):
		replace(state, allocations=(replace(state.allocations[0], available_cash=Decimal("1.00")), state.allocations[1]))
	state = initial_portfolio(request.allocation, Decimal("1000.00"), simulation.samples.decision(5).decision_cutoff)
	with pytest.raises(ValueError, match="conserve"):
		replace(state, allocations=(replace(state.allocations[0], available_cash=Decimal("510.00")), replace(state.allocations[1], available_cash=Decimal("490.00"))))


@pytest.mark.parametrize("capital", [Decimal("0.00"), Decimal("-1.00"), Decimal("1.001"), Decimal("NaN"), 1000.0])
def test_invalid_initial_capital_is_rejected(simulation, capital):
	with pytest.raises(ValueError):
		initial_portfolio(simulation.request.allocation, capital, simulation.samples.decision(5).decision_cutoff)


def test_halves_split_once_into_equal_cents_and_never_round_a_transfer(simulation):
	allocation = replace(simulation.request.allocation, policy=AllocationPolicy.STAGGERED_HALVES)
	with pytest.raises(ValueError, match="equal"):
		initial_portfolio(allocation, Decimal("1000.01"), simulation.samples.decision(5).decision_cutoff)
	state = initial_portfolio(allocation, Decimal("1000.02"), simulation.samples.decision(5).decision_cutoff)
	assert tuple(item.available_cash for item in state.allocations) == (Decimal("500.01"),) * 2


def test_portfolio_snapshot_round_trip_preserves_independent_positions_and_proceeds(simulation):
	request = replace(simulation.request, allocation=replace(simulation.request.allocation, policy=AllocationPolicy.STAGGERED_HALVES))
	state = simulation.run(request).valuations[1].portfolio
	payload = portfolio_payload(state)
	assert decode_portfolio(payload, request.allocation, simulation.calendar) == state
	payload["api_key"] = "fixture-secret"
	with pytest.raises(ValueError):
		decode_portfolio(payload, request.allocation, simulation.calendar)


@pytest.mark.parametrize("fee", [Decimal("-0.01"), Decimal("0.001"), Decimal("Infinity"), 0.0])
def test_fees_require_nonnegative_decimal_cents(fee):
	with pytest.raises(ValueError):
		ExecutionPolicy(fee)


def test_fractional_quantity_rounds_down_without_overspending():
	quantity, debit = ExecutionPolicy(Decimal("1.00")).size(Decimal("10.00"), Decimal("7.00"))
	assert quantity == Decimal("1.28571428") and debit == Decimal("10.00")
	assert ExecutionPolicy(Decimal("1.00")).size(Decimal("1.00"), Decimal("7.00")) == (0, 0)
	with pytest.raises(ValueError):
		CashOrder("A", 17, Decimal("0.00"))


def test_future_or_different_portfolio_state_cannot_plan(simulation):
	timing = simulation.samples.decision(5)
	state = initial_portfolio(simulation.request.allocation, Decimal("1000.00"), timing.decision_cutoff + timedelta(seconds=1))
	with pytest.raises(ValueError, match="timestamp"):
		plan_orders(state, state.allocation, timing, (RankedTicker(17, 1.0),))


def test_portfolio_rejects_forged_exchange_session_timestamps(simulation):
	state = simulation.run(replace(simulation.request, end=date(2024, 7, 8))).final
	state.validate_calendar(simulation.calendar)
	proceeds = state.allocations[0].resting[0]
	eligible = replace(proceeds.timing.eligible_session, opens_at=proceeds.timing.eligible_session.opens_at - timedelta(hours=1))
	forged = replace(proceeds, timing=replace(proceeds.timing, eligible_session=eligible))
	state = replace(state, allocations=(replace(state.allocations[0], resting=(forged,)),))
	with pytest.raises(ValueError, match="authoritative calendar"):
		state.validate_calendar(simulation.calendar)
