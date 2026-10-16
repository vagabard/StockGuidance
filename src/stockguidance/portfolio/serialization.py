from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path

from stockguidance.artifacts.decoding import exact_mapping, integer
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.market_data.identity import text_field
from stockguidance.portfolio.availability import SaleProceedsTiming
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.portfolio.precision import require_decimal
from stockguidance.portfolio.state import AllocationState, PortfolioState, Position, RestingProceeds


def decimal_text(value: object) -> Decimal:
	if not isinstance(value, str):
		raise ValueError("Financial values require explicit decimal strings")
	result = Decimal(value)
	require_decimal(result, "financial value", signed=True)
	return result


def object_array(value: object) -> tuple[object, ...]:
	if not isinstance(value, list):
		raise ValueError("Expected an immutable projection of a JSON array")
	return tuple(value)


def position_payload(position: Position) -> dict[str, object]:
	return {"ticker_id": position.ticker_id, "quantity": str(position.quantity), "entry_session": position.entry_session.session_date.isoformat(), "exit_session": position.exit_session.session_date.isoformat(), "entry_price": str(position.entry_price), "cost_basis": str(position.cost_basis)}


def portfolio_payload(state: PortfolioState) -> dict[str, object]:
	return {
		"schema": "stockguidance-portfolio-1", "as_of": state.as_of.isoformat(), "initial_capital": str(state.initial_capital),
		"policy": state.allocation.policy.value, "anchor_session": state.allocation.anchor_session.session_date.isoformat(),
		"allocations": [{"name": item.name, "available_cash": str(item.available_cash), "realized_pnl": str(item.realized_pnl), "fees_paid": str(item.fees_paid), "positions": [position_payload(position) for position in item.positions], "resting": [{"amount": str(proceeds.amount), "sale_session": proceeds.timing.sale_session.session_date.isoformat(), "eligible_session": proceeds.timing.eligible_session.session_date.isoformat()} for proceeds in item.resting]} for item in state.allocations],
	}


def decode_portfolio(value: object, allocation: AllocationSpec, calendar: SessionCalendar) -> PortfolioState:
	fields = exact_mapping(value, ("schema", "as_of", "initial_capital", "policy", "anchor_session", "allocations"))
	if fields["schema"] != "stockguidance-portfolio-1" or fields["policy"] != allocation.policy.value or fields["anchor_session"] != allocation.anchor_session.session_date.isoformat():
		raise ValueError("Portfolio schema, allocation policy or anchor differs")
	allocations = []
	for value in object_array(fields["allocations"]):
		item = exact_mapping(value, ("name", "available_cash", "realized_pnl", "fees_paid", "positions", "resting"))
		positions = []
		for position_value in object_array(item["positions"]):
			position = exact_mapping(position_value, ("ticker_id", "quantity", "entry_session", "exit_session", "entry_price", "cost_basis"))
			positions.append(Position(integer(position["ticker_id"]), decimal_text(position["quantity"]), calendar.session(date.fromisoformat(text_field(position, "entry_session"))), calendar.session(date.fromisoformat(text_field(position, "exit_session"))), decimal_text(position["entry_price"]), decimal_text(position["cost_basis"])))
		resting = []
		for proceeds_value in object_array(item["resting"]):
			proceeds = exact_mapping(proceeds_value, ("amount", "sale_session", "eligible_session"))
			timing = SaleProceedsTiming(calendar.session(date.fromisoformat(text_field(proceeds, "sale_session"))), calendar.session(date.fromisoformat(text_field(proceeds, "eligible_session"))))
			resting.append(RestingProceeds(decimal_text(proceeds["amount"]), timing))
		allocations.append(AllocationState(text_field(item, "name"), decimal_text(item["available_cash"]), tuple(resting), tuple(positions), decimal_text(item["realized_pnl"]), decimal_text(item["fees_paid"])))
	return PortfolioState(allocation, datetime.fromisoformat(text_field(fields, "as_of")), decimal_text(fields["initial_capital"]), tuple(allocations))


def load_portfolio(path: Path, allocation: AllocationSpec, calendar: SessionCalendar) -> PortfolioState:
	return decode_portfolio(json.loads(path.read_text(encoding="utf-8")), allocation, calendar)
