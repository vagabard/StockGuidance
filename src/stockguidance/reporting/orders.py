from stockguidance.portfolio.planning import OrderPlan
from stockguidance.portfolio.serialization import portfolio_payload


def order_plan_payload(plan: OrderPlan) -> dict[str, object]:
	return {"state": plan.state.value, "allocation": plan.allocation, "decision_cutoff": plan.timing.decision_cutoff.isoformat(), "entry_session": plan.timing.entry_session.session_date.isoformat(), "exit_session": plan.timing.exit_session.session_date.isoformat(), "cash_rest": plan.portfolio.allocation.cash_rest.value, "fee_per_fill": str(plan.execution.fee_per_fill), "currency_quantum": "0.01", "share_quantum": "0.00000001", "sizing": "equal_fixed_maximum_debit_fractional_at_close", "unfilled_policy": "retain_cash_without_redistribution", "projected_available_state": portfolio_payload(plan.portfolio), "orders": [{"allocation": item.allocation, "ticker_id": item.ticker_id, "maximum_debit": str(item.maximum_debit)} for item in plan.orders], "fills_recorded": False}
