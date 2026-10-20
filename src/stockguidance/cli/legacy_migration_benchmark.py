from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

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
	arguments.add_argument("--data-root", type=_path, default=Path("data"))
	arguments.add_argument("--dbinfo-file", type=_path, default=Path("dbinfo.id"))
	arguments.add_argument("--evaluation-plan", type=_path, default=Path("output/legacy-migration/evaluation-plan.json"))
	arguments.add_argument("--precision-report", type=_path, default=Path("output/legacy-migration/precision-inventory.json"))
	arguments.add_argument("--audit-report", type=_path, default=Path("output/legacy-migration/audit.json"))
	arguments.add_argument("--manifest", type=_path, default=Path("output/legacy-migration/benchmark/candidates/manifest.json"))
	arguments.add_argument("--output", type=_path, default=Path("output/legacy-migration/benchmark/candidates/python-baselines.json"))
	arguments.add_argument("--target", type=_path, default=Path("output/legacy-migration/benchmark/candidates/python-disposable.sqlite"), help="Offline database file, used only with --storage sqlite")
	arguments.add_argument("--storage", choices=("mysql", "sqlite"), default="mysql")
	arguments.add_argument("--disposable-schema")
	arguments.add_argument("--workloads", default=",".join(WORKLOADS))
	arguments.add_argument("--repetitions", type=int, default=4)
	arguments.add_argument("--timeout-seconds", type=float, default=3600.0)
	arguments.add_argument("--report", type=_path, action="append", default=[])
	arguments.add_argument("--aggregate-output", type=_path, default=Path("output/legacy-migration/benchmark/candidates/study.json"))
	return arguments


def _resolve(path: Path) -> Path:
	return path.resolve()


def main(argv: Sequence[str] | None = None) -> int:
	arguments = parser().parse_args(argv)
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
