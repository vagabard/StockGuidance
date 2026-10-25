import argparse
import json

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


def test_cli_verbose_configuration_failure_reports_detail_on_stderr(capsys):
	def run(arguments):
		raise ValueError("unsupported requested session range")
	assert main([*POLICY_ARGS, "--verbose"], run=run) == 1
	captured = capsys.readouterr()
	assert "unsupported requested session range" in captured.err
	assert json.loads(captured.out)["successful"] is False


def test_default_policy_is_applied_before_running(harness):
	seen = {}

	def run(arguments: argparse.Namespace):
		seen.update(vars(arguments))
		return harness.run()

	assert main([], run=run) == 0
	assert seen["publication_delay_minutes"] == 15.0
	assert seen["publication_evidence"].startswith("Default operational policy")
	assert seen["recent_window_days"] == 30.0
	assert seen["recent_refresh_hours"] == 0.0
	assert seen["archive_refresh_days"] == 30.0
	assert seen["correction_evidence"] == "Default daily maintenance correction policy"


def test_unknown_history_boundary_has_no_network_or_database_side_effects(tmp_path, capsys):
	assert main([*POLICY_ARGS, "--data-root", str(tmp_path)]) == 1
	assert list(tmp_path.iterdir()) == []
	assert "supported-history" in capsys.readouterr().out
