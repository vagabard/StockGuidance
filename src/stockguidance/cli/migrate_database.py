"""Operator-only Stage 12 database migration command."""

from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys

from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.simplified_migration import MigrationError, MigrationPaths, SimplifiedDatabaseMigration, build_source_configs


def _windows(value: str) -> tuple[int, ...]:
	try:
		result = tuple(int(item.strip()) for item in value.split(",") if item.strip())
	except ValueError:
		raise argparse.ArgumentTypeError("windows must be comma-separated integers") from None
	if not result or any(item < 2 for item in result) or len(set(result)) != len(result):
		raise argparse.ArgumentTypeError("windows must be distinct integers of at least 2")
	return result


def parser() -> argparse.ArgumentParser:
	result = argparse.ArgumentParser(description="Plan or explicitly execute the Stage 12 simplified-schema migration")
	result.add_argument("phase", nargs="?", choices=("plan", "apply", "status", "validate", "cutover", "retire"), default="plan", help="plan and status do not write; validate checkpoints comparisons in the target")
	result.add_argument("--legacy-dbinfo-file", type=Path, required=True)
	result.add_argument("--target-dbinfo-file", type=Path, required=True)
	result.add_argument("--data-root", type=Path, default=Path("data"))
	result.add_argument("--windows", type=_windows, default=(20, 60))
	result.add_argument("--backup-manifest", type=Path, required=False)
	result.add_argument("--export-dir", type=Path, required=False)
	result.add_argument("--journal", type=Path, required=False)
	result.add_argument("--batch-size", type=int, default=10_000, help="Maximum source rows per SQL copy/validation batch (default: 10000)")
	result.add_argument("--apply-confirmation", default=None)
	result.add_argument("--cutover-confirmation", default=None)
	result.add_argument("--retire-confirmation", default=None)
	return result


def _paths(arguments: argparse.Namespace) -> MigrationPaths:
	if arguments.backup_manifest is None or arguments.export_dir is None:
		raise MigrationError("--backup-manifest and --export-dir are required for an operator migration")
	return MigrationPaths.from_values(arguments.backup_manifest, arguments.export_dir, arguments.journal)


def main(argv: list[str] | None = None) -> int:
	arguments = parser().parse_args(argv)
	try:
		legacy_config = load_database_configuration(arguments.legacy_dbinfo_file)
		target_config = load_database_configuration(arguments.target_dbinfo_file)
		if legacy_config.database.casefold() == target_config.database.casefold():
			raise MigrationError("Legacy and target databases must be distinct")
		legacy, target = build_source_configs(arguments.legacy_dbinfo_file, arguments.target_dbinfo_file, arguments.data_root)
		migration = SimplifiedDatabaseMigration(legacy, target, MySQLConnectionFactory(), MySQLConnectionFactory(), windows=arguments.windows, batch_size=arguments.batch_size)
		if arguments.phase == "plan":
			paths = None if arguments.backup_manifest is None and arguments.export_dir is None else _paths(arguments)
			payload = migration.plan(paths).payload
			print(json.dumps(payload, sort_keys=True, indent=2, default=str))
			return 2 if payload.get("blocked_reasons") else 0
		paths = _paths(arguments)
		if arguments.phase == "apply":
			payload = migration.apply(paths, confirmation=arguments.apply_confirmation or "")
		elif arguments.phase == "status":
			payload = migration.status(paths)
		elif arguments.phase == "validate":
			payload = migration.validate(paths)
		elif arguments.phase == "cutover":
			payload = migration.cutover(paths, confirmation=arguments.cutover_confirmation or "")
		else:
			payload = migration.retire(paths, confirmation=arguments.retire_confirmation or "")
		print(json.dumps(payload, sort_keys=True, indent=2, default=str))
		return 0
	except Exception as error:
		print(f"database migration failed: {error}", file=sys.stderr)
		print("Fix the reported problem and rerun the same apply/validate command; the database checkpoint is authoritative.", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
