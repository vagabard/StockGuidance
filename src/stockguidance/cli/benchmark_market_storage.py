import argparse
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import re
from time import perf_counter
from typing import Any

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.archive_study import execute, stratified_sample
from stockguidance.market_data.archive import archive_content
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.market_data.manifests import decode_response, response_payload
from stockguidance.market_data.providers.contracts import DownloadedSession


def publication_trial(request: dict[str, Any]) -> dict[str, Any]:
	paths = [Path(value) for value in request["paths"]]
	calendar = ExchangeCalendar("XNYS", date.fromisoformat(paths[0].name[:10]).replace(month=1, day=1), date.fromisoformat(paths[-1].name[:10]).replace(month=12, day=31))
	downloads = []
	for path in paths:
		metadata = json.loads(path.read_bytes())
		content = (path.parent / "responses" / f"{metadata['content_id']}.json").read_bytes()
		if sha256(content).hexdigest() != metadata["content_id"]:
			raise ValueError("Benchmark input changed")
		downloads.append(DownloadedSession(calendar.session(date.fromisoformat(metadata["session_date"])), decode_response(json.loads(content)), datetime.fromisoformat(metadata["available_at"])))
	with tempfile.TemporaryDirectory(prefix="stockguidance-publication-") as temporary:
		started = perf_counter()
		results = []
		for download in downloads:
			if request["candidate"] == "legacy-envelope-adapted":
				envelope = encode_json(response_payload(download.response))
				response = decode_response(json.loads(envelope))
				download = DownloadedSession(download.session, response, download.available_at)
			content = archive_content(download)
			path = Path(temporary) / str(download.session.session_date.year) / f"{download.session.session_date}.ndjson"
			atomic_write(path, content)
			results.append({"date": str(download.session.session_date), "sha256": sha256(content).hexdigest(), "bytes": len(content)})
		seconds = perf_counter() - started
	return {"seconds": seconds, "digest": sha256(encode_json(results)).hexdigest(), "files": len(results), "bytes": sum(item["bytes"] for item in results)}


def benchmark(data_root: Path, output: Path) -> dict[str, Any]:
	paths = tuple(sorted((data_root / "market_data").glob("*/*.manifest.json")))
	selected = stratified_sample(paths, 24, 20261019)
	for day in ("2024-07-30", "2025-06-18", "2026-05-07"):
		selected.extend(path for path in paths if path.name.startswith(day) and path not in selected)
	selected.sort()
	output.mkdir(parents=True, exist_ok=True)
	report: dict[str, Any] = {"schema_version": 1, "complete": False, "paths": [str(path.resolve()) for path in selected],
		"protocol": "Captured responses; exact same canonical output; atomic replacement and fsync included; preparation and scratch cleanup excluded; candidate order alternates; first invocation is not guaranteed cache cold", "trials": []}
	for repetition in range(4):
		order = ("direct-ndjson", "legacy-envelope-adapted") if repetition % 2 == 0 else ("legacy-envelope-adapted", "direct-ndjson")
		for candidate in order:
			request = {"paths": report["paths"], "candidate": candidate}
			request_path = output / f"request-{candidate}.json"
			atomic_write(request_path, encode_json(request))
			trial = execute([sys.executable, "-m", "stockguidance.cli.benchmark_market_storage", "--execute-request", str(request_path)], dict(os.environ), 600)
			trial.update(candidate=candidate, repetition=repetition)
			report["trials"].append(trial)
			atomic_write(output / "publication.json", encode_json(report))
	digests = {trial["result"]["digest"] for trial in report["trials"]}
	report["parity"] = len(digests) == 1
	report["candidates"] = {candidate: {"warm_median_seconds": statistics.median(trial["result"]["seconds"] for trial in report["trials"] if trial["candidate"] == candidate and trial["repetition"] > 0),
		"startup_inclusive_warm_median_seconds": statistics.median(trial["process_seconds"] for trial in report["trials"] if trial["candidate"] == candidate and trial["repetition"] > 0)} for candidate in ("direct-ndjson", "legacy-envelope-adapted")}
	fastest = min(report["candidates"], key=lambda candidate: report["candidates"][candidate]["warm_median_seconds"])
	if report["candidates"]["direct-ndjson"]["warm_median_seconds"] <= report["candidates"][fastest]["warm_median_seconds"] * 1.05:
		fastest = "direct-ndjson"
	report.update(complete=True, selected=fastest if report["parity"] else None)
	atomic_write(output / "publication.json", encode_json(report))
	return report


