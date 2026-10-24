from datetime import date
from typing import Any

from stockguidance.calendar.clock import SystemClock
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.update_schema import assert_source, assert_transactional_tables, assert_zscore_tables
from stockguidance.workflows.daily_maintenance_context import LOOKBACK_YEARS, MaintenanceSession, maintenance_range, open_maintenance, raw_import_state, session_workload
from stockguidance.workflows.daily_maintenance_java import build_workload, execute_java, java_preflight, prepare_partial_imports, run_java_stage
from stockguidance.workflows.daily_maintenance_source import archive_entry, archive_identity, availability_from_database, export_archive, read_archives_for_java, run_source_stage, SourceStageResult
from stockguidance.workflows.daily_maintenance_validation import run_validation_stage, validate_batch, validate_materialized_zscores


_archive_entry = archive_entry
_raw_import_state = raw_import_state
_prepare_partial_imports = prepare_partial_imports


def _database_availability(maintenance: MaintenanceSession) -> dict[date, str]:
	return availability_from_database(maintenance)


def _report(
	maintenance: MaintenanceSession,
	stage: str | None,
	source_result: SourceStageResult,
	reports: list[dict[str, Any]],
	validations: list[dict[str, Any]],
	run_validation: bool,
) -> dict[str, object]:
	context = maintenance.context
	if stage == "source":
		successful = True
	elif stage == "java":
		successful = True
	else:
		successful = all(item["complete"] and item["zscores"]["successful"] for item in validations)
	training_through = context.targets[-1] if context.calendar.shift(context.targets[-1], 1).session_date in source_result.availability else context.calendar.shift(context.targets[-1], -1)
	return {
		"successful": successful,
		"stage": stage or "all",
		"lookback_years": 2,
		"start_date": context.targets[0].session_date.isoformat(),
		"end_date": context.targets[-1].session_date.isoformat(),
		"downloaded_sessions": source_result.downloaded,
		"java_updates": reports,
		"partial_imports_removed": maintenance.partial_imports_removed,
		"python_validation": {"complete": successful if run_validation else False, "skipped": not run_validation,
			"batches": [{**item, "findings": [str(finding) for finding in item["findings"]]} for item in validations]},
		"forecast_session": context.targets[-1].session_date.isoformat(),
		"forecast_input_session": context.targets[-1].session_date.isoformat(),
		"training_outcomes_through": training_through.session_date.isoformat(),
	}


def run_daily_maintenance(arguments: Any) -> dict[str, object]:
	stage = getattr(arguments, "stage", None)
	run_source = stage in (None, "source")
	run_java = stage in (None, "java")
	run_validation = stage in (None, "validation")
	command = java_preflight(arguments) if run_java else None
	with open_maintenance(
		arguments,
		run_java=run_java,
		clock=SystemClock(),
		connection_factory=MySQLConnectionFactory(),
		assert_source=assert_source,
		assert_transactional_tables=assert_transactional_tables,
		assert_zscore_tables=assert_zscore_tables,
		prepare_partial_imports=prepare_partial_imports,
	) as maintenance:
		if run_source:
			source_result = run_source_stage(maintenance)
		elif run_java:
			source_result = read_archives_for_java(maintenance)
		else:
			source_result = SourceStageResult({}, _database_availability(maintenance), 0, set())

		if stage == "source":
			return _report(maintenance, stage, source_result, [], [], False)

		if stage == "validation":
			validation_result = run_validation_stage(maintenance)
			return _report(maintenance, stage, source_result, validation_result.reports, validation_result.validations, True)

		java_result = run_java_stage(maintenance, command or [], source_result,
			validate_batch=validate_batch if run_validation else None)
		return _report(maintenance, stage, source_result, java_result.reports, java_result.validations, run_validation)
