from pathlib import Path


def project_root() -> Path:
	current = Path.cwd().resolve()
	for candidate in (current, *current.parents):
		if (candidate / "pyproject.toml").is_file() and (candidate / "src").is_dir():
			return candidate
	module_root = Path(__file__).resolve().parents[3]
	if (module_root / "pyproject.toml").is_file() and (module_root / "src").is_dir():
		return module_root
	return current


def resolve_cli_path(value: str, default_name: str) -> Path:
	if not isinstance(value, str) or not value:
		raise ValueError("CLI path must be nonempty text")
	path = Path(value)
	if path.is_absolute():
		return path
	if value == default_name:
		return project_root() / value
	return path
