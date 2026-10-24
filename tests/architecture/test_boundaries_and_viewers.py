import ast
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from stockguidance.cli import inspect_features as diagnostics
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.reporting.simulation import simulation_payload
from stockguidance.reporting.storage import write_report
from stockguidance.training import jobs
from stockguidance.viewers import artifacts, reports


@pytest.mark.parametrize("name", ["batch", "incremental", "inspect_features", "reports"])
def test_new_frontend_help_has_no_application_side_effects(name, tmp_path, monkeypatch, capsys):
	monkeypatch.chdir(tmp_path)
	with pytest.raises(SystemExit) as error:
		importlib.import_module(f"stockguidance.cli.{name}").main(["--help"])
	assert error.value.code == 0
	assert capsys.readouterr().out and not tuple(tmp_path.iterdir())


def test_package_dependencies_do_not_import_legacy_modules_or_application_cli_helpers():
	root = Path(__file__).resolve().parents[2] / "src" / "stockguidance"
	legacy = {"trainer", "models", "database", "StockRecommenderFromModel", "training_instance_provider", "feature_vector_builder", "simulate_trading", "choose_next_day_stocks", "train_pytorch_network"}
	for path in root.rglob("*.py"):
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			modules = [node.module] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
			for module in modules:
				if module:
					assert module.split(".")[0] not in legacy, (path, module)
					assert "cli" in path.relative_to(root).parts or not module.startswith("stockguidance.cli"), (path, module)


@pytest.mark.parametrize("kind", ["features", "correlations"])
def test_diagnostic_frontend_reads_the_explicit_shared_job_without_training(job, samples, tmp_path, monkeypatch, capsys, kind):
	path = tmp_path / "diagnostic-job.json"
	path.write_text(json.dumps(job), encoding="utf-8")
	JsonHistoryStore(tmp_path / "source" / "history").save(samples.boundary)
	monkeypatch.setattr(jobs, "VerifiedCloseRepository", lambda *values: samples.repository)
	assert diagnostics.main(["--config", str(path), "--kind", kind, "--sample-count", "10", "--seed", "31"]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"]
	assert payload["schema"] == ("stockguidance-feature-inspection-1" if kind == "features" else "stockguidance-target-correlations-1")
	assert "database_config" not in str(payload)
	assert not tuple(tmp_path.glob("*.sgmodel"))


def test_report_viewer_uses_explicit_current_reports_with_no_model_or_database_discovery(simulation, tmp_path, monkeypatch):
	path = tmp_path / "simulation.json"
	write_report(path, simulation_payload(simulation.run()))
	tables, errors = [], []
	ui = SimpleNamespace(set_page_config=lambda **values: None, title=lambda value: None, text_area=lambda value: str(path), dataframe=tables.append, subheader=lambda value: None, write=lambda value: None, error=errors.append)
	monkeypatch.setitem(sys.modules, "streamlit", ui)
	reports.main()
	assert not errors and tables[0][0]["final_equity"] == "1000.00"
	assert tables[0][0]["open_positions"] == 1


def test_artifact_viewer_requires_the_explicit_valid_identity(simulation, monkeypatch):
	values = iter((str(simulation.reference.path), simulation.reference.artifact_id))
	shown, errors = [], []
	ui = SimpleNamespace(set_page_config=lambda **values: None, title=lambda value: None, text_input=lambda value: next(values), json=shown.append, error=errors.append)
	monkeypatch.setitem(sys.modules, "streamlit", ui)
	artifacts.main()
	assert not errors and shown[0]["artifact_id"] == simulation.reference.artifact_id
	assert shown[0]["target"]["formula"] == "close(T+1) / close(T) - 1"
	assert "password" not in str(shown)
