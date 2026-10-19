import argparse
from collections.abc import Sequence
import json
from pathlib import Path

from stockguidance.database.table_names import load_table_names
from stockguidance.database.update_schema import update_schema_statements


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Preview selected database table names and explicit updater DDL without opening a database")
	arguments.add_argument("--table-names-file", type=Path)
	return arguments


def main(argv: Sequence[str] | None = None) -> int:
	try:
		table_names = load_table_names(parser().parse_args(argv).table_names_file)
		payload = {"tables": table_names.tables(), "zscore_templates": table_names.templates(), "ddl": update_schema_statements(table_names)}
		print(json.dumps(payload, sort_keys=True))
		return 0
	except ValueError:
		print(json.dumps({"successful": False, "error": "Invalid or unavailable table-names file"}, sort_keys=True))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
