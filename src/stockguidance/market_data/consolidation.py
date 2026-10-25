from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import gzip
from pathlib import Path
from typing import Any

from stockguidance.market_data.archive import FIELDS, unique_object
from stockguidance.market_data.securities import classification


def file_id(path: Path) -> str:
	with path.open("rb") as stream:
		return sha256_stream(stream)


def sha256_stream(stream: Any) -> str:
	digest = sha256()
	while chunk := stream.read(1024 * 1024):
		digest.update(chunk)
	return digest.hexdigest()


def archive_rows(path: Path) -> dict[str, dict[str, Any]]:
	rows = {}
	with path.open("rb") as stream:
		for line in stream:
			row = json.loads(line, parse_float=Decimal, object_pairs_hook=unique_object)
			if not isinstance(row, dict) or not isinstance(row.get("T"), str) or row["T"] in rows:
				raise ValueError(f"Invalid or duplicate ticker in {path}")
			rows[row["T"]] = row
	if not rows:
		raise ValueError(f"Empty archive: {path}")
	return rows


def response_rows(path: Path) -> dict[str, dict[str, Any]]:
	payload = json.loads(path.read_bytes(), parse_float=Decimal, object_pairs_hook=unique_object)
	rows = {row["T"]: row for row in payload["records"]}
	if len(rows) != len(payload["records"]):
		raise ValueError("Duplicate response ticker")
	for ticker, values in payload.get("bad_records", []):
		if ticker in rows:
			raise ValueError("Duplicate response ticker")
		rows[ticker] = {"T": ticker, **dict(zip(FIELDS, values))}
	for ticker, timestamp in payload["source_timestamps"].items():
		if ticker in rows:
			rows[ticker]["t"] = timestamp
	return rows


def candidates(data_root: Path) -> dict[str, list[dict[str, Any]]]:
	result: dict[str, list[dict[str, Any]]] = {}
	for location, pattern in (("market_summary", "[0-9][0-9][0-9][0-9]/*.ndjson"), ("daily-maintenance", "*/*.ndjson")):
		for path in sorted((data_root / location).glob(pattern)):
			session_date = date.fromisoformat(path.stem)
			if location == "market_summary" and path.parent.name != str(session_date.year):
				raise ValueError(f"Archive year and date disagree: {path}")
			result.setdefault(path.stem, []).append({"location": location, "path": str(path.resolve()), "adjustment": "unknown", "available_at": None})
	for path in sorted((data_root / "market_data").glob("*/*.manifest.json")):
		payload = json.loads(path.read_bytes())
		session_date = date.fromisoformat(payload["session_date"])
		response = path.parent / "responses" / f"{payload['content_id']}.json"
		result.setdefault(session_date.isoformat(), []).append({"location": "market_data", "path": str(response.resolve()),
			"manifest": str(path.resolve()), "adjustment": payload["source"]["adjustment"], "available_at": payload["available_at"],
			"expected_sha256": payload["content_id"]})
	return result


def normalize(value: Any) -> Any:
	if value is None:
		return None
	if isinstance(value, bool):
		raise ValueError("Boolean archive value")
	return Decimal(str(value))


def conflicts(session: str, baseline: dict[str, Any], candidate: dict[str, Any], securities: dict[str, Any]) -> Iterator[dict[str, Any]]:
	def load(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
		return response_rows(Path(item["path"])) if item["location"] == "market_data" else archive_rows(Path(item["path"]))
	left, right = load(baseline), load(candidate)
	for ticker in sorted(left.keys() | right.keys()):
		metadata = classification(securities, ticker, date.fromisoformat(session))
		common = {"date": session, "ticker": ticker, "baseline": baseline["path"], "candidate": candidate["path"],
			"exchange": metadata.get("exchange"), "asset_type": metadata.get("asset_type"), "is_test": metadata.get("is_test"),
			"classification_evidence": metadata.get("evidence")}
		if ticker not in left or ticker not in right:
			yield {**common, "field": "ticker_presence", "baseline_value": ticker in left, "candidate_value": ticker in right}
			continue
		for field in (*FIELDS, "t"):
			before, after = left[ticker].get(field), right[ticker].get(field)
			if normalize(before) != normalize(after):
				yield {**common, "field": field, "baseline_value": None if before is None else str(before), "candidate_value": None if after is None else str(after)}


def audit(data_root: Path, output: Path, *, securities: dict[str, Any] | None = None) -> dict[str, Any]:
	output.mkdir(parents=True, exist_ok=True)
	inventory = candidates(data_root)
	counts: dict[str, int] = {}
	conflict_counts: dict[str, int] = {}
	errors = []
	with (output / "inventory.jsonl").open("w", encoding="utf-8") as listing, gzip.open(output / "conflicts.jsonl.gz", "wt", encoding="utf-8") as differences:
		for session, copies in sorted(inventory.items()):
			for item in copies:
				counts[item["location"]] = counts.get(item["location"], 0) + 1
				try:
					item["sha256"] = file_id(Path(item["path"]))
					if "expected_sha256" in item and item["sha256"] != item["expected_sha256"]:
						raise ValueError("Legacy response checksum differs from manifest")
					listing.write(json.dumps({"date": session, **item}) + "\n")
				except (OSError, ValueError) as error:
					errors.append({"date": session, "path": item["path"], "error": str(error)})
			baseline = next((item for item in copies if item["location"] == "market_summary"), copies[0])
			for item in copies:
				if item is baseline:
					continue
				try:
					count = 0
					for difference in conflicts(session, baseline, item, securities or {}):
						differences.write(json.dumps(difference) + "\n")
						count += 1
					if count:
						conflict_counts[session] = conflict_counts.get(session, 0) + count
				except (OSError, ValueError, KeyError, TypeError) as error:
					errors.append({"date": session, "path": item["path"], "error": str(error)})
	canonical = {session for session, copies in inventory.items() if any(item["location"] == "market_summary" for item in copies)}
	report = {"schema_version": 1, "counts": counts, "dates": len(inventory), "dates_missing_canonical": sorted(set(inventory) - canonical),
		"conflicting_dates": len(conflict_counts), "field_conflicts": sum(conflict_counts.values()), "conflicts_by_date": conflict_counts,
		"errors": errors, "ready_for_automatic_migration": False,
		"policy": "Only evidenced complete unadjusted sources may be promoted; unknown provenance requires verification. No files were changed."}
	(output / "audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
	return report
