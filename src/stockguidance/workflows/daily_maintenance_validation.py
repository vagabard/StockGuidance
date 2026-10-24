from contextlib import redirect_stdout
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from stockguidance.config.source import SourceConfig
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.table_names import TableNames
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.workflows.daily_maintenance_context import MaintenanceSession


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
	zscores = validate_materialized_zscores(context.source, context.names, workload, report, checkpoint.parent / "python-validation")
	validation_report = {"exit_code": 0 if zscores["successful"] else 1, "complete": zscores["successful"], "zscores": zscores,
		"findings": [] if zscores["successful"] else ["Independent z-score validation found mismatches"]}
	atomic_write(checkpoint / f"validation-{workload['manifest_id']}.json", encode_json(validation_report))
	return validation_report


def run_validation_stage(maintenance: MaintenanceSession) -> ValidationStageResult:
	context = maintenance.context
	checkpoint = Path(context.arguments.java_checkpoint_directory).resolve()
	reports: list[dict[str, Any]] = []
	validations: list[dict[str, Any]] = []
	for offset in range(0, len(context.targets), context.arguments.batch_sessions):
		batch = context.targets[offset:offset + context.arguments.batch_sessions]
		expected_sessions = [session.session_date.isoformat() for session in batch]
		candidates = []
		for workload_path in sorted(checkpoint.glob("workload-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
			with workload_path.open(encoding="utf-8") as stream:
				workload = json.load(stream)
			if workload.get("zscore_sessions") == expected_sessions:
				candidates.append((workload_path, workload))
		if not candidates:
			raise ValueError(f"Stage 'validation' requires a completed Java workload for {batch[0].session_date} through {batch[-1].session_date}; run --stage java first")
		workload_path, workload = candidates[0]
		report_path = checkpoint / f"report-{workload['manifest_id']}.json"
		if not report_path.is_file():
			raise ValueError(f"Stage 'validation' requires the Java report for {workload_path.name}; run --stage java first")
		with report_path.open(encoding="utf-8") as stream:
			report = json.load(stream)
		reports.append(report)
		validation_report = validate_batch(maintenance, workload, report, checkpoint)
		validations.append(validation_report)
		if not validation_report["complete"]:
			break
	return ValidationStageResult(reports, validations)
