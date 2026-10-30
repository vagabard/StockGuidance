"""Stage 10 orchestration around the shared simulation engine."""

from dataclasses import dataclass, replace

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.prices import ExecutionPriceSource
from stockguidance.simulation.result import SimulationResult
from stockguidance.simulation.score_source import MemoizedScoreSource, ScoreSource
from stockguidance.simulation.configuration import SimulationRequest


@dataclass(frozen=True)
class PairedSimulation:
	"""Results for independent schedules using one frozen score set."""

	parent_policy: str
	alternate_sessions: SimulationResult
	staggered_halves: SimulationResult

	@property
	def results(self) -> tuple[SimulationResult, SimulationResult]:
		return self.alternate_sessions, self.staggered_halves


def simulate_paired(
	request: SimulationRequest,
	boundary: HistoryBoundary,
	calendar: SessionCalendar,
	scores: ScoreSource,
	prices: ExecutionPriceSource,
) -> PairedSimulation:
	"""Run both cash-rest schedules from identical frozen forecasts and quotes."""
	if request.allocation.policy not in (AllocationPolicy.ALTERNATE_SESSIONS, AllocationPolicy.STAGGERED_HALVES):
		raise ValueError("Paired simulation requires a supported cash-rest allocation")
	shared = MemoizedScoreSource(scores)
	shared.prime(request, calendar)
	alternate_request = replace(request, allocation=replace(request.allocation, policy=AllocationPolicy.ALTERNATE_SESSIONS))
	staggered_request = replace(request, allocation=replace(request.allocation, policy=AllocationPolicy.STAGGERED_HALVES))
	return PairedSimulation(
		"shared_frozen_model_and_scores",
		simulate(alternate_request, boundary, calendar, shared, prices),
		simulate(staggered_request, boundary, calendar, shared, prices),
	)
