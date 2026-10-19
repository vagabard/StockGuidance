import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from stockguidance.cli.paths import project_root, resolve_cli_path
from stockguidance.validation.java_backend import JavaValidationBackendError, selected_table_names_file


def parser() -> argparse.ArgumentParser:
	arguments = argparse.ArgumentParser(description="Read-only Java comparison of legacy NDJSON downloads and database raw rows")
	arguments.add_argument("--data-root", default="data")
	arguments.add_argument("--dbinfo-file", default="dbinfo.id")
	arguments.add_argument("--start-date")
	arguments.add_argument("--end-date")
	arguments.add_argument("--adjustment", choices=("adjusted", "unadjusted"), default="adjusted")
	arguments.add_argument("--java-classpath", help="Java classpath containing the StockGuidance classes and dependencies")
	arguments.add_argument("--java-executable", default="java")
	arguments.add_argument("--format", choices=("text", "json"), default="text")
	return arguments


def _classpath(value: str | None) -> str:
	if value:
		return value
	root = project_root() / "StockGuidance" / "java"
	classes = root / "target" / "classes"
	dependencies = root / "target" / "dependency" / "*"
	connector = root / "lib" / "mysql-connector-j-9.6.0.jar"
	return str(classes) + os.pathsep + str(dependencies) + os.pathsep + str(connector)


def _text_report(payload: dict[str, Any]) -> str:
	scope = payload.get("scope", {})
	findings = payload.get("findings", [])
	lines = [
		f"Download comparison: {'complete' if payload.get('complete') else 'findings'} (exit {payload.get('exit_code', 1)})",
		f"Scope: {scope.get('start')} through {scope.get('end')}",
		f"Downloaded files: {payload.get('download_file_count', 0)}; findings: {payload.get('finding_count', len(findings))}",
	]
	for finding in findings:
		lines.append(f"{finding.get('category')} {finding.get('check')} {finding.get('session_date')} {finding.get('ticker') or ''}: {finding.get('detail')}")
	if payload.get("omitted_finding_count", 0):
		lines.append(f"Additional findings omitted: {payload['omitted_finding_count']}")
	return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
	arguments = parser().parse_args(argv)
	data_root = resolve_cli_path(arguments.data_root, "data")
	dbinfo_file = resolve_cli_path(arguments.dbinfo_file, "dbinfo.id")
	try:
		with selected_table_names_file(dbinfo_file) as table_names_file:
			command = [
				arguments.java_executable,
				"-cp",
				_classpath(arguments.java_classpath),
				"stockguidance.validation.DownloadComparisonMain",
				"--table-names-file", str(table_names_file),
				"--config", str(dbinfo_file),
				"--data-root", str(data_root),
				"--adjustment", arguments.adjustment,
			]
			for name, value in (("--start-date", arguments.start_date), ("--end-date", arguments.end_date)):
				if value is not None:
					command.extend((name, value))
			completed = subprocess.run(command, capture_output=True, text=True, check=False)
		payload = json.loads(completed.stdout)
	except (OSError, ValueError, json.JSONDecodeError, JavaValidationBackendError):
		payload = {"complete": False, "failed": True, "exit_code": 1, "error": "Download comparison backend failed"}
	if arguments.format == "json":
		print(json.dumps(payload, sort_keys=True))
	else:
		print(_text_report(payload))
	return int(payload.get("exit_code", 1))


if __name__ == "__main__":
	raise SystemExit(main())
