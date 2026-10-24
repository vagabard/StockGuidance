from dataclasses import replace
from decimal import Decimal
import json

import pytest

from stockguidance.cli import reports as cli
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.reporting.documents import load_report
from stockguidance.reporting.metrics import compare_simulations, simulation_summary
from stockguidance.reporting.simulation import simulation_payload
from stockguidance.reporting.storage import write_report


def test_report_metrics_include_resting_cash_open_positions_and_drawdown(simulation, tmp_path, capsys):
	path = tmp_path / "simulation.json"
	write_report(path, simulation_payload(simulation.run()))
	summary = simulation_summary(load_report(path))
	assert summary.initial_capital == Decimal("1000.00") and summary.final_equity == Decimal("1000.00")
	assert summary.total_return == 0 and summary.maximum_drawdown == Decimal("0.5")
	assert summary.open_positions == 1 and summary.realized_trades == 2
	assert cli.main(["summary", str(path)]) == 0
	assert json.loads(capsys.readouterr().out)["results"][0]["maximum_drawdown"] == "0.5"


def test_comparison_allows_both_schedules_with_exact_shared_semantics(simulation, tmp_path):
	paths = []
	for policy in AllocationPolicy:
		request = replace(simulation.request, allocation=replace(simulation.request.allocation, policy=policy))
		path = tmp_path / f"{policy.value}.json"
		write_report(path, simulation_payload(simulation.run(request)))
		paths.append(path)
	summaries = compare_simulations(tuple(load_report(path) for path in paths))
	assert {row.allocation_policy for row in summaries} == {policy.value for policy in AllocationPolicy}
	assert {row.final_equity for row in summaries} == {Decimal("1000.00")}


@pytest.mark.parametrize("change", ["source", "period", "fee", "target", "feature", "schema"])
def test_report_comparison_and_reading_reject_legacy_or_incompatible_economics(simulation, tmp_path, change):
	payload = simulation_payload(simulation.run())
	first = tmp_path / "first.json"
	write_report(first, payload)
	if change == "source":
		payload["source"]["universe"] = "different-universe"
	elif change == "period":
		payload["start_session"] = "2024-07-08"
	elif change == "fee":
		payload["execution"]["fee_per_fill"] = "1.00"
	elif change == "target":
		payload["target"]["formula"] = "close(T) / open(T) - 1"
	elif change == "feature":
		payload["features"]["signal_mode"] = "entry_open"
	else:
		payload["schema"] = "legacy-saved-score-csv"
	second = tmp_path / "second.json"
	second.write_text(json.dumps(payload), encoding="utf-8")
	with pytest.raises(ValueError):
		compare_simulations((load_report(first), load_report(second)))


def test_unpriced_or_unfilled_position_remains_visible_in_metrics(simulation, tmp_path):
	simulation.prices.missing.add((simulation.calendar.session(simulation.request.start), 17))
	path = tmp_path / "incomplete.json"
	write_report(path, simulation_payload(simulation.run()))
	summary = simulation_summary(load_report(path))
	assert summary.successful and not summary.complete
	assert summary.final_equity == Decimal("500.00")


def test_legacy_report_errors_do_not_echo_credentials(tmp_path, capsys):
	path = tmp_path / "legacy.json"
	path.write_text('{"password":"fixture-password-never-log"}', encoding="utf-8")
	assert cli.main(["summary", str(path)]) == 1
	assert "password" not in capsys.readouterr().out
