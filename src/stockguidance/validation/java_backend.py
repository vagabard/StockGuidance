"""Opt-in process boundary for the bounded Java validation operation."""

from collections.abc import Callable
import json
from pathlib import Path
import subprocess
from typing import Any


class JavaValidationBackendError(RuntimeError):
	"""The Java process did not return a validation report."""


def run_java_validator(
	database_config: Path,
	request_path: Path,
	*,
	classpath: str | Path,
	java_executable: str = "java",
	working_directory: Path | None = None,
	timeout_seconds: float = 300.0,
	run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
	"""Run the Java validator with paths only; credentials stay in the config file."""
	if not isinstance(database_config, Path) or not isinstance(request_path, Path) or not isinstance(classpath, (Path, str)):
		raise ValueError("Java validation paths must be pathlib.Path values")
	command = (
		java_executable,
		"-cp",
		str(classpath),
		"stockguidance.validation.ValidatorMain",
		"--config",
		str(database_config),
		"--request",
		str(request_path),
	)
	completed = run(
		command,
		cwd=None if working_directory is None else str(working_directory),
		capture_output=True,
		text=True,
		check=False,
		timeout=timeout_seconds,
	)
	try:
		payload = json.loads(completed.stdout)
	except (TypeError, ValueError, json.JSONDecodeError) as error:
		raise JavaValidationBackendError("Java validation returned no machine-readable report") from error
	if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("backend") != "java":
		raise JavaValidationBackendError("Java validation returned an unsupported report")
	if completed.returncode not in {0, 1, 2}:
		raise JavaValidationBackendError("Java validation process failed before producing a validation result")
	if completed.returncode == 1 and payload.get("error"):
		raise JavaValidationBackendError("Java validation backend failed")
	return payload


def normalize_validation_report(payload: dict[str, Any]) -> dict[str, Any]:
	"""Reduce Python or Java output to the shared selected-operation contract."""
	if not isinstance(payload, dict):
		raise ValueError("Validation report must be an object")
	session_values = payload["sessions"] if "sessions" in payload else payload.get("session_results", [])
	if not isinstance(session_values, list):
		raise ValueError("Validation sessions must be a list")
	return {
		"complete": bool(payload.get("complete")),
		"failed": bool(payload.get("failed")),
		"exit_code": int(payload.get("exit_code", 1)),
		"source": payload.get("source"),
		"calendar": payload.get("calendar", {"version": payload.get("calendar_version")}),
		"scope": payload.get("scope", {"start": payload.get("start"), "end": payload.get("end"), "as_of": payload.get("as_of")}),
		"history": payload.get("history", {"history_start": payload.get("history_start"), "history_provenance": payload.get("history_provenance")}),
		"sessions": [_normalize_session(item) for item in session_values],
		"findings": sorted((_normalize_finding(item) for item in payload.get("findings", [])), key=lambda item: (item["session_date"] or "", item["check"], item["detail"])),
		"repair_plan": _normalize_repair_plan(payload.get("repair_plan")),
	}


def compare_validation_reports(python_payload: dict[str, Any], java_payload: dict[str, Any]) -> None:
	"""Raise a useful assertion when normalized Python and Java reports diverge."""
	left = normalize_validation_report(python_payload)
	right = normalize_validation_report(java_payload)
	if left != right:
		raise AssertionError("Python and Java validation reports differ")


def _normalize_session(value: Any) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise ValueError("Validation session report must be an object")
	return {
		"date": value.get("date"),
		"state": value.get("state"),
		"checks": list(value.get("checks", [])),
		"source_keys": sorted(value.get("source_keys", [])),
		"raw_valid_keys": sorted(value.get("raw_valid_keys", [])),
		"derived_valid_keys": sorted(value.get("derived_valid_keys", [])),
		"source_count": value.get("source_count"),
		"raw_count": value.get("raw_count"),
		"derived_count": value.get("derived_count"),
		"source_state": value.get("source_state"),
		"available_at": value.get("available_at"),
	}


def _normalize_finding(value: Any) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise ValueError("Validation finding must be an object")
	return {
		"category": value.get("category"),
		"check": value.get("check"),
		"detail": value.get("detail"),
		"session_date": value.get("session_date"),
		"dependency_session_date": value.get("dependency_session_date"),
		"tickers": sorted(value.get("tickers", [])),
		"record_ids": sorted(value.get("record_ids", [])),
		"blocking": bool(value.get("blocking")),
	}


def _normalize_repair_plan(value: Any) -> dict[str, Any] | None:
	if value is None:
		return None
	if not isinstance(value, dict):
		raise ValueError("Validation repair plan must be an object")
	items = []
	for item in value.get("items", []):
		if not isinstance(item, dict):
			raise ValueError("Validation repair item must be an object")
		items.append({key: item.get(key) for key in ("action", "session_date", "check", "tickers")})
	return {
		"schema_version": value.get("schema_version"),
		"source": value.get("source"),
		"start": value.get("start"),
		"end": value.get("end"),
		"as_of": value.get("as_of"),
		"items": sorted(items, key=lambda item: (item["session_date"] or "", item["check"], item["action"], item["tickers"])),
	}
