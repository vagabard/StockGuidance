import argparse
from dataclasses import asdict
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.archive_study import stratified_sample
from stockguidance.cli.paths import resolve_cli_path
from stockguidance.config.source import DataConfiguration
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.market_data.providers.polygon import GROUPED_ENDPOINT, GROUPED_UNIVERSE
from stockguidance.performance.archive_read import archive_records_id, inventory, read_archive


def verify_sample(root: Path, attestation: str, seed: int = 20261019) -> dict[str, Any]:
	if not attestation.strip():
		raise ValueError("Operator source attestation is required")
	paths = inventory(root)
	selected = stratified_sample(paths, 9, seed)
	first, last = date.fromisoformat(paths[0].stem), date.fromisoformat(paths[-1].stem)
	calendar = ExchangeCalendar("XNYS", date(first.year, 1, 1), date(last.year, 12, 31))
	sessions = {session.session_date for session in calendar.sessions(first, last)}
	results = []
	for path in selected:
		result: dict[str, Any] = {"path": str(path.resolve()), "session": path.stem}
		try:
			if date.fromisoformat(path.stem) not in sessions:
				raise ValueError("Archive date is not an exchange session")
			bars, digest = read_archive(path)
			result.update(passed=True, file_sha256=digest, records_id=archive_records_id(bars), record_count=len(bars), fractional_volume_count=sum(bar.volume != bar.volume.to_integral_value() for bar in bars), exceeds_legacy_price_range_count=sum(any(value >= 10**15 for value in (bar.open, bar.high, bar.low, bar.close, bar.vwap) if value is not None) for bar in bars))
		except (OSError, ValueError, KeyError, OverflowError):
			result.update(passed=False, finding="Archive file failed content, timestamp, numeric or session validation")
		results.append(result)
	identity = DataConfiguration("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, True, False).to_source_identity()
	return {"schema_version": 1, "operation": "verify_archive_sample", "verified_at": datetime.now(timezone.utc).isoformat(), "seed": seed, "source_identity": asdict(identity), "provenance_kind": "operator_attested_retained_archive", "operator_attestation": attestation, "original_network_received_at": None, "inventory_id": content_id([str(path.relative_to(root)) for path in paths]), "inventory_count": len(paths), "samples": results, "source_accepted": all(result["passed"] for result in results), "all_files_adopted": False, "scope_note": "Passing samples establish provenance under the operator policy; each other file still requires validation. No original receipt time or independent market completeness is established."}


def recheck_sample(acceptance_file: Path, root: Path, java_comparison_file: Path, database_comparison_file: Path | None = None) -> dict[str, Any]:
	acceptance = json.loads(acceptance_file.read_text(encoding="utf-8"))
	if acceptance.get("schema_version") != 1 or acceptance.get("source_accepted") is not True:
		raise ValueError("An accepted source report is required")
	samples = acceptance.get("samples")
	if not isinstance(samples, list) or len(samples) != 9 or len({sample.get("session") for sample in samples}) != 9:
		raise ValueError("Exactly nine distinct accepted samples are required")
	paths = inventory(root)
	current_inventory_id = content_id([str(path.relative_to(root)) for path in paths])
	findings: list[dict[str, Any]] = []
	identity = asdict(DataConfiguration("polygon", GROUPED_ENDPOINT, "us-stocks", GROUPED_UNIVERSE, True, False).to_source_identity())
	if acceptance.get("seed") != 20261019:
		findings.append({"type": "seed_mismatch", "expected": 20261019, "actual": acceptance.get("seed")})
	if acceptance.get("source_identity") != identity:
		findings.append({"type": "source_identity_mismatch", "expected": identity, "actual": acceptance.get("source_identity")})
	if current_inventory_id != acceptance.get("inventory_id"):
		findings.append({"type": "inventory_drift", "expected": acceptance.get("inventory_id"), "actual": current_inventory_id})
	if len(paths) != acceptance.get("inventory_count"):
		findings.append({"type": "inventory_count_drift", "expected": acceptance.get("inventory_count"), "actual": len(paths)})
	first, last = date.fromisoformat(paths[0].stem), date.fromisoformat(paths[-1].stem)
	calendar = ExchangeCalendar("XNYS", date(first.year, 1, 1), date(last.year, 12, 31))
	sessions = {session.session_date for session in calendar.sessions(first, last)}
	inventory_paths = {item.resolve() for item in paths}
	checked: list[dict[str, Any]] = []
	for sample in samples:
		path = Path(sample["path"]).resolve()
		result = {"session": sample.get("session"), "path": str(path), "expected_file_sha256": sample.get("file_sha256"), "expected_records_id": sample.get("records_id"), "expected_record_count": sample.get("record_count")}
		try:
			if path.stem != sample["session"] or path not in inventory_paths:
				raise ValueError("Accepted path is not the retained sample path")
			if date.fromisoformat(path.stem) not in sessions:
				raise ValueError("Archive date is not an exchange session")
			bars, digest = read_archive(path)
			actual_id = archive_records_id(bars)
			result.update(file_sha256=digest, records_id=actual_id, record_count=len(bars), passed=digest == sample["file_sha256"] and actual_id == sample["records_id"] and len(bars) == sample["record_count"])
			if not result["passed"]:
				result["finding"] = "Rechecked archive content differs from the accepted record"
		except (OSError, ValueError, KeyError, OverflowError):
			result.update(passed=False, finding="Accepted archive file failed recheck content, timestamp, numeric or session validation")
		checked.append(result)
	java = json.loads(java_comparison_file.read_text(encoding="utf-8"))
	java_sessions = java.get("measurements", [{}])[0].get("sessions", []) if java.get("measurements") else []
	java_by_session = {item.get("session"): item for item in java_sessions}
	cross_language_findings: list[dict[str, Any]] = []
	if java.get("backend") != "java" or java.get("workers") != 3 or len(java_sessions) != 9:
		cross_language_findings.append({"type": "cross_language_report_shape", "backend": java.get("backend"), "workers": java.get("workers"), "session_count": len(java_sessions)})
	for sample in samples:
		java_item = java_by_session.get(sample["session"])
		if java_item is None or any(java_item.get(name) != sample_value for name, sample_value in (("file_sha256", sample["file_sha256"]), ("archive_id", sample["records_id"]), ("archive_count", sample["record_count"]))):
			cross_language_findings.append({"type": "cross_language_mismatch", "session": sample["session"]})
	database = None
	if database_comparison_file is not None:
		database_report = json.loads(database_comparison_file.read_text(encoding="utf-8"))
		database = {"path": str(database_comparison_file), "read_only": database_report.get("read_only"), "matches": database_report.get("matches"), "import_receipts_created": database_report.get("import_receipts_created"), "scope": database_report.get("scope")}
	return {"schema_version": 1, "operation": "recheck_archive_sample", "checked_at": datetime.now(timezone.utc).isoformat(), "source_acceptance_file": str(acceptance_file), "seed": acceptance.get("seed"), "source_identity": acceptance.get("source_identity"), "inventory_id": current_inventory_id, "inventory_count": len(paths), "sample_count": len(checked), "record_count": sum(item.get("record_count", 0) for item in checked if item.get("passed")), "samples": checked, "cross_language": {"report": str(java_comparison_file), "passed": not cross_language_findings, "findings": cross_language_findings}, "database_comparison": database, "findings": findings, "source_accepted": not findings and all(item["passed"] for item in checked) and not cross_language_findings, "scope_note": "Recheck reused the recorded nine files and seed; it did not resample or establish market-wide completeness."}


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Check nine deterministic random retained Massive files without database access")
	parser.add_argument("--data-root", default="data")
	parser.add_argument("--attestation")
	parser.add_argument("--output", type=Path, default=Path("output/legacy-migration/source-acceptance.json"))
	parser.add_argument("--recheck", action="store_true")
	parser.add_argument("--acceptance-file", type=Path, default=Path("output/legacy-migration/source-acceptance.json"))
	parser.add_argument("--java-comparison-file", type=Path, default=Path("output/legacy-migration/source-java-comparison.json"))
	parser.add_argument("--database-comparison-file", type=Path, default=Path("output/legacy-migration/sample-database-comparison.json"))
	arguments = parser.parse_args(argv)
	try:
		if arguments.recheck:
			acceptance_file = arguments.acceptance_file.resolve()
			recheck = recheck_sample(acceptance_file, resolve_cli_path(arguments.data_root, "data"), arguments.java_comparison_file.resolve(), arguments.database_comparison_file.resolve())
			acceptance = json.loads(acceptance_file.read_text(encoding="utf-8"))
			acceptance["stage4_recheck"] = recheck
			atomic_write(acceptance_file, encode_json(acceptance))
			print(json.dumps({"source_accepted": recheck["source_accepted"], "report": str(acceptance_file), "recheck": str(acceptance_file)}))
			return 0 if recheck["source_accepted"] else 2
		if not arguments.attestation:
			parser.error("--attestation is required unless --recheck is used")
		report = verify_sample(resolve_cli_path(arguments.data_root, "data"), arguments.attestation)
		atomic_write(arguments.output, encode_json(report))
		print(json.dumps({"source_accepted": report["source_accepted"], "report": str(arguments.output)}))
		return 0 if report["source_accepted"] else 2
	except Exception:
		print(json.dumps({"failed": True, "error": "Archive source sampling could not complete"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
