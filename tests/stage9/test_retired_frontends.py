import ast
import importlib
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = (ROOT / "docs" / "stage9_retirement.md").read_text(encoding="utf-8")
FRONTENDS = re.findall(r"^\| `([^`]+\.py)` \| `([^`]+)` \|$", MANIFEST, re.MULTILINE)
RETIRED = re.findall(r"^- `([^`]+\.py)`$", MANIFEST, re.MULTILINE)


@pytest.mark.parametrize(("relative_path", "entrypoint"), [*FRONTENDS, ("polygon_fetch.py", "stockguidance.cli.update_market_data")])
def test_retained_frontends_import_safely_and_delegate_once(relative_path, entrypoint, monkeypatch):
	module_name, separator, function_name = entrypoint.partition(":")
	function_name = function_name if separator else "main"
	module = importlib.import_module(module_name)
	calls = []
	monkeypatch.setattr(module, function_name, lambda: calls.append(True) or 17)
	path = str(ROOT / relative_path)
	runpy.run_path(path, run_name="frontend_import_safety_check")
	assert calls == []
	if ".viewers." in module_name:
		runpy.run_path(path, run_name="__main__")
	else:
		with pytest.raises(SystemExit) as error:
			runpy.run_path(path, run_name="__main__")
		assert error.value.code == 17
	assert calls == [True]


def test_retired_paths_are_absent_and_supported_callers_have_no_retired_imports():
	assert len(RETIRED) == 85
	assert len(FRONTENDS) == 21
	for relative_path in RETIRED:
		assert not (ROOT / relative_path).exists(), relative_path
	retired_modules = {str(Path(relative_path).with_suffix("")).replace("\\", ".").replace("/", ".").removesuffix(".__init__") for relative_path in RETIRED}
	paths = [*ROOT.glob("*.py")]
	for directory in ("src", "tests", "scripts", "database", "verify_operations", "viewers"):
		paths.extend((ROOT / directory).rglob("*.py"))
	for path in paths:
		for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
			modules = [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
			for module in modules:
				if module:
					assert not any(module == retired or module.startswith(retired + ".") for retired in retired_modules), (path, module)


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
import importlib
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
    assert polygon_fetch.main is importlib.import_module("stockguidance.cli.update_market_data").main
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
