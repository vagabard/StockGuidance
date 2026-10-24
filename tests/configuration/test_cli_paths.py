from pathlib import Path


def test_default_cli_paths_follow_checkout_when_launched_elsewhere(monkeypatch):
	from stockguidance.cli.paths import project_root, resolve_cli_path

	monkeypatch.chdir(Path(__file__).resolve().parents[2] / "tests")
	root = project_root()
	assert root == Path(__file__).resolve().parents[2]
	assert resolve_cli_path("data", "data") == root / "data"
	assert resolve_cli_path("dbinfo.id", "dbinfo.id") == root / "dbinfo.id"


def test_explicit_relative_cli_paths_remain_relative(monkeypatch, tmp_path):
	from stockguidance.cli.paths import resolve_cli_path

	monkeypatch.chdir(tmp_path)
	assert resolve_cli_path("custom-data", "data") == Path("custom-data")
	assert resolve_cli_path("custom.id", "dbinfo.id") == Path("custom.id")
