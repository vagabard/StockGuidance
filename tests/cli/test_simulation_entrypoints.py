from pathlib import Path

import pytest

from stockguidance.cli.simulate import main, parse_args


def test_root_simulation_frontend_is_retired():
	path = Path(__file__).resolve().parents[1] / "simulate_trading.py"
	assert not path.exists()


def test_old_score_directory_and_numeric_date_switches_are_rejected():
	with pytest.raises(SystemExit) as error:
		parse_args(["old-model-directory", "--from-saved-scores", "--date-range", "100-105"])
	assert error.value.code == 2


def test_package_simulation_frontend_is_the_supported_cli():
	assert main.__module__ == "stockguidance.cli.simulate"
