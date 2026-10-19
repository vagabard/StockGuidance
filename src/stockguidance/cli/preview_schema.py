import argparse
from collections.abc import Sequence
import json
from pathlib import Path

from stockguidance.database.table_names import load_table_names
from stockguidance.database.update_schema import update_schema_statements, zscore_schema_statements


def _windows(value: str) -> tuple[int, ...]:
	parts = tuple(part.strip() for part in value.split(","))
	if not parts or any(not part for part in parts):
		raise ValueError("Invalid z-score windows")
	try:
		values = tuple(int(part) for part in parts)
	except ValueError:
		raise ValueError("Invalid z-score windows") from None
	if any(type(window) is not int or window < 2 for window in values) or len(set(values)) != len(values):
		raise ValueError("Invalid z-score windows")
	return values


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Preview selected database table names and explicit updater DDL without opening a database")
	arguments.add_argument("--table-names-file", type=Path)
	arguments.add_argument("--windows", type=_windows, help="Optional comma-separated z-score windows")
	return arguments


def main(argv: Sequence[str] | None = None) -> int:
	try:
		arguments = parser().parse_args(argv)
		table_names = load_table_names(arguments.table_names_file)
		payload = {"tables": table_names.tables(), "zscore_templates": table_names.templates(), "ddl": update_schema_statements(table_names)}
		if arguments.windows is not None:
			zscore_ddl = zscore_schema_statements(table_names, arguments.windows)
			payload["zscore_ddl"] = zscore_ddl
			payload["zscore_metadata_ddl"] = zscore_ddl[:2]
			payload["zscore_tables"] = {str(window): {"price": tables.price, "price_delta": tables.price_delta} for window, tables in table_names.resolve_zscore_tables(arguments.windows).items()}
		print(json.dumps(payload, sort_keys=True))
		return 0
	except ValueError:
		print(json.dumps({"successful": False, "error": "Invalid or unavailable table-names file"}, sort_keys=True))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
