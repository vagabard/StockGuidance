import os
from pathlib import Path
import subprocess
import sys


def test_all_package_imports_are_safe_in_an_empty_working_directory(tmp_path: Path) -> None:
	source_root = Path(__file__).resolve().parents[2] / "src"
	code = """
import importlib
import pkgutil
import sys
from unittest.mock import patch

with patch('builtins.open', side_effect=AssertionError('import opened an application file')), \
     patch('socket.socket', side_effect=AssertionError('import accessed the network')), \
     patch('subprocess.Popen', side_effect=AssertionError('import started a command')):
    import stockguidance
    for module in pkgutil.walk_packages(stockguidance.__path__, stockguidance.__name__ + '.'):
        importlib.import_module(module.name)

assert not {'torch', 'numpy', 'polygon_fetch', 'database.db_connect', 'choose_next_day_stocks'} & sys.modules.keys()
"""
	python_path = os.pathsep.join(filter(None, (str(source_root), os.environ.get("PYTHONPATH", ""))))
	environment = dict(os.environ, PYTHONPATH=python_path, PYTHONDONTWRITEBYTECODE="1")
	completed = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=30)
	assert completed.returncode == 0, completed.stderr
	assert completed.stdout == ""
	assert completed.stderr == ""
	assert list(tmp_path.iterdir()) == []
