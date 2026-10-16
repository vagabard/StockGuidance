from dataclasses import dataclass
from datetime import datetime

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.portfolio.planning import OrderPlan
from stockguidance.portfolio.state import PortfolioState
from stockguidance.portfolio.valuation import PortfolioValue
from stockguidance.simulation.benchmarks import BenchmarkResult
from stockguidance.simulation.configuration import ScorePolicy, SimulationRequest
from stockguidance.simulation.events import EventKind, SimulationEvent
from stockguidance.simulation.execution import RealizedTrade
from stockguidance.simulation.score_source import DecisionForecast
from stockguidance.simulation.prices import ExecutionFrame
from stockguidance.training.result import TrainingResult


@dataclass(frozen=True)
class SimulationResult:
	request: SimulationRequest
	score_policy: ScorePolicy
	calendar_version: str
	initial: PortfolioState
	final: PortfolioState
	events: tuple[SimulationEvent, ...]
	plans: tuple[OrderPlan, ...]
	forecasts: tuple[DecisionForecast, ...]
	valuations: tuple[PortfolioValue, ...]
	trades: tuple[RealizedTrade, ...]
	benchmarks: tuple[BenchmarkResult, ...]
	training_runs: tuple[tuple[DecisionTiming, TrainingResult], ...]
	execution_frames: tuple[ExecutionFrame, ...]
	execution_audit_cutoff: datetime

	@property
	def successful(self) -> bool:
		return not any(item.kind is EventKind.FAILURE for item in self.events)

	@property
	def complete(self) -> bool:
		return self.successful and not any(item.kind in (EventKind.SKIP, EventKind.UNFILLED_BUY, EventKind.UNFILLED_EXIT) for item in self.events) and all(item.marks_current for item in self.valuations) and all(item.complete or item.entry.timing.exit_session.session_date > self.request.end for item in self.benchmarks)
