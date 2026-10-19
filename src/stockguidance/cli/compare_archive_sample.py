import argparse
from dataclasses import fields
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any

from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.performance.archive_read import ArchiveBar, archive_records_id, read_archive, read_database, streaming_connection


def compare_sample(acceptance_file: Path, database_config: Path) -> dict[str, Any]:
	acceptance = json.loads(acceptance_file.read_text(encoding="utf-8"))
	samples = acceptance["samples"]
	if acceptance.get("schema_version") != 1 or acceptance.get("source_accepted") is not True or len(samples) != 9 or len({sample["session"] for sample in samples}) != 9 or any(sample.get("passed") is not True for sample in samples):
		raise ValueError("Nine accepted distinct source files are required")
	connection = streaming_connection(database_config)
	results: list[dict[str, Any]] = []
	try:
		cursor = connection.cursor()
		try:
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			cursor.execute("START TRANSACTION READ ONLY")
		finally:
			cursor.close()
		for sample in samples:
			path = Path(sample["path"])
			if path.stem != sample["session"]:
				raise ValueError("Accepted path and session disagree")
			bars, digest = read_archive(path)
			if digest != sample["file_sha256"] or archive_records_id(bars) != sample["records_id"]:
				raise ValueError("Accepted archive content changed")
			stored = read_database(connection, database_config, date.fromisoformat(sample["session"]))
			expected_by_ticker = {bar.ticker: bar for bar in bars}
			actual_by_ticker = {bar.ticker: bar for bar in stored}
			names = [field.name for field in fields(ArchiveBar) if field.name != "ticker"]
			counts = dict.fromkeys(names, 0)
			examples = []
			differing_rows = 0
			for ticker in sorted(expected_by_ticker.keys() & actual_by_ticker.keys()):
				expected, actual = expected_by_ticker[ticker], actual_by_ticker[ticker]
				differences = {name: {"archive": None if getattr(expected, name) is None else str(getattr(expected, name)), "database": None if getattr(actual, name) is None else str(getattr(actual, name))} for name in names if getattr(expected, name) != getattr(actual, name)}
				if differences:
					differing_rows += 1
					for name in differences:
						counts[name] += 1
					if len(examples) < 20:
						examples.append({"ticker": ticker, "fields": differences})
			missing = sorted(expected_by_ticker.keys() - actual_by_ticker.keys())
			extra = sorted(actual_by_ticker.keys() - expected_by_ticker.keys())
			results.append({"session": sample["session"], "file_sha256": digest, "archive_count": len(bars), "database_count": len(stored), "archive_id": archive_records_id(bars), "database_id": archive_records_id(stored), "matches": not missing and not extra and differing_rows == 0, "missing_count": len(missing), "extra_count": len(extra), "missing_examples": missing[:20], "extra_examples": extra[:20], "differing_row_count": differing_rows, "field_difference_counts": counts, "difference_examples": examples, "omitted_difference_examples": max(0, differing_rows - len(examples))})
	finally:
		try:
			connection.rollback()
		finally:
			connection.close()
	return {"schema_version": 1, "operation": "compare_accepted_archive_sample", "checked_at": datetime.now(timezone.utc).isoformat(), "source_identity": acceptance["source_identity"], "provenance_kind": acceptance["provenance_kind"], "source_acceptance_file": str(acceptance_file), "read_only": True, "scope": "Nine accepted archive sessions only; global orphans and dates outside the sample are not assessed", "matches": all(result["matches"] for result in results), "import_receipts_created": False, "sessions": results}


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Read-only field comparison of nine accepted archive files with raw stock tables")
	parser.add_argument("--acceptance-file", type=Path, default=Path("output/legacy-migration/source-acceptance.json"))
	parser.add_argument("--dbinfo-file", type=Path, default=Path("dbinfo.id"))
	parser.add_argument("--output", type=Path, default=Path("output/legacy-migration/sample-database-comparison.json"))
	arguments = parser.parse_args(argv)
	try:
		report = compare_sample(arguments.acceptance_file, arguments.dbinfo_file)
		atomic_write(arguments.output, encode_json(report))
		print(json.dumps({"matches": report["matches"], "report": str(arguments.output)}))
		return 0 if report["matches"] else 2
	except Exception:
		print(json.dumps({"failed": True, "error": "Accepted archive sample comparison could not complete"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