def select_implementations(root: Path) -> dict[str, Any]:
	readers = json.loads((root / "readers/study.json").read_bytes())
	publication = json.loads((root / "publication/publication.json").read_bytes())
	database = json.loads((root / "database/stage9a-raw.json").read_bytes())
	if not readers.get("complete") or not readers.get("parity") or not publication.get("complete") or not publication.get("parity") or database.get("status") != "complete":
		raise ValueError("Completed parity-checked reader, publication and database reports are required")
	candidates = database["configurations"]
	if any(candidate["status"] != "measured" or any(len(scenario["correctness"]) != 4 or any(item["status"] != "passed" for item in scenario["correctness"]) for scenario in candidate["scenarios"].values()) for candidate in candidates.values()):
		raise ValueError("Database candidates have incomplete correctness evidence")
	fastest = min(candidates, key=lambda name: candidates[name]["warm_median_seconds"])
	selected = "java-p1" if candidates["java-p1"]["warm_median_seconds"] <= candidates[fastest]["warm_median_seconds"] * 1.05 else fastest
	reader = min(readers["runs"], key=lambda item: item["warm_median_seconds"])
	report = {"schema_version": 1, "correctness_qualified": True, "archive_writer": publication["selected"],
		"fastest_isolated_reader": f"{reader['backend']}-p{reader['workers']}", "operational_raw_pipeline": selected,
		"selection_policy": "Lowest correctness-qualified warm median; keep existing Java single-writer pipeline within 5%",
		"operational_defaults_match_selection": selected == "java-p1" and publication["selected"] == "direct-ndjson",
		"limitations": ["The host was not exclusively reserved during initial trials; timings are local indicative measurements", "Process-tree memory monitoring requires optional psutil, which was unavailable", "Archive-read and database-pipeline timings measure different work and must not be combined into a fabricated total"],
		"disposable_schemas": [value["schema"] for value in candidates.values()],
		"database_warm_seconds": {name: item["warm_median_seconds"] for name, item in candidates.items()},
		"publication_warm_seconds": {name: item["warm_median_seconds"] for name, item in publication["candidates"].items()},
		"reader_warm_seconds": {f"{item['backend']}-p{item['workers']}": item["warm_median_seconds"] for item in readers["runs"]}}
	atomic_write(root / "selection.json", encode_json(report))
	lines = ["# Market storage benchmark selection", "", f"Correctness-qualified: {report['correctness_qualified']}",
		f"Archive publication: {report['archive_writer']}; operational raw pipeline: {selected}.", "",
		"| Workload | Candidate | Warm median seconds |", "|---|---|---:|"]
	for workload, key in (("Archive reader", "reader_warm_seconds"), ("Canonical publication", "publication_warm_seconds"), ("Database publication scenarios", "database_warm_seconds")):
		lines.extend(f"| {workload} | {name} | {seconds:.3f} |" for name, seconds in report[key].items())
	lines.extend(["", *report["limitations"], "", "All database writes used the disposable schemas listed in selection.json. Production archives and database adjustment settings were not migrated."])
	atomic_write(root / "summary.md", "\n".join(lines).encode("utf-8"))
	return report


def cleanup_disposable_databases(root: Path, dbinfo_file: Path) -> dict[str, Any]:
	from stockguidance.database.configuration import load_database_configuration
	from stockguidance.database.connections import MySQLConnectionFactory
	from stockguidance.performance.mysql_candidates import MySQLTarget, load_manifest
	from stockguidance.performance.sql_raw_candidate import SQLRawTarget

	report = json.loads((root / "database/stage9a-raw.json").read_bytes())
	if report["status"] != "complete":
		raise ValueError("Only a completed benchmark may be cleaned up")
	configuration = load_database_configuration(dbinfo_file)
	manifest = load_manifest(Path(report["manifest"]["path"]))
	schemas = []
	for candidate in report["configurations"].values():
		schema = candidate["schema"]
		if not re.fullmatch(r"stockguidance_disposable_s9a_[jps][13]_[0-9a-f]{8}", schema) or schema.casefold() == configuration.database.casefold():
			raise ValueError("Cleanup target is not a benchmark-owned disposable schema")
		target = (SQLRawTarget if candidate["backend"] == "sql" else MySQLTarget)(dbinfo_file, schema, manifest)
		connection = target.connect()
		try:
			target.validate(connection)
		finally:
			connection.close()
		schemas.append(schema)
	connection = MySQLConnectionFactory()(dbinfo_file)
	try:
		cursor = connection.cursor()
		try:
			for schema in schemas:
				cursor.execute(f"DROP DATABASE `{schema}`", ())
		finally:
			cursor.close()
	finally:
		connection.close()
	result = {"removed_disposable_schemas": schemas, "production_schema_changed": False, "reports_retained": True}
	atomic_write(root / "cleanup.json", encode_json(result))
	return result


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Benchmark captured-response publication into the canonical NDJSON format")
	parser.add_argument("--data-root", type=Path, default=Path("data"))
	parser.add_argument("--output", type=Path, default=Path("output/storage-consolidation/benchmark/publication"))
	parser.add_argument("--execute-request", type=Path)
	parser.add_argument("--select-from", type=Path, help="Select implementations from completed reader/publication/database reports")
	parser.add_argument("--cleanup-database", type=Path, help="Remove only the validated disposable schemas belonging to this completed benchmark directory")
	parser.add_argument("--dbinfo-file", type=Path, default=Path("dbinfo.id"))
	arguments = parser.parse_args(argv)
	if arguments.cleanup_database is not None:
		print(json.dumps(cleanup_disposable_databases(arguments.cleanup_database, arguments.dbinfo_file)))
		return 0
	if arguments.select_from is not None:
		print(json.dumps(select_implementations(arguments.select_from)))
		return 0
	if arguments.execute_request is not None:
		print(json.dumps(publication_trial(json.loads(arguments.execute_request.read_bytes()))))
		return 0
	report = benchmark(arguments.data_root, arguments.output)
	print(json.dumps({"complete": report["complete"], "parity": report["parity"], "selected": report["selected"], "candidates": report["candidates"]}))
	return 0 if report["parity"] else 1


if __name__ == "__main__":
	raise SystemExit(main())
