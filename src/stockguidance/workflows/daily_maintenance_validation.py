from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from stockguidance.config.source import SourceConfig
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.table_names import TableNames, quote_identifier
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.zscores.specification import ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.storage import ZScoreSeriesDefinition
from stockguidance.workflows.daily_maintenance_context import MaintenanceSession
from stockguidance.workflows.daily_maintenance_artifacts import create_run_directory, cleanup_successful_run


@dataclass
class ValidationStageResult:
	reports: list[dict[str, Any]]
	validations: list[dict[str, Any]]


def validate_materialized_zscores(source: SourceConfig, names: TableNames, workload: dict[str, Any], report: dict[str, Any], directory: Path) -> dict[str, Any]:
	from stockguidance.performance.independent_zscore_validation import validate
	selected = set(workload["zscore_sessions"])
	manifest = {**workload, "targets": [target for target in workload["targets"] if target["session"] in selected]}
	connection = MySQLConnectionFactory()(source.database_config)
	try:
		with redirect_stdout(sys.stderr):
			result = validate(connection, names, manifest, directory, report["zscores"]["series"],
				{"workload": workload["manifest_id"], "snapshot": uuid4().hex})
		metadata = result["metadata"]
		result["successful"] = result["status"] == "complete" and not result["mismatch_count"] and not metadata["series_findings"] and not metadata["metadata_findings"]
		return result
	finally:
		connection.rollback()
		connection.close()


def validate_batch(maintenance: MaintenanceSession, workload: dict[str, Any], report: dict[str, Any], checkpoint: Path) -> dict[str, Any]:
	sessions = workload["zscore_sessions"]
	print(f"Independently validating {sessions[0]} through {sessions[-1]}", file=sys.stderr, flush=True)
	context = maintenance.context
	zscores = validate_materialized_zscores(context.source, context.names, workload, report, checkpoint / "python-validation")
	validation_report = {"exit_code": 0 if zscores["successful"] else 1, "complete": zscores["successful"], "zscores": zscores,
		"findings": [] if zscores["successful"] else ["Independent z-score validation found mismatches"]}
	atomic_write(checkpoint / f"validation-{workload['manifest_id']}.json", encode_json(validation_report))
	return validation_report


def run_validation_stage(maintenance: MaintenanceSession) -> ValidationStageResult:
	from stockguidance.workflows.daily_maintenance_java import build_workload
	from stockguidance.workflows.daily_maintenance_source import availability_from_database

	context = maintenance.context
	checkpoint = create_run_directory(maintenance, "validation")
	reports: list[dict[str, Any]] = []
	validations: list[dict[str, Any]] = []
	availability = availability_from_database(maintenance)
	source_scope = content_id(asdict(context.source.identity))
	series_keys = tuple(ZScoreSeriesDefinition.from_specification(source_scope, context.calendar.calendar_id,
		ZScoreSpecification(family, (20, 60), delta_definition="close_ratio_to_prior_minus_one" if family is ZScoreFamily.PRICE_DELTA else "not_applicable"), window).series_key for family in ZScoreFamily for window in (20, 60))
	for offset in range(0, len(context.targets), context.arguments.batch_sessions):
		maintenance.assert_lock()
		batch = context.targets[offset:offset + context.arguments.batch_sessions]
		first = context.calendar.shift(batch[0], -61).session_date
		last = min(context.calendar.shift(batch[-1], 3).session_date, context.raw_sessions[-1].session_date)
		inputs = tuple(session for session in context.raw_sessions if first <= session.session_date <= last)
		workload = build_workload(context.source, context.names, context.calendar, inputs, batch, availability)
		placeholders = ",".join("%s" for _ in batch)
		maintenance.cursor.execute(f"SELECT d.transaction_date, s.family, s.`window`, z.content_digest, z.dependency_digest, z.availability_digest FROM {quote_identifier(context.names.zscore_sessions)} z JOIN {quote_identifier(context.names.zscore_series)} s ON s.series_key = z.series_key JOIN {quote_identifier(context.names.sessions)} d ON d.id = z.date_id WHERE d.transaction_date IN ({placeholders}) AND s.series_key IN (%s,%s,%s,%s) ORDER BY d.transaction_date, s.family, s.`window`", (*[session.session_date for session in batch], *series_keys))
		series = [{"session": str(session), "family": family, "window": int(window), "content_digest": content,
			"dependency_digest": dependency, "availability_digest": available} for session, family, window, content, dependency, available in maintenance.cursor.fetchall()]
		if len(series) != len(batch) * 4:
			raise ValueError("Stage 'validation' requires completed database z-score metadata; run --stage java first")
		report = {"status": "complete", "zscores": {"series": series}}
		reports.append(report)
		validation_report = validate_batch(maintenance, workload, report, checkpoint)
		validations.append(validation_report)
		if not validation_report["complete"]:
			break
	if all(item["complete"] for item in validations):
		cleanup_successful_run(checkpoint)
	return ValidationStageResult(reports, validations)
