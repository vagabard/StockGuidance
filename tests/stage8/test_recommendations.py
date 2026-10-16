from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import json

import pytest

from stage7.test_cli import arguments, configure_cli
from stockguidance.cli import recommend as cli
from stockguidance.portfolio.planning import plan_orders
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.portfolio.serialization import portfolio_payload
from stockguidance.portfolio.state import initial_portfolio
from stockguidance.reporting.recommendations import recommendation_payload, render_recommendations
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.prices import RepositoryExecutionPrices
from stockguidance.simulation.score_source import FixedModelScores, HistoricalSignals
from stockguidance.workflows.failures import StageExecutionError
from stockguidance.workflows.recommend import recommend
from stockguidance.workflows import recommend as workflow
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


def capital_job(recommender):
	job = recommender.job
	return replace(job, allocation=AllocationSpec(job.allocation.policy, recommender.calendar.session(job.entry_date)))


def test_capital_aware_recommendation_returns_the_shared_plan_without_booking_fills(recommender):
	job = capital_job(recommender)
	state = initial_portfolio(job.allocation, Decimal("1000.00"), recommender.storage.clock.now())
	run = recommend(job, recommender.storage.boundary, recommender.calendar, recommender.services, recommender.storage.clock, portfolio=state)
	assert run.order_plan == plan_orders(state, job.allocation, run.sample.timing, run.recommendation.ranking)
	assert run.portfolio == state and state.allocations[0].available_cash == 1000
	assert not state.allocations[0].positions and not state.allocations[0].resting
	payload = recommendation_payload(run)
	assert payload["kind"] == "portfolio_order_plan"
	assert payload["orders_planned"] and payload["cash_rest_enforced"] and not payload["fills_recorded"]
	assert payload["supplied_portfolio"] == portfolio_payload(state)
	assert payload["order_plan"]["orders"][0]["maximum_debit"] == "1000.00"
	assert "no fills are recorded" in render_recommendations(run)


def test_ranking_without_capital_does_not_claim_cash_rest_enforcement(recommender):
	run = recommender.run()
	assert run.order_plan is None and run.portfolio is None
	payload = recommendation_payload(run)
	assert payload["kind"] == "ranked_suggestions"
	assert not payload["cash_rest_enforced"] and payload["supplied_portfolio"] is None


def test_future_portfolio_fails_before_refresh(recommender):
	job = capital_job(recommender)
	state = initial_portfolio(job.allocation, Decimal("1000.00"), recommender.storage.clock.now() + timedelta(seconds=1))
	with pytest.raises(StageExecutionError):
		recommend(job, recommender.storage.boundary, recommender.calendar, recommender.services, recommender.storage.clock, portfolio=state)
	assert not recommender.storage.provider.calls and not recommender.storage.database.statements


def test_complete_recommendation_and_simulation_use_identical_model_signal_and_order_plan(recommender, tmp_path):
	job = capital_job(recommender)
	cutoff = recommender.calendar.session(date(2024, 7, 8)).closes_at + timedelta(minutes=16)
	training = train_from_history(HistoryTrainingRequest(recommender.plan.decisions, cutoff, job.features, job.target, recommender.plan.configuration, tmp_path / "pre-period.sgmodel"), recommender.storage.boundary, recommender.calendar, job.publication, recommender.closes)
	job = replace(job, current_artifact=training.artifact, model=replace(job.model, artifact_id=training.artifact.artifact_id))
	state = initial_portfolio(job.allocation, Decimal("1000.00"), recommender.storage.clock.now())
	recommendation = recommend(job, recommender.storage.boundary, recommender.calendar, recommender.services, recommender.storage.clock, portfolio=state)
	request = SimulationRequest(job.source.identity, job.features, job.target, job.allocation, job.entry_date, job.entry_date, Decimal("1000.00"), timedelta(hours=1), job.eligibility, job.top_k)
	signals = HistoricalSignals(recommender.storage.boundary, recommender.calendar, job.publication, recommender.closes, recommender.services.eligibility)
	result = simulate(request, recommender.storage.boundary, recommender.calendar, FixedModelScores(training.artifact, signals), RepositoryExecutionPrices(recommender.storage.boundary, recommender.closes, recommender.calendar.session(job.entry_date).closes_at + timedelta(minutes=16)))
	assert recommendation.order_plan == result.plans[0]
	assert recommendation.scores == result.forecasts[0].prediction.scores
	assert recommendation.sample == result.forecasts[0].sample
	assert recommendation.recommendation.artifact_id == result.forecasts[0].artifact_id
	assert result.final.allocations[0].available_cash == 1000
	assert not result.final.allocations[0].positions


@pytest.mark.parametrize("mode", ["capital", "snapshot"])
def test_recommendation_cli_accepts_actual_cash_or_full_snapshot(recommender, job_json, tmp_path, monkeypatch, capsys, mode):
	configure_cli(recommender, monkeypatch)
	job_json["allocation"]["anchor_date"] = "2024-07-09"
	path = tmp_path / "recommend.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")
	values = arguments(recommender, path)
	if mode == "capital":
		values += ["--capital", "1000.00"]
	else:
		state = initial_portfolio(capital_job(recommender).allocation, Decimal("1000.00"), recommender.storage.clock.now())
		snapshot = tmp_path / "portfolio.json"
		snapshot.write_text(json.dumps(portfolio_payload(state)), encoding="utf-8")
		values += ["--portfolio-state", str(snapshot)]
	assert cli.main(values) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["cash_rest_enforced"] and payload["orders_planned"] and not payload["fills_recorded"]


def test_later_entry_requires_snapshot_instead_of_inventing_initial_cash(recommender, job_json, tmp_path, monkeypatch, capsys):
	configure_cli(recommender, monkeypatch)
	path = tmp_path / "recommend.json"
	path.write_text(json.dumps(job_json), encoding="utf-8")
	assert cli.main(arguments(recommender, path) + ["--capital", "1000.00"]) == 1
	assert not json.loads(capsys.readouterr().out)["successful"]
	assert not recommender.storage.provider.calls and not recommender.storage.database.statements


def test_planning_that_crosses_entry_close_cannot_return_success(recommender, monkeypatch):
	job = capital_job(recommender)
	state = initial_portfolio(job.allocation, Decimal("1000.00"), recommender.storage.clock.now())
	original, calls = workflow.plan_orders, []

	def planner(*arguments):
		plan = original(*arguments)
		calls.append(plan)
		if len(calls) == 2:
			recommender.storage.clock.value = recommender.calendar.session(job.entry_date).closes_at
		return plan

	monkeypatch.setattr(workflow, "plan_orders", planner)
	with pytest.raises(StageExecutionError, match="Entry close passed before portfolio") as error:
		recommend(job, recommender.storage.boundary, recommender.calendar, recommender.services, recommender.storage.clock, portfolio=state)
	assert error.value.failure.stage.value == "planning"
