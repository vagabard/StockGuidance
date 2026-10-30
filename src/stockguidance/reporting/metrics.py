from dataclasses import asdict, dataclass
from decimal import Decimal

from stockguidance.market_data.identity import content_id, mapping
from stockguidance.portfolio.serialization import decimal_text
from stockguidance.reporting.documents import ReportDocument, ReportKind, object_rows


@dataclass(frozen=True)
class SimulationSummary:
	report: str
	model_policy: str
	allocation_policy: str
	initial_capital: Decimal
	final_equity: Decimal
	total_return: Decimal
	maximum_drawdown: Decimal
	realized_trades: int
	open_positions: int
	successful: bool
	complete: bool
	marks_current: bool

	def payload(self) -> dict[str, object]:
		return {name: str(value) if isinstance(value, Decimal) else value for name, value in asdict(self).items()}


def simulation_summary(document: ReportDocument) -> SimulationSummary:
	if document.kind not in (ReportKind.SIMULATION, ReportKind.SIMULATION_V2):
		raise ValueError("Simulation metrics require a current simulation report")
	fields = document.payload
	initial_state, final_state = mapping(fields["initial_portfolio"]), mapping(fields["final_portfolio"])
	initial = decimal_text(initial_state["initial_capital"])
	values = object_rows(fields["equity"])
	if initial <= 0 or not values:
		raise ValueError("Metrics require positive capital and explicit valuations")
	peak, drawdown = initial, Decimal(0)
	for row in values:
		equity = decimal_text(row["total_equity"])
		if equity < 0:
			raise ValueError("Marked equity cannot be negative")
		peak = max(peak, equity)
		drawdown = max(drawdown, (peak - equity) / peak)
	final = decimal_text(values[-1]["total_equity"])
	positions = sum(len(object_rows(row["positions"])) for row in object_rows(final_state["allocations"]))
	return SimulationSummary(str(document.path), str(fields["model_policy"]), str(mapping(fields["allocation"])["policy"]), initial, final, final / initial - 1, drawdown, len(object_rows(fields["realized_trades"])), positions, fields["successful"] is True, fields["complete"] is True, values[-1]["marks_current"] is True)


def comparison_identity(document: ReportDocument) -> str:
	fields = document.payload
	return content_id({name: fields[name] for name in ("source", "features", "target", "calendar", "start_session", "end_session", "decision_lead_seconds", "execution", "eligibility", "top_k")} | {"initial_capital": str(simulation_summary(document).initial_capital), "allocation_anchor": mapping(fields["allocation"])["anchor_session"]})


def compare_simulations(documents: tuple[ReportDocument, ...]) -> tuple[SimulationSummary, ...]:
	if not documents:
		raise ValueError("Select explicit simulation reports")
	summaries = tuple(simulation_summary(document) for document in documents)
	if len({comparison_identity(document) for document in documents}) != 1:
		raise ValueError("Comparison requires identical source, feature/target, period, capital and execution specifications")
	return summaries
