from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Sequence

from stockguidance.config.source import DEFAULT_CALENDAR_ID
from stockguidance.performance.candidates import ManifestPaths, WORKLOADS, aggregate_candidate_reports, build_manifest, run_python_baselines


def _path(value: str) -> Path:
	path = Path(value)
	if not value:
		raise argparse.ArgumentTypeError("Path must be nonempty")
	return path


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Build and run isolated MySQL migration candidates; SQLite requires explicit offline selection")
	mode = arguments.add_mutually_exclusive_group(required=True)
	mode.add_argument("--build-manifest", action="store_true")
	mode.add_argument("--run-python", action="store_true")
	mode.add_argument("--aggregate", action="store_true")
	mode.add_argument("--run-java-writer-trial", action="store_true")
	mode.add_argument("--run-java-zscore-trial", action="store_true")
	mode.add_argument("--run-sql-raw-trial", action="store_true")
	mode.add_argument("--run-sql-derived-trial", action="store_true")
	mode.add_argument("--run-sql-zscore-trial", action="store_true")
	mode.add_argument("--run-stage9-raw", action="store_true")
	mode.add_argument("--run-stage9-derived", action="store_true")
	mode.add_argument("--run-stage9-zscores", action="store_true")
	mode.add_argument("--run-stage10", action="store_true")
	mode.add_argument("--run-stage11a", action="store_true")
	mode.add_argument("--run-stage11b", action="store_true")
	mode.add_argument("--run-stage12", action="store_true")
	mode.add_argument("--run-stage13", action="store_true")
	mode.add_argument("--run-stage14", action="store_true")
	mode.add_argument("--run-stage15", action="store_true")
	arguments.add_argument("--data-root", type=_path, default=Path("data"))
	arguments.add_argument("--dbinfo-file", type=_path, default=Path("dbinfo.id"))
	arguments.add_argument("--evaluation-plan", type=_path, default=Path("output/legacy-migration/evaluation-plan.json"))
	arguments.add_argument("--precision-report", type=_path, default=Path("output/legacy-migration/precision-inventory.json"))
	arguments.add_argument("--audit-report", type=_path, default=Path("output/legacy-migration/audit.json"))
	arguments.add_argument("--manifest", type=_path, default=Path("output/legacy-migration/benchmark/candidates/manifest.json"))
	arguments.add_argument("--output", type=_path, default=Path("output/legacy-migration/benchmark/candidates/python-baselines.json"))
	arguments.add_argument("--target", type=_path, default=Path("output/legacy-migration/benchmark/candidates/python-disposable.sqlite"), help="Offline database file, used only with --storage sqlite")
	arguments.add_argument("--storage", choices=("mysql", "sqlite"), default="mysql")
	arguments.add_argument("--calendar-id", default=DEFAULT_CALENDAR_ID, help="exchange_calendars calendar name, for example XNYS or XNAS")
	arguments.add_argument("--disposable-schema")
	arguments.add_argument("--workloads", default=",".join(WORKLOADS))
	arguments.add_argument("--repetitions", type=int, default=4)
	arguments.add_argument("--timeout-seconds", type=float, default=3600.0)
	arguments.add_argument("--report", type=_path, action="append", default=[])
	arguments.add_argument("--aggregate-output", type=_path, default=Path("output/legacy-migration/benchmark/candidates/study.json"))
	arguments.add_argument("--stage9-timeout-seconds", type=float, default=3600.0)
	arguments.add_argument("--stage9-disposable-schema")
	arguments.add_argument("--stage9-profile", choices=("full", "compact"), default="full")
	arguments.add_argument("--stage9-resume", action="store_true")
	arguments.add_argument("--stage9-retry-failed", action="store_true")
	arguments.add_argument("--stage9-max-trials", type=int)
	arguments.add_argument("--stage9-phase", choices=("all", "measure", "audit"), default="all")
	arguments.add_argument("--stage9-audit-series-limit", type=int)
	arguments.add_argument("--stage9a-report", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage9a-20261021/stage9a-raw.json"))
	arguments.add_argument("--stage9b-report", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage9b-20261021-rerun/stage9b-derived.json"))
	arguments.add_argument("--stage10-study-report", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage9c-compact-20261022-ready/study.json"))
	arguments.add_argument("--stage10-diagnostic", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage9c-independent-diagnostic-20261022-ready/diagnostic.json"))
	arguments.add_argument("--stage10-provisional-decision", type=_path, default=Path("output/legacy-migration/decision.json"))
	arguments.add_argument("--stage10-audit-report", type=_path, default=Path("output/legacy-migration/audit.json"))
	arguments.add_argument("--stage10-output", type=_path, default=Path("output/legacy-migration/decision.json"))
	arguments.add_argument("--stage11-decision", type=_path, default=Path("output/legacy-migration/decision.json"))
	arguments.add_argument("--stage11-evidence-path", type=_path, default=Path("output/legacy-migration/evidence"))
	arguments.add_argument("--stage11-cutoff")
	arguments.add_argument("--stage11-storage", choices=("sqlite", "mysql"), default="sqlite")
	arguments.add_argument("--stage11-target", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage11a-disposable.sqlite"))
	arguments.add_argument("--stage11-sessions", required=False)
	arguments.add_argument("--stage11-workloads", default="raw,derived,zscores")
	arguments.add_argument("--stage11-output", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage11a-integration.json"))
	arguments.add_argument("--stage11-checkpoint-directory", type=_path)
	arguments.add_argument("--stage11-disposable-schema")
	arguments.add_argument("--stage11a-report", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage11a-mysql-raw-derived.json"))
	arguments.add_argument("--stage11b-output", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage11b-recovery.json"))
	arguments.add_argument("--stage11b-checkpoint-directory", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage11b-checkpoints"))
	arguments.add_argument("--stage11b-session", default="2026-04-29")
	arguments.add_argument("--stage12-stage11b-report", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage11b-recovery-4.json"))
	arguments.add_argument("--stage12-backup", type=_path, default=Path("output/database-backups/stockforecasting-full-20261019-112816.sql"))
	arguments.add_argument("--stage12-restore-schema", default="stockguidance_disposable_stage12_restore_20261022")
	arguments.add_argument("--stage12-reuse-restored-schema", action="store_true")
	arguments.add_argument("--stage12-output", type=_path, default=Path("output/legacy-migration/stage12.json"))
	arguments.add_argument("--stage12-zscore-sample-count", type=int, default=3)
	arguments.add_argument("--stage12-java-timeout-seconds", type=float, default=7200.0)
	arguments.add_argument("--stage13-stage12-report", type=_path, default=Path("output/legacy-migration/stage12.json"))
	arguments.add_argument("--stage13-stage11b-report", type=_path, default=Path("output/legacy-migration/benchmark/candidates/stage11b-recovery-4.json"))
	arguments.add_argument("--stage13-output", type=_path, default=Path("output/legacy-migration/population.json"))
	arguments.add_argument("--stage13-chunk-size", type=int, default=20)
	arguments.add_argument("--stage13-java-timeout-seconds", type=float, default=7200.0)
	arguments.add_argument("--stage13-no-resume", action="store_true")
	arguments.add_argument("--stage14-stage13-report", type=_path, default=Path("output/legacy-migration/population.json"))
	arguments.add_argument("--stage14-stage13-manifest", type=_path, default=Path("output/legacy-migration/stage13/population-manifest.json"))
	arguments.add_argument("--stage14-stage12-report", type=_path, default=Path("output/legacy-migration/stage12.json"))
	arguments.add_argument("--stage14-snapshot", type=_path, default=Path("output/legacy-migration/snapshot.json"))
	arguments.add_argument("--stage14-output", type=_path, default=Path("output/legacy-migration/zscores.json"))
	arguments.add_argument("--stage14-chunk-size", type=int, default=20)
	arguments.add_argument("--stage14-java-timeout-seconds", type=float, default=7200.0)
	arguments.add_argument("--stage14-audit-limit", type=int, default=3)
	arguments.add_argument("--stage14-audit-seed", type=int, default=20261022)
	arguments.add_argument("--stage14-max-chunks", type=int)
	arguments.add_argument("--stage14-no-resume", action="store_true")
	arguments.add_argument("--stage15-stage12-report", type=_path, default=Path("output/legacy-migration/stage12.json"))
	arguments.add_argument("--stage15-stage13-report", type=_path, default=Path("output/legacy-migration/population.json"))
	arguments.add_argument("--stage15-stage13-manifest", type=_path, default=Path("output/legacy-migration/stage13/population-manifest.json"))
	arguments.add_argument("--stage15-stage14-report", type=_path, default=Path("output/legacy-migration/zscores.json"))
	arguments.add_argument("--stage15-stage14-manifest", type=_path, default=Path("output/legacy-migration/stage14/zscore-manifest.json"))
	arguments.add_argument("--stage15-snapshot", type=_path, default=Path("output/legacy-migration/snapshot.json"))
	arguments.add_argument("--stage15-output", type=_path, default=Path("output/legacy-migration/final-audit.json"))
	arguments.add_argument("--stage15-zscore-batch-size", type=int, default=256)
	arguments.add_argument("--stage15-workers", type=int, default=5)
	arguments.add_argument("--stage15-fetch-size", type=int, default=1000)
	arguments.add_argument("--stage15-sessions-per-batch", type=int, default=128)
	arguments.add_argument("--stage15-cache-slots", type=int, default=2)
	arguments.add_argument("--stage15-max-batches", type=int)
	arguments.add_argument("--stage15-progress-seconds", type=float, default=10.0)
	arguments.add_argument("--stage15-benchmark", action="store_true")
	arguments.add_argument("--stage15-no-resume", action="store_true")
	return arguments


def _resolve(path: Path) -> Path:
	return path.resolve()


def main(argv: Sequence[str] | None = None) -> int:
	arguments = parser().parse_args(argv)
	if arguments.run_stage9_raw:
		from stockguidance.performance.stage9_raw import aggregate_stage9_study, run_stage9_raw

		try:
			raw_report = run_stage9_raw(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), _resolve(arguments.output), timeout_seconds=arguments.stage9_timeout_seconds)
			aggregate_stage9_study(raw_report, _resolve(arguments.output))
		except Exception:
			print(json.dumps({"successful": False, "error": "Stage 9a raw measurement failed"}))
			return 1
		print(json.dumps({"successful": raw_report["status"] == "complete", "raw_report": str((_resolve(arguments.output) / "stage9a-raw.json").resolve()), "study": str((_resolve(arguments.output) / "study.json").resolve()), "status": raw_report["status"]}))
		return 0 if raw_report["status"] == "complete" else 1
	if arguments.run_stage9_derived:
		from stockguidance.performance.stage9_derived import aggregate_stage9_study, run_stage9_derived

		try:
			derived_report = run_stage9_derived(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), _resolve(arguments.output), timeout_seconds=arguments.stage9_timeout_seconds)
			raw_path = _resolve(arguments.stage9a_report)
			raw_report = json.loads(raw_path.read_text(encoding="utf-8"))
			study = aggregate_stage9_study(raw_report, derived_report, _resolve(arguments.output))
		except Exception:
			print(json.dumps({"successful": False, "error": "Stage 9b derived measurement failed"}))
			return 1
		print(json.dumps({"successful": derived_report["status"] == "complete", "derived_report": str((_resolve(arguments.output) / "stage9b-derived.json").resolve()), "study": str((_resolve(arguments.output) / "study.json").resolve()), "status": study["status"]}))
		return 0 if derived_report["status"] == "complete" else 1
	if arguments.run_stage9_zscores:
		from stockguidance.performance.stage9_zscores import aggregate_stage9_study, run_stage9_zscores

		try:
			zscore_report = run_stage9_zscores(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), _resolve(arguments.output), timeout_seconds=arguments.stage9_timeout_seconds, disposable_schema=arguments.stage9_disposable_schema, profile=arguments.stage9_profile, resume=arguments.stage9_resume, max_trials=arguments.stage9_max_trials, phase=arguments.stage9_phase, audit_series_limit=arguments.stage9_audit_series_limit, retry_failed=arguments.stage9_retry_failed)
			raw_report = json.loads(_resolve(arguments.stage9a_report).read_text(encoding="utf-8"))
			derived_report = json.loads(_resolve(arguments.stage9b_report).read_text(encoding="utf-8"))
			study = aggregate_stage9_study(raw_report, derived_report, zscore_report, _resolve(arguments.output))
		except Exception as error:
			print(json.dumps({"successful": False, "error": "Stage 9c z-score measurement failed", "failure_type": type(error).__name__, "detail": str(error)}))
			return 1
		successful = zscore_report["status"] == "complete" or zscore_report.get("execution", {}).get("status") == "paused"
		print(json.dumps({"successful": successful, "comparison_complete": zscore_report["status"] == "complete", "execution": zscore_report.get("execution"), "zscore_report": str((_resolve(arguments.output) / "stage9c-zscores.json").resolve()), "study": str((_resolve(arguments.output) / "study.json").resolve()), "status": study["status"]}))
		return 0 if successful else 1
	if arguments.run_stage10:
		from stockguidance.performance.stage10_selection import Stage10Error, build_stage10_decision, write_stage10_decision

		try:
			decision = build_stage10_decision(_resolve(arguments.stage10_study_report), _resolve(arguments.manifest), _resolve(arguments.stage10_provisional_decision), _resolve(arguments.stage10_audit_report), _resolve(arguments.stage10_diagnostic), _resolve(arguments.stage10_output))
			write_stage10_decision(decision, _resolve(arguments.stage10_output))
		except (OSError, Stage10Error) as error:
			print(json.dumps({"successful": False, "error": "Stage 10 selection failed", "detail": str(error)}))
			return 1
		print(json.dumps({"successful": True, "decision": str(_resolve(arguments.stage10_output)), "status": decision["status"], "production_ready": decision["production_ready"]}))
		return 0
	if arguments.run_stage11a:
		from stockguidance.performance.stage11_integration import Stage11Error, run_mysql, run_offline, validate_prerequisites

		if not arguments.stage11_cutoff or not arguments.stage11_sessions:
			parser().error("--run-stage11a requires --stage11-cutoff and --stage11-sessions")
		try:
			cutoff = datetime.fromisoformat(arguments.stage11_cutoff)
			workloads = tuple(item for item in (value.strip() for value in arguments.stage11_workloads.split(",")) if item)
			sessions = tuple(item for item in (value.strip() for value in arguments.stage11_sessions.split(",")) if item)
			context = validate_prerequisites(_resolve(arguments.stage11_decision), _resolve(arguments.manifest), _resolve(arguments.audit_report), _resolve(arguments.stage11_evidence_path), cutoff)
			if arguments.stage11_storage == "mysql":
				if not arguments.stage11_disposable_schema:
					parser().error("--run-stage11a --stage11-storage mysql requires --stage11-disposable-schema")
				checkpoint_directory = _resolve(arguments.stage11_checkpoint_directory or _resolve(arguments.stage11_output).parent)
				report = run_mysql(context, _resolve(arguments.dbinfo_file), arguments.stage11_disposable_schema, sessions, workloads, _resolve(arguments.stage11_output), checkpoint_directory)
			else:
				report = run_offline(context, _resolve(arguments.stage11_target), sessions, workloads, _resolve(arguments.stage11_output))
		except (OSError, Stage11Error, ValueError) as error:
			print(json.dumps({"successful": False, "error": "Stage 11a integration failed", "detail": str(error)}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "operation": "run_stage11a", "report": str(_resolve(arguments.stage11_output)), "status": report["status"], "production_access": report["production_access"]}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_stage11b:
		from stockguidance.performance.stage11_integration import Stage11Error, validate_prerequisites
		from stockguidance.performance.stage11_recovery import run_stage11b

		if not arguments.stage11_cutoff or not arguments.stage11_disposable_schema:
			parser().error("--run-stage11b requires --stage11-cutoff and --stage11-disposable-schema")
		try:
			cutoff = datetime.fromisoformat(arguments.stage11_cutoff)
			context = validate_prerequisites(_resolve(arguments.stage11_decision), _resolve(arguments.manifest), _resolve(arguments.audit_report), _resolve(arguments.stage11_evidence_path), cutoff)
			report = run_stage11b(context, _resolve(arguments.stage11a_report), _resolve(arguments.dbinfo_file), arguments.stage11_disposable_schema, arguments.stage11b_session, _resolve(arguments.stage11b_output), _resolve(arguments.stage11b_checkpoint_directory))
		except (OSError, Stage11Error, ValueError) as error:
			print(json.dumps({"successful": False, "error": "Stage 11b recovery proof failed", "detail": str(error)}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "operation": "run_stage11b", "report": str(_resolve(arguments.stage11b_output)), "status": report["status"], "production_access": report["production_access"]}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_stage12:
		from stockguidance.performance.stage12 import Stage12Error, run_stage12, validate_prerequisites

		try:
			context = validate_prerequisites(_resolve(arguments.stage10_output), _resolve(arguments.manifest), _resolve(arguments.audit_report), _resolve(arguments.stage12_stage11b_report), _resolve(arguments.precision_report), _resolve(arguments.stage12_backup))
			report = run_stage12(context, _resolve(arguments.dbinfo_file), _resolve(arguments.data_root), arguments.stage12_restore_schema, _resolve(arguments.stage12_output), zscore_sample_count=arguments.stage12_zscore_sample_count, java_timeout=arguments.stage12_java_timeout_seconds, reuse_restore=arguments.stage12_reuse_restored_schema)
		except (OSError, Stage12Error, ValueError) as error:
			print(json.dumps({"successful": False, "error": "Stage 12 restore, migration or pilot failed", "detail": str(error)}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "operation": "run_stage12", "report": str(_resolve(arguments.stage12_output)), "status": report["status"], "production_ready": report["production_ready"]}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_stage13:
		from stockguidance.performance.stage13 import Stage13Error, run_stage13, validate_prerequisites

		try:
			context = validate_prerequisites(_resolve(arguments.stage10_output), _resolve(arguments.manifest), _resolve(arguments.audit_report), _resolve(arguments.precision_report), _resolve(arguments.stage13_stage11b_report), _resolve(arguments.stage13_stage12_report), _resolve(arguments.stage11_evidence_path))
			report = run_stage13(context, _resolve(arguments.dbinfo_file), _resolve(arguments.data_root), _resolve(arguments.stage13_output), chunk_size=arguments.stage13_chunk_size, java_timeout=arguments.stage13_java_timeout_seconds, resume=not arguments.stage13_no_resume)
		except (OSError, Stage13Error, TypeError, ValueError) as error:
			print(json.dumps({"successful": False, "error": "Stage 13 population failed", "detail": str(error)}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "operation": "run_stage13", "report": str(_resolve(arguments.stage13_output)), "status": report["status"], "coverage": report.get("coverage")}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_stage14:
		from stockguidance.performance.stage14 import Stage14Error, run_stage14, validate_prerequisites

		try:
			context = validate_prerequisites(_resolve(arguments.stage10_output), _resolve(arguments.stage14_stage12_report), _resolve(arguments.stage14_stage13_report), _resolve(arguments.stage14_stage13_manifest), _resolve(arguments.stage14_snapshot))
			report = run_stage14(context, _resolve(arguments.dbinfo_file), _resolve(arguments.stage14_output), chunk_size=arguments.stage14_chunk_size, java_timeout=arguments.stage14_java_timeout_seconds, resume=not arguments.stage14_no_resume, audit_limit=arguments.stage14_audit_limit, audit_seed=arguments.stage14_audit_seed, max_chunks=arguments.stage14_max_chunks)
		except (OSError, Stage14Error, TypeError, ValueError) as error:
			print(json.dumps({"successful": False, "error": "Stage 14 z-score population failed", "detail": str(error)}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "operation": "run_stage14", "report": str(_resolve(arguments.stage14_output)), "status": report["status"], "coverage": report.get("coverage")}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_stage15:
		from stockguidance.performance.stage15 import Stage15Error, run_stage15, validate_prerequisites

		try:
			context = validate_prerequisites(_resolve(arguments.stage10_output), _resolve(arguments.stage15_stage12_report), _resolve(arguments.stage15_stage13_report), _resolve(arguments.stage15_stage13_manifest), _resolve(arguments.stage15_stage14_report), _resolve(arguments.stage15_stage14_manifest), _resolve(arguments.stage15_snapshot))
			report = run_stage15(context, _resolve(arguments.dbinfo_file), _resolve(arguments.data_root), _resolve(arguments.stage15_output), zscore_batch_size=arguments.stage15_zscore_batch_size, resume=not arguments.stage15_no_resume, workers=arguments.stage15_workers, fetch_size=arguments.stage15_fetch_size, sessions_per_batch=arguments.stage15_sessions_per_batch, cache_slots=arguments.stage15_cache_slots, max_batches=arguments.stage15_max_batches, benchmark_only=arguments.stage15_benchmark, progress_seconds=arguments.stage15_progress_seconds)
		except (OSError, Stage15Error, TypeError, ValueError, RuntimeError) as error:
			print(json.dumps({"successful": False, "error": "Stage 15 final audit failed", "detail": str(error)}))
			return 1
		report_path = _resolve(arguments.stage15_output).with_name("stage15-benchmark.json") if arguments.stage15_benchmark else _resolve(arguments.stage15_output)
		successful = report["status"] in {"partial", "passed"} or report["status"] == "complete" and report.get("acceptance", {}).get("audit_passed") is True
		print(json.dumps({"successful": successful, "operation": "run_stage15", "report": str(report_path), "status": report["status"], "acceptance": report.get("acceptance")}))
		return 0 if successful else 2 if report["status"] == "complete" else 1
	if arguments.run_java_writer_trial:
		from stockguidance.performance.java_writer_trial import run_trial

		if not arguments.disposable_schema:
			parser().error("--run-java-writer-trial requires --disposable-schema")
		report = run_trial(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), arguments.disposable_schema, _resolve(arguments.output))
		print(json.dumps({"successful": report["status"] == "complete", "report": str(_resolve(arguments.output)), "status": report["status"]}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_java_zscore_trial:
		from stockguidance.performance.java_zscore_trial import run_trial

		if not arguments.disposable_schema:
			parser().error("--run-java-zscore-trial requires --disposable-schema")
		try:
			report = run_trial(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), arguments.disposable_schema, _resolve(arguments.output))
		except Exception:
			print(json.dumps({"successful": False, "error": "Stage 7b Java z-score trial failed"}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "report": str(_resolve(arguments.output)), "status": report["status"]}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_sql_raw_trial:
		from stockguidance.performance.sql_raw_candidate import run_trial

		if not arguments.disposable_schema:
			parser().error("--run-sql-raw-trial requires --disposable-schema")
		try:
			report = run_trial(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), arguments.disposable_schema, _resolve(arguments.output))
		except Exception:
			print(json.dumps({"successful": False, "error": "Stage 8a SQL raw trial failed"}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "report": str(_resolve(arguments.output)), "status": report["status"]}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_sql_derived_trial:
		from stockguidance.performance.sql_derived_candidate import run_trial

		if not arguments.disposable_schema:
			parser().error("--run-sql-derived-trial requires --disposable-schema")
		try:
			report = run_trial(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), arguments.disposable_schema, _resolve(arguments.output))
		except Exception:
			print(json.dumps({"successful": False, "error": "Stage 8b SQL derived trial failed"}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "report": str(_resolve(arguments.output)), "status": report["status"]}))
		return 0 if report["status"] == "complete" else 1
	if arguments.run_sql_zscore_trial:
		from stockguidance.performance.sql_zscore_candidate import run_trial

		if not arguments.disposable_schema:
			parser().error("--run-sql-zscore-trial requires --disposable-schema")
		try:
			report = run_trial(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), arguments.disposable_schema, _resolve(arguments.output))
		except Exception:
			print(json.dumps({"successful": False, "error": "Stage 8c SQL z-score trial failed"}))
			return 1
		print(json.dumps({"successful": report["status"] == "complete", "report": str(_resolve(arguments.output)), "status": report["status"]}))
		return 0 if report["status"] == "complete" else 1
	if arguments.build_manifest:
		manifest = build_manifest(
			ManifestPaths(
				_resolve(arguments.data_root),
				_resolve(arguments.evaluation_plan),
				_resolve(arguments.precision_report),
				_resolve(arguments.audit_report),
				_resolve(arguments.dbinfo_file) if arguments.dbinfo_file else None,
			),
			_resolve(arguments.manifest),
			calendar_id=arguments.calendar_id,
		)
		print(json.dumps({"successful": True, "operation": "build_manifest", "manifest": str(_resolve(arguments.manifest)), "manifest_id": manifest["manifest_id"]}))
		return 0
	if arguments.aggregate:
		reports = tuple(_resolve(path) for path in arguments.report)
		try:
			aggregate = aggregate_candidate_reports(reports, _resolve(arguments.aggregate_output))
		except Exception:
			print(json.dumps({"successful": False, "error": "Stage 6 candidate aggregation failed"}))
			return 1
		print(json.dumps({"successful": True, "operation": "aggregate", "report": str(_resolve(arguments.aggregate_output)), "status": aggregate["status"]}))
		return 0
	workloads = tuple(item for item in (value.strip() for value in arguments.workloads.split(",")) if item)
	if not workloads or any(item not in WORKLOADS for item in workloads):
		print(json.dumps({"successful": False, "error": "Unknown Stage 6 workload"}))
		return 2
	try:
		if arguments.storage == "mysql":
			from stockguidance.performance.mysql_candidates import run_mysql_baselines

			if not arguments.disposable_schema:
				parser().error("MySQL candidate trials require --disposable-schema")
			report = run_mysql_baselines(_resolve(arguments.manifest), _resolve(arguments.dbinfo_file), arguments.disposable_schema, _resolve(arguments.output), workloads, repetitions=arguments.repetitions, timeout_seconds=arguments.timeout_seconds)
		else:
			report = run_python_baselines(_resolve(arguments.manifest), _resolve(arguments.output), _resolve(arguments.target), workloads, repetitions=arguments.repetitions, timeout_seconds=arguments.timeout_seconds)
	except Exception:
		print(json.dumps({"successful": False, "error": "Stage 6 Python baseline failed"}))
		return 1
	print(json.dumps({"successful": True, "operation": "run_python", "report": str(_resolve(arguments.output)), "status": report["status"]}))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
