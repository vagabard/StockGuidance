from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
import torch

from support.samples import calendar, chronological_storage, harness, samples
from support.training import cpu_threads
from support.recommendations import job_json, recommender
from stockguidance.artifacts.exporting import export_artifact
from stockguidance.artifacts.loading import load_artifact
from stockguidance.market_data.closes import CloseIssue, PriceState
from stockguidance.models.specification import ModelSpec
from stockguidance.portfolio.marks import ClosingMark
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec
from stockguidance.recommendations.eligibility import EligibilityPolicy, LiquidityObservation
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.prices import ExecutionFrame
from stockguidance.simulation.score_source import FixedModelScores, HistoricalSignals
from stockguidance.training.configuration import DeviceChoice, TrainingConfig
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


class FixtureLiquidity:
	def __init__(self, repository):
		self.repository, self.calls, self.missing = repository, [], set()

	def read(self, query):
		self.calls.append(query)
		return tuple(LiquidityObservation(item.ticker_id, 500, item.timing, item.revision) for item in self.repository.read(query).observations if (item.timing.session, item.ticker_id) not in self.missing)


class FixtureExecution:
	def __init__(self, source, calendar):
		self.source, self.calendar = source, calendar
		self.audit_cutoff = calendar.session(date(2024, 7, 11)).closes_at + timedelta(minutes=16)
		self.missing, self.calls = set(), []
		self.prices = {(calendar.session(date(2024, 7, day)), ticker_id): Decimal(str(price)) for day, price in ((5, 100), (8, 200), (9, 100), (10, 50), (11, 100)) for ticker_id in (17,)}
		self.prices.update({(calendar.session(date(2024, 7, day)), 3): Decimal(50) for day in (5, 8, 9, 10, 11)})

	def read(self, session, ticker_ids):
		self.calls.append((session, ticker_ids))
		quotes, issues = [], []
		for ticker_id in ticker_ids:
			key = session, ticker_id
			if key in self.missing or key not in self.prices:
				issues.append(CloseIssue(ticker_id, session, PriceState.EXPECTED_ABSENCE, "Fixture suspended trading or missing close"))
			else:
				quotes.append(ClosingMark(ticker_id, session, self.prices[key], f"execution-{session.session_date}-{ticker_id}-{self.prices[key]}"))
		return ExecutionFrame(self.source, session, ticker_ids, tuple(quotes), tuple(issues))


@pytest.fixture
def simulation(samples, tmp_path):
	features = replace(samples.specification, lookback_sessions=1)
	configuration = TrainingConfig(model=ModelSpec(hidden_widths=()), epochs=1, batch_size=1, device=DeviceChoice.CPU, seed=42)
	cutoff = samples.calendar.session(date(2024, 7, 3)).closes_at + timedelta(minutes=16)
	training = train_from_history(HistoryTrainingRequest((samples.decision(2),), cutoff, features, samples.target, configuration, tmp_path / "trained.sgmodel"), samples.boundary, samples.calendar, samples.publication, samples.repository)
	loaded = load_artifact(training.artifact)
	with torch.no_grad():
		for parameter in loaded.model.parameters():
			parameter.zero_()
		linear = tuple(module for module in loaded.model.modules() if isinstance(module, torch.nn.Linear))[-1]
		linear.bias.copy_(torch.tensor([1.0, -1.0]))
	reference = export_artifact(tmp_path / "fixed.sgmodel", loaded.model, loaded.metadata)
	liquidity = FixtureLiquidity(samples.repository)
	signals = HistoricalSignals(samples.boundary, samples.calendar, samples.publication, samples.repository, liquidity)
	request = SimulationRequest(samples.boundary.source, features, samples.target, AllocationSpec(AllocationPolicy.ALTERNATE_SESSIONS, samples.calendar.session(date(2024, 7, 5))), date(2024, 7, 5), date(2024, 7, 11), Decimal("1000.00"), eligibility=EligibilityPolicy(200), top_k=1)
	prices = FixtureExecution(request.source, samples.calendar)

	def run(request_override=None, *, source=None):
		return simulate(request_override or request, samples.boundary, samples.calendar, source or FixedModelScores(reference, signals), prices)

	return SimpleNamespace(samples=samples, calendar=samples.calendar, request=request, reference=reference, signals=signals, liquidity=liquidity, prices=prices, configuration=configuration, run=run, tmp_path=tmp_path)


@pytest.fixture
def simulation_json(simulation, tmp_path, monkeypatch):
	from stockguidance.workflows import simulation_runtime as setup

	from stockguidance.market_data.history_store import JsonHistoryStore

	JsonHistoryStore(tmp_path / "source" / "history").save(simulation.samples.boundary)
	(tmp_path / "custom-config.id").write_text(json.dumps({"host": "fixture", "database": "fixture", "user": "fixture", "password": ""}), encoding="utf-8")
	monkeypatch.setattr(setup, "ExchangeCalendar", lambda *values: simulation.calendar)
	monkeypatch.setattr(setup, "MySQLConnectionFactory", lambda: object())
	monkeypatch.setattr(setup, "MySQLValidationRepository", lambda *values: object())
	monkeypatch.setattr(setup, "VerifiedCloseRepository", lambda *values: simulation.samples.repository)
	monkeypatch.setattr(setup, "VerifiedEligibilityRepository", lambda *values: simulation.liquidity)
	return {"source": asdict(simulation.request.source), "data_root": str(tmp_path / "source"), "database_config": str(tmp_path / "custom-config.id"), "features": asdict(simulation.request.features), "start_date": "2024-07-05", "end_date": "2024-07-11", "allocation": {"policy": "alternate_sessions", "anchor_date": "2024-07-05"}, "initial_capital": "1000.00", "decision_minutes_before_close": 30, "publication": {"delay_minutes": 15, "evidence": "Fixture-only publication"}, "execution_audit_cutoff": "2024-07-11T21:00:00+00:00", "eligibility": {"minimum_transactions": 200}, "top_k": 1, "fee_per_fill": "0.00", "retraining": None}
