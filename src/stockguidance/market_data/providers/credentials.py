from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path


def _legacy_credential() -> object:
	try:
		module = import_module("polygon_fetch")
	except ModuleNotFoundError as error:
		if error.name != "polygon_fetch":
			raise
		root = Path(__file__).resolve().parents[4]
		path = root / "polygon_fetch.py"
		if not (root / "pyproject.toml").is_file() or not path.is_file():
			return None
		specification = spec_from_file_location("polygon_fetch", path)
		if specification is None or specification.loader is None:
			return None
		module = module_from_spec(specification)
		specification.loader.exec_module(module)
	return getattr(module, "DEFAULT_API_KEY", None)


def polygon_credential() -> str:
	credential: object = os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY") or _legacy_credential()
	if not isinstance(credential, str) or not credential:
		raise ValueError("Provider credential is unavailable; set MASSIVE_API_KEY or POLYGON_API_KEY")
	return credential
