import ast
from pathlib import Path

import pytest

from stockguidance.cli.simulate import main, parse_args


def test_root_simulation_frontend_delegates_to_the_typed_cli():
	import simulate_trading

	assert simulate_trading.main is main


def test_old_score_directory_and_numeric_date_switches_are_rejected():
	with pytest.raises(SystemExit) as error:
		parse_args(["old-model-directory", "--from-saved-scores", "--date-range", "100-105"])
	assert error.value.code == 2


def test_frontend_has_no_worker_controller_or_file_discovery():
	path = Path(__file__).resolve().parents[1] / "simulate_trading.py"
	tree = ast.parse(path.read_text(encoding="utf-8"))
	imports = tuple(node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
	assert imports == ("stockguidance.cli.simulate",)
	assert not any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in ast.walk(tree))
