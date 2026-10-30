"""Render bounded Stage 4 universe-selection SQL without opening a database."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from collections.abc import Sequence
import sys

from stockguidance.database.table_names import load_table_names
from stockguidance.database.universe_selection import UniverseSqlScope, render_universe_selection_sql


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Render read-only SQL for bounded frozen-universe selection")
	arguments.add_argument("--config", type=Path, required=True, help="Sanitized universe-selection scope JSON")
	arguments.add_argument("--step", choices=("plan", "select", "validate"), default="select")
	arguments.add_argument("--sql-output", type=Path)
	arguments.add_argument("--table-names-file", type=Path)
	return arguments


def _scope(path: Path) -> UniverseSqlScope:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError):
		raise ValueError("Universe-selection configuration is unavailable or invalid") from None
	if not isinstance(payload, dict):
		raise ValueError("Universe-selection configuration must be an object")
	dates = payload.get("session_dates")
	if not isinstance(dates, list) or not dates:
		raise ValueError("session_dates must be a nonempty array")
	try:
		session_dates = tuple(sorted({date.fromisoformat(str(item)) for item in dates}))
	except ValueError:
		raise ValueError("session_dates must contain ISO dates") from None
	ids_value = payload.get("ticker_ids")
	ticker_ids = None
	if ids_value is not None:
		if not isinstance(ids_value, list) or not ids_value:
			raise ValueError("ticker_ids must be a nonempty array when provided")
		try:
			ticker_ids = tuple(int(item) for item in ids_value)
		except (TypeError, ValueError):
			raise ValueError("ticker_ids must contain positive integers") from None
	filters = payload.get("filters", {})
	if not isinstance(filters, dict):
		raise ValueError("filters must be an object")
	read_volume = payload.get("read_volume", filters.get("average_volume") is not None)
	read_transactions = payload.get("read_transactions", filters.get("legacy_transactions") is not None)
	if type(read_volume) is not bool or type(read_transactions) is not bool:
		raise ValueError("read_volume and read_transactions must be booleans")
	return UniverseSqlScope(session_dates, ticker_ids, read_volume, read_transactions)


def main(argv: Sequence[str] | None = None) -> int:
	try:
		arguments = parser().parse_args(argv)
		scope = _scope(arguments.config)
		names = load_table_names(arguments.table_names_file)
		rendered = render_universe_selection_sql(scope, arguments.step, names)
		if arguments.sql_output is None:
			print(rendered.script, end="")
		else:
			arguments.sql_output.parent.mkdir(parents=True, exist_ok=True)
			arguments.sql_output.write_text(rendered.script, encoding="utf-8")
			print(json.dumps({"step": rendered.step, "sql_output": str(arguments.sql_output), "read_only": rendered.read_only, "calculation_route": rendered.calculation_route}, sort_keys=True))
		return 0
	except Exception as error:
		print(json.dumps({"successful": False, "error": str(error)}), file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
