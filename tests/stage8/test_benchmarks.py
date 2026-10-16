from dataclasses import replace
from datetime import date
from decimal import Decimal

from stockguidance.reporting.simulation import simulation_payload


def test_benchmarks_use_identical_close_horizon_and_decision_eligible_universe(simulation):
	result = simulation.run(replace(simulation.request, end=date(2024, 7, 8)))
	benchmark = result.benchmarks[0]
	assert benchmark.entry.eligible_tickers == (17, 3)
	assert dict(benchmark.returns) == {17: 1.0, 3: 0.0}
	assert benchmark.equal_weight_return == 0.5
	assert benchmark.selected_return == result.trades[0].close_return == 1.0
	assert benchmark.selected_percentiles == ((17, 0.5),)
	assert benchmark.complete


def test_missing_loser_in_benchmark_prevents_subset_mean_and_percentile(simulation):
	simulation.prices.missing.add((simulation.calendar.session(date(2024, 7, 8)), 3))
	result = simulation.run(replace(simulation.request, end=date(2024, 7, 8)))
	benchmark = result.benchmarks[0]
	assert dict(benchmark.returns) == {17: 1.0, 3: None}
	assert not benchmark.complete and benchmark.equal_weight_return is None
	assert benchmark.selected_percentiles == ((17, None),)
	assert benchmark.selected_return == 1.0
	assert result.valuations[-1].total_equity == 2000
	assert not result.complete


def test_equal_weight_selected_return_does_not_drop_missing_selected_outcomes(simulation):
	simulation.prices.missing.add((simulation.calendar.session(date(2024, 7, 8)), 3))
	result = simulation.run(replace(simulation.request, end=date(2024, 7, 8), top_k=2))
	assert result.benchmarks[0].selected_return is None
	assert len(result.final.allocations[0].positions) == 1
	assert result.final.allocations[0].positions[0].ticker_id == 3


def test_reports_preserve_end_positions_cash_categories_and_precise_values(simulation):
	result = simulation.run()
	payload = simulation_payload(result)
	assert payload["target"]["formula"] == "close(T+1) / close(T) - 1"
	assert payload["execution"]["assumption"] == "pre_close_decision_same_close_fill"
	assert payload["final_valuation"]["total_equity"] == "1000.00"
	assert len(payload["final_portfolio"]["allocations"][0]["positions"]) == 1
	assert payload["prediction_cache"] == "disabled"
	assert payload["benchmarks"][-1]["close_returns"][0]["return"] is None
	assert "credential" not in str(payload) and "password" not in str(payload)
	assert len(payload["equity"]) == 5 and payload["realized_trades"][0]["close_return"] == 1


def test_execution_correction_changes_realized_results_without_reusing_predictions(simulation):
	first = simulation.run()
	simulation.prices.prices[simulation.calendar.session(date(2024, 7, 8)), 17] = Decimal(300)
	second = simulation.run()
	assert first.trades[0].net_proceeds == 2000 and second.trades[0].net_proceeds == 3000
	assert first.forecasts[0].prediction == second.forecasts[0].prediction
	assert first.benchmarks[0].selected_return == 1.0 and second.benchmarks[0].selected_return == 2.0
	assert simulation_payload(first)["execution_revision"] != simulation_payload(second)["execution_revision"]
