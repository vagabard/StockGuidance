import json
from pathlib import Path
import re

import pytest

from stockguidance.cli.simulation_configuration import load_configuration


@pytest.mark.parametrize("retrain", [False, True])
def test_documented_complete_simulation_configuration_parses(tmp_path, retrain):
	path = Path(__file__).resolve().parents[2] / "docs" / "simulation.md"
	blocks = re.findall(r"```json\n(.*?)\n```", path.read_text(encoding="utf-8"), flags=re.DOTALL)
	configuration = json.loads(blocks[0])
	if retrain:
		configuration["retraining"] = json.loads(blocks[1])
	job = tmp_path / "documented.json"
	job.write_text(json.dumps(configuration), encoding="utf-8")
	parsed = load_configuration(job)
	assert parsed.features.ticker_ids == (17, 3) and parsed.features.lookback_sessions == 2
	assert bool(parsed.retraining) == retrain
