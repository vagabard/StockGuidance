from importlib import import_module
import os


def polygon_credential() -> str:
	credential: object = os.environ.get("POLYGON_API_KEY") or import_module("polygon_fetch").DEFAULT_API_KEY
	if not isinstance(credential, str) or not credential:
		raise ValueError("Provider credential is unavailable")
	return credential
