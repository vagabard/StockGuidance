import argparse
import json

import pytest

from stockguidance.cli.update_market_data import main
from stockguidance.workflows.failures import FailureCode, StageExecutionError, StageFailure, WorkflowStage


POLICY_ARGS = ["--publication-delay-minutes", "15", "--publication-evidence", "fixture-only", "--recent-window-days", "30", "--recent-refresh-hours", "1", "--archive-refresh-days", "7", "--correction-evidence", "fixture-only"]


def test_cli_parses_custom_source_and_emits_scoped_success(harness, capsys):
	def run(arguments: argparse.Namespace):
		assert arguments.data_root == "custom-data" and arguments.dbinfo_file == "custom.id"
		return harness.run()
	assert main([*POLICY_ARGS, "--data-root", "custom-data", "--dbinfo-file", "custom.id"], run=run) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] and payload["data_revision"]
	assert payload["history_start"] == "2024-07-01"
	assert "fixture-credential-reference" not in str(payload)


def test_cli_maps_unverified_coverage_to_nonzero_exit(harness, capsys):
	from dataclasses import replace
	from stockguidance.market_data.publication import DailyPublicationPolicy
	settings = replace(harness.settings, publication=DailyPublicationPolicy(None, "Unknown fixture policy"))
	assert main(POLICY_ARGS, run=lambda arguments: harness.run(settings)) == 2
	assert not json.loads(capsys.readouterr().out)["successful"]


def test_cli_failure_is_nonzero_and_sanitized(capsys):
	def run(arguments):
		raise StageExecutionError(StageFailure(WorkflowStage.REFRESH, FailureCode.PROVIDER, "Provider unavailable"))
	assert main(POLICY_ARGS, run=run) == 1
	assert json.loads(capsys.readouterr().out)["failure"]["code"] == "provider"


def test_cli_configuration_failure_does_not_leak_exception_text(capsys):
	def run(arguments):
		raise ValueError("fixture secret API key")
	assert main(POLICY_ARGS, run=run) == 1
	assert "fixture secret" not in capsys.readouterr().out


def test_missing_required_policy_fails_before_running():
	with pytest.raises(SystemExit) as error:
		main([])
	assert error.value.code == 2


def test_unknown_history_boundary_has_no_network_or_database_side_effects(tmp_path, capsys):
	assert main([*POLICY_ARGS, "--data-root", str(tmp_path)]) == 1
	assert list(tmp_path.iterdir()) == []
	assert "supported-history" in capsys.readouterr().out
