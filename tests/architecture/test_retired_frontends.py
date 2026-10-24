import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[2]
RETIRED = (
	"StockRecommenderFromModel.py", "batch_run.py", "calculate_ticker_correlations.py",
	"choose_next_day_stocks.py", "debug_feature_logging.py", "incremental_training_harness.py",
	"reinstallPackages.py", "run_recommender_from_model_dir.py", "simulate_trading.py",
	"train_pytorch_network.py", "train_pytorch_network_cli.py", "update_market_data.py",
	"validate_database.py", "database/__init__.py", "database/populate_db_daily_delta_stock_data.py",
	"database/populate_db_raw_stock_info.py", "database/populate_db_ticker_info.py",
	"scripts/aggregate_run_options.py", "scripts/generate_simulation_stats.py",
	"scripts/get_metrics_for_runs.py", "verify_operations/__init__.py",
	"verify_operations/verify_daily_delta_stock_data.py",
	"verify_operations/verify_daily_delta_stock_data_per_ticker.py",
	"verify_operations/verify_raw_stock_load.py", "viewers/__init__.py",
	"viewers/compare_run_statistics_viewer.py", "viewers/compare_runs_viewer.py",
	"viewers/examine_models_viewer.py", "viewers/run_selection_dashboard.py",
	"viewers/pages/__init__.py", "viewers/pages/compare_runs.py",
	"viewers/pages/compare_statistics.py", "viewers/pages/examine_models.py",
)


def test_legacy_frontends_and_adapters_are_absent():
	for relative_path in RETIRED:
		assert not (ROOT / relative_path).exists(), relative_path


def test_built_wheel_has_import_safe_credential_module_outside_checkout(tmp_path):
	project = tmp_path / "project"
	project.mkdir()
	shutil.copy2(ROOT / "pyproject.toml", project / "pyproject.toml")
	shutil.copy2(ROOT / "polygon_fetch.py", project / "polygon_fetch.py")
	shutil.copytree(ROOT / "src" / "stockguidance", project / "src" / "stockguidance", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
	wheels = tmp_path / "wheels"
	wheels.mkdir()
	build = subprocess.run([sys.executable, "-c", "from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])", str(wheels)], cwd=project, text=True, capture_output=True, timeout=60)
	assert build.returncode == 0, build.stderr
	wheel, = wheels.glob("*.whl")
	installed = tmp_path / "installed"
	with zipfile.ZipFile(wheel) as archive:
		assert "polygon_fetch.py" in archive.namelist()
		assert "stockguidance/py.typed" in archive.namelist()
		assert not any(name.startswith(("trainer/", "models/", "database/")) for name in archive.namelist())
		archive.extractall(installed)
	working = tmp_path / "working"
	working.mkdir()
	code = """
from pathlib import Path
import sys
from unittest.mock import patch

with patch("builtins.open", side_effect=AssertionError("import opened an application file")), \\
     patch("socket.socket", side_effect=AssertionError("import accessed network")), \\
     patch("subprocess.Popen", side_effect=AssertionError("import started a command")):
    import polygon_fetch
    import stockguidance
    from stockguidance.market_data.providers import configured_polygon
    assert isinstance(polygon_fetch.DEFAULT_API_KEY, str)
    assert bool(polygon_fetch.DEFAULT_API_KEY)
    assert not hasattr(polygon_fetch, "main")
    assert Path(polygon_fetch.__file__).parent == Path(sys.argv[1])
    assert Path(stockguidance.__file__).parent.parent == Path(sys.argv[1])
    with patch.dict("os.environ", {}, clear=True), \\
         patch.object(configured_polygon, "PolygonHttpTransport") as transport, \\
         patch.object(configured_polygon, "PolygonGroupedProvider") as grouped:
        configured_polygon.ConfiguredPolygonProvider().fetch(object(), object())
        assert transport.call_args.args[0] == polygon_fetch.DEFAULT_API_KEY
        assert grouped.return_value.fetch.call_count == 1
assert not {"torch", "numpy", "trainer", "models", "database.db_connect"} & sys.modules.keys()
"""
	environment = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, (str(installed), os.environ.get("PYTHONPATH", "")))), PYTHONDONTWRITEBYTECODE="1")
	result = subprocess.run([sys.executable, "-c", code, str(installed)], cwd=working, env=environment, text=True, capture_output=True, timeout=30)
	assert result.returncode == 0, result.stderr
	assert result.stdout == "" and result.stderr == ""
	assert not tuple(working.iterdir())
