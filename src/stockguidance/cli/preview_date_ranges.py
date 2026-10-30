"""Preview structured date ranges without database, provider, model, or Torch access."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.calendar.ranges import (
	DatePlanSpec,
	DateRangeResolver,
	RangeConfigurationError,
	coverage_snapshot_from_mapping,
	parse_preview_spec,
	source_identity_from_mapping,
)


def _mapping(value: object, name: str) -> dict[str, object]:
	if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
		raise RangeConfigurationError(f"{name} must be a string-keyed object")
	return dict(value)


def _date(value: object, name: str) -> datetime:
	if not isinstance(value, str):
		raise RangeConfigurationError(f"{name} must be an ISO timestamp")
	try:
		parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
	except ValueError:
		raise RangeConfigurationError(f"{name} must be an ISO timestamp") from None
	if parsed.tzinfo is None:
		raise RangeConfigurationError(f"{name} must be timezone aware")
	return parsed


def _load(path: Path) -> dict[str, object]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError):
		raise RangeConfigurationError("Cannot read the explicit preview configuration") from None
	return _mapping(value, "preview configuration")


def _preview(path: Path) -> dict[str, object]:
	payload = _load(path)
	calendar_payload = _mapping(payload.get("calendar"), "calendar")
	name = calendar_payload.get("name", calendar_payload.get("calendar_id"))
	if not isinstance(name, str):
		raise RangeConfigurationError("calendar.name is required")
	try:
		calendar_start = datetime.fromisoformat(str(calendar_payload["start"])).date()
		calendar_end = datetime.fromisoformat(str(calendar_payload["end"])).date()
	except (KeyError, TypeError, ValueError):
		raise RangeConfigurationError("calendar.start and calendar.end must be ISO dates") from None
	clock_value = payload.get("clock_now", payload.get("now"))
	clock_now = datetime.now(timezone.utc) if clock_value is None else _date(clock_value, "clock_now")
	source = source_identity_from_mapping(_mapping(payload.get("source"), "source"))
	calendar = ExchangeCalendar(name, calendar_start, calendar_end)
	coverage = coverage_snapshot_from_mapping(source, calendar.calendar_id, _mapping(payload.get("coverage"), "coverage"))
	specification: DatePlanSpec = parse_preview_spec(payload)
	resolution = DateRangeResolver(calendar, coverage, source, _FrozenClock(clock_now)).resolve_plan(specification)
	return {"schema": "stockguidance-date-preview-1", "successful": True, "resolution": resolution.to_mapping()}


class _FrozenClock:
	def __init__(self, value: datetime) -> None:
		self.value = value

	def now(self) -> datetime:
		return self.value


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Preview exchange-session date and dependency resolution without opening a database or loading a model.")
	parser.add_argument("--config", type=Path, required=True, help="JSON file containing source, calendar, coverage, and date-range settings")
	arguments = parser.parse_args(argv)
	try:
		print(json.dumps(_preview(arguments.config), sort_keys=True, allow_nan=False))
		return 0
	except Exception:
		print(json.dumps({"schema": "stockguidance-date-preview-1", "successful": False, "failure": "Date range preview could not be resolved"}, sort_keys=True))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
