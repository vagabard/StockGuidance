from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import torch

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.artifacts.exporting import export_artifact
from stockguidance.artifacts.loading import load_artifact
from stockguidance.market_data.closes import CloseIssue, PriceState
from stockguidance.models.specification import ModelSpec
from stockguidance.portfolio.marks import ClosingMark
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.schedule import scheduled_allocation
from stockguidance.recommendations.eligibility import EligibilityPolicy, LiquidityObservation
from stockguidance.reporting.simulation import simulation_payload
from stockguidance.reporting.training import training_payload
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.events import EventKind
from stockguidance.simulation.prices import ExecutionFrame
from stockguidance.simulation.score_source import FixedModelScores, HistoricalSignals
from stockguidance.training.configuration import DeviceChoice, TrainingConfig
from stockguidance.training.jobs import load_job
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history
from stockguidance.simulation.jobs import load_configuration


EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "stage5"


class FixtureLiquidity:
	def __init__(self, repository):
		self.repository = repository

	def read(self, query):
		return tuple(LiquidityObservation(item.ticker_id, 500, item.timing, item.revision) for item in self.repository.read(query).observations)


class FixtureExecution:
	def __init__(self, source, calendar):
		self.source = source
		self.calendar = calendar
		self.audit_cutoff = calendar.session(date(2024, 7, 11)).closes_at + timedelta(minutes=16)
		self.missing = set()
		self.prices = {(calendar.session(date(2024, 7, day)), 17): Decimal(str(price)) for day, price in ((5, 100), (8, 200), (9, 100), (10, 50), (11, 100))}
		self.prices.update({(calendar.session(date(2024, 7, day)), 3): Decimal("50") for day in (5, 8, 9, 10, 11)})

	def read(self, session, ticker_ids):
		quotes, issues = [], []
		for ticker_id in ticker_ids:
			key = session, ticker_id
			if key in self.missing or key not in self.prices:
				issues.append(CloseIssue(ticker_id, session, PriceState.EXPECTED_ABSENCE, "Stage 5 fixture price is missing"))
			else:
				quotes.append(ClosingMark(ticker_id, session, self.prices[key], f"execution-{session.session_date}-{ticker_id}"))
		return ExecutionFrame(self.source, session, ticker_ids, tuple(quotes), tuple(issues))


def test_stage5_json_examples_are_complete_and_parser_valid():
	training = load_job(EXAMPLES / "previous-close-training-subset.json")
	fixed = load_configuration(EXAMPLES / "fixed-later-simulation.json")
	chronological = load_configuration(EXAMPLES / "chronological-retraining-simulation.json")

	assert training.features.signal_mode.value == "previous_close"
	assert training.availability_policy.value == "strict"
	assert training.start == date(2024, 7, 2) and training.end == date(2024, 7, 3)
	assert fixed.start > training.end and fixed.retraining is None
	assert fixed.features.ticker_ids == training.features.ticker_ids
	assert fixed.allocation_policy is AllocationPolicy.STAGGERED_HALVES
	assert fixed.execution.fee_per_fill == Decimal("0.50")
	assert chronological.retraining is not None
	assert chronological.retraining.start < chronological.start
	assert chronological.retraining.every_buy_decisions == 2
	assert chronological.availability_policy.value == "strict"


def test_stage5_offline_fixture_trains_exports_reloads_and_simulates_later_range(samples, tmp_path):
	features = replace(samples.specification, lookback_sessions=1)
	configuration = TrainingConfig(model=ModelSpec(hidden_widths=()), epochs=1, batch_size=1, learning_rate=0.01, device=DeviceChoice.CPU, seed=42)
	cutoff = samples.calendar.session(date(2024, 7, 3)).closes_at + timedelta(minutes=16)
	training = train_from_history(HistoryTrainingRequest((samples.decision(2),), cutoff, features, samples.target, configuration, tmp_path / "trained.sgmodel"), samples.boundary, samples.calendar, samples.publication, samples.repository)
	reported = training_payload(training)["artifact"]
	reference = ArtifactReference(Path(reported["path"]), reported["artifact_id"])
	loaded = load_artifact(reference)
	with torch.no_grad():
		for parameter in loaded.model.parameters():
			parameter.zero_()
		linear = tuple(module for module in loaded.model.modules() if isinstance(module, torch.nn.Linear))[-1]
		linear.bias.copy_(torch.tensor([1.0, -1.0]))
	fixed = export_artifact(tmp_path / "fixed.sgmodel", loaded.model, loaded.metadata)
	reloaded = load_artifact(fixed)

	assert fixed.artifact_id == reloaded.artifact_id
	assert fixed.artifact_id != sha256(fixed.path.read_bytes()).hexdigest()
	assert reloaded.metadata.semantics.features.ticker_ids == (17, 3)
	assert reloaded.metadata.training.first_entry == date(2024, 7, 2)
	assert reloaded.metadata.training.last_exit == date(2024, 7, 3)

	liquidity = FixtureLiquidity(samples.repository)
	request = SimulationRequest(samples.boundary.source, features, samples.target, AllocationSpec(AllocationPolicy.STAGGERED_HALVES, samples.calendar.session(date(2024, 7, 5))), date(2024, 7, 5), date(2024, 7, 11), Decimal("1000.00"), eligibility=EligibilityPolicy(200), top_k=1, execution=ExecutionPolicy(Decimal("0.50")))
	signals = HistoricalSignals(samples.boundary, samples.calendar, samples.publication, samples.repository, liquidity)
	prices = FixtureExecution(request.source, samples.calendar)
	result = simulate(request, samples.boundary, samples.calendar, FixedModelScores(fixed, signals), prices)
	payload = simulation_payload(result)

	assert result.successful and result.complete
	assert payload["evaluation"] == "fixed_pre_period_model"
	assert payload["features"]["ticker_ids"] == (17, 3)
	assert payload["execution"]["fee_per_fill"] == "0.50"
	assert len(result.training_runs) == 0
	assert tuple(item.name for item in result.final.allocations) == ("A", "B")
	assert all(item.fees_paid > 0 for item in result.final.allocations)
	assert any(item.positions for item in result.final.allocations)
	assert all(event.allocation is not None and event.ticker_id == 17 for event in result.events if event.kind is EventKind.BUY)
	assert all(scheduled_allocation(request.allocation, event.session) == event.allocation for event in result.events if event.kind is EventKind.BUY)
	assert all(samples.calendar.session(event.session.session_date) == event.session for event in result.events if event.kind in (EventKind.BUY, EventKind.SALE))
	assert all(allocation.available_cash + sum((item.amount for item in allocation.resting), Decimal(0)) + sum((item.cost_basis for item in allocation.positions), Decimal(0)) == Decimal("500.00") + allocation.realized_pnl for allocation in result.final.allocations)
	assert all(session.sequence < forecast.sample.timing.entry_session.sequence for forecast in result.forecasts for session in forecast.sample.features.sessions)
	assert all(forecast.sample.target_state.value == "not_requested" for forecast in result.forecasts)

	prices.missing.add((samples.calendar.session(date(2024, 7, 8)), 17))
	incomplete = simulate(request, samples.boundary, samples.calendar, FixedModelScores(fixed, signals), prices)
	assert incomplete.successful and not incomplete.complete
	assert any(event.kind is EventKind.UNFILLED_BUY for event in incomplete.events)
