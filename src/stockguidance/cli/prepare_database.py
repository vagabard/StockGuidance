"""Standalone database-preparation operator commands.

The command intentionally has no provider or training dependency.  ``plan`` and
``render-sql`` are database-free; selection, apply, and validation use only the
explicit target database configuration supplied by the operator.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from collections.abc import Sequence
import sys

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import SourceConfig
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.preparation import (
	PreparationError,
	PreparationStatus,
	PreparationStore,
	VolumePreparationRequest,
	VolumePreparationPlan,
	_plan_from_payload,
	apply_volume_result,
	calculate_volume_zscores,
	plan_volume_zscore,
	read_inputs,
	read_result,
	render_volume_sql,
	select_volume_inputs,
	validate_volume_result,
	write_inputs,
	write_result,
)
from stockguidance.database.simplified_runtime import source_from_database
from stockguidance.database.table_names import TableNames, load_table_names
from stockguidance.market_data.identity import content_id, data_configuration_key, source_key


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Plan, inspect, calculate, apply, validate, or resume bounded target-schema preparation")
	arguments.add_argument("--config", type=Path, required=True, help="Sanitized preparation JSON")
	arguments.add_argument("--step", choices=("plan", "create", "render-sql", "select", "calculate", "apply", "validate", "status", "resume"), default="plan")
	arguments.add_argument("--operation-id")
	arguments.add_argument("--sql-step", choices=("plan", "select", "calculate", "apply", "validate"), default="select")
	arguments.add_argument("--sql-output", type=Path)
	return arguments


def _config(path: Path) -> dict[str, object]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		raise PreparationError("Preparation configuration is unavailable or invalid") from None
	if not isinstance(value, dict):
		raise PreparationError("Preparation configuration must be an object")
	return value


def _path(config: dict[str, object], name: str, default: str | None = None) -> Path:
	value = config.get(name, default)
	if not isinstance(value, str) or not value:
		raise PreparationError(f"Preparation configuration requires {name}")
	return Path(value)


def _ints(config: dict[str, object], name: str, default: tuple[int, ...] | None = None) -> tuple[int, ...]:
	value = config.get(name, default)
	if not isinstance(value, list) and not isinstance(value, tuple):
		raise PreparationError(f"Preparation configuration requires {name}")
	try:
		return tuple(int(str(item)) for item in value)
	except (TypeError, ValueError):
		raise PreparationError(f"Preparation configuration has invalid {name}") from None


def _calendar(config: dict[str, object], calendar_id: str, start: date, end: date) -> ExchangeCalendar:
	return ExchangeCalendar(calendar_id, start - timedelta(days=370), end + timedelta(days=370))


def _source_and_names(config: dict[str, object]) -> tuple[SourceConfig, TableNames]:
	database_config = _path(config, "database_config")
	data_root = _path(config, "data_root", "data")
	connect = MySQLConnectionFactory()
	return source_from_database(database_config, data_root, connect, _names(config))


def _names(config: dict[str, object]) -> TableNames:
	value = config.get("table_names_file")
	return load_table_names(None if value is None else _path(config, "table_names_file"))


def _request(config: dict[str, object], source: SourceConfig | None = None) -> VolumePreparationRequest:
	start = date.fromisoformat(str(config.get("start_date")))
	end = date.fromisoformat(str(config.get("end_date")))
	calendar_id = source.calendar_id if source is not None else str(config.get("calendar_id", "XNYS"))
	source_scope = source_key(source.identity) if source is not None else str(config.get("source_scope", ""))
	configuration_key = data_configuration_key(source.data_configuration) if source is not None else str(config.get("configuration_key", ""))
	return VolumePreparationRequest(source_scope, configuration_key, calendar_id, start, end, _ints(config, "ticker_ids"), _ints(config, "windows", (20, 60)), int(str(config.get("batch_size", 1000))))


def _store(config: dict[str, object]) -> PreparationStore:
	return PreparationStore(_path(config, "operations_root", "database-preparations"))


def _plan_from_operation(store: PreparationStore, operation_id: str) -> tuple[dict[str, object], VolumePreparationPlan]:
	manifest = store.read(operation_id)
	return manifest, _plan_from_payload(manifest["plan"])


def _print_or_write(value: str, path: Path | None) -> None:
	if path is None:
		print(value)
	else:
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(value, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
	try:
		arguments = parser().parse_args(argv)
		config = _config(arguments.config)
		store = _store(config)
		if arguments.step == "status":
			if not arguments.operation_id:
				raise PreparationError("status requires --operation-id")
			print(json.dumps(store.read(arguments.operation_id), sort_keys=True))
			return 0
		if arguments.step == "plan":
			request = _request(config)
			calendar = _calendar(config, request.calendar_id, request.start_date, request.end_date)
			print(json.dumps(plan_volume_zscore(request, calendar, _names(config)).payload(), sort_keys=True))
			return 0
		if arguments.step == "create":
			request = _request(config)
			calendar = _calendar(config, request.calendar_id, request.start_date, request.end_date)
			plan = plan_volume_zscore(request, calendar, _names(config))
			operation_id = store.create(plan, datetime.now(timezone.utc))
			print(json.dumps({"operation_id": operation_id, "status": PreparationStatus.PLANNED.value, "plan_digest": plan.plan_digest}, sort_keys=True))
			return 0
		if not arguments.operation_id:
			raise PreparationError(f"{arguments.step} requires --operation-id")
		manifest, plan = _plan_from_operation(store, arguments.operation_id)
		if arguments.step == "render-sql":
			rendered = render_volume_sql(plan, arguments.sql_step)
			_print_or_write(rendered.script, arguments.sql_output)
			return 0
		if arguments.step in {"select", "apply", "resume", "validate"}:
			source, names = _source_and_names(config)
			connect = MySQLConnectionFactory()
			if arguments.step == "select":
				rows = select_volume_inputs(source, connect, plan, names)
				digest = write_inputs(store.path(arguments.operation_id) / "inputs.json", plan, rows)
				source_revision = content_id([(row.session_date.isoformat(), row.ticker_id, row.raw_revision) for row in rows])
				store.update(arguments.operation_id, status=PreparationStatus.EXPORTED.value, source_revision=source_revision, input_digest=digest)
				print(json.dumps({"operation_id": arguments.operation_id, "status": PreparationStatus.EXPORTED.value, "rows": len(rows), "input_digest": digest}, sort_keys=True))
				return 0
			result_path = store.path(arguments.operation_id) / "results.json"
			if arguments.step in {"apply", "resume", "validate"}:
				result = read_result(result_path, plan)
				if arguments.step in {"apply", "resume"}:
					apply_volume_result(source, connect, plan, result, table_names=names)
					store.update(arguments.operation_id, status=PreparationStatus.APPLIED.value)
					print(json.dumps({"operation_id": arguments.operation_id, "status": PreparationStatus.APPLIED.value}, sort_keys=True))
					return 0
				validation = validate_volume_result(source, connect, plan, result, table_names=names)
				validation_payload = {"rows_checked": validation.rows_checked, "metadata_checked": validation.metadata_checked, "ready_rows": validation.ready_rows, "result_digest": validation.result_digest}
				store.update(arguments.operation_id, status=PreparationStatus.VALIDATED.value, validation=validation_payload)
				print(json.dumps({"operation_id": arguments.operation_id, "status": PreparationStatus.VALIDATED.value, **validation_payload}, sort_keys=True))
				return 0
		if arguments.step == "calculate":
			rows = read_inputs(store.path(arguments.operation_id) / "inputs.json", plan)
			result = calculate_volume_zscores(plan, rows, datetime.now(timezone.utc))
			digest = write_result(result_path, result)
			store.update(arguments.operation_id, status=PreparationStatus.CALCULATED.value, result_digest=digest)
			print(json.dumps({"operation_id": arguments.operation_id, "status": PreparationStatus.CALCULATED.value, "rows": len(result.rows), "result_digest": digest}, sort_keys=True))
			return 0
		raise PreparationError("Unsupported preparation step")
	except Exception:
		try:
			if arguments.operation_id:
				store.update(arguments.operation_id, status=PreparationStatus.FAILED.value, failure={"code": "operation_failed", "detail": "The bounded preparation step failed; inspect the typed inputs and retry"})
		except Exception:
			pass
		print(json.dumps({"successful": False, "error": "Preparation operation failed; inspect its manifest and rerun the bounded step or resume"}, sort_keys=True), file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
