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


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Check nine deterministic random retained Massive files without database access")
	parser.add_argument("--data-root", default="data")
	parser.add_argument("--attestation", required=True)
	parser.add_argument("--output", type=Path, default=Path("output/legacy-migration/source-acceptance.json"))
	arguments = parser.parse_args(argv)
	try:
		report = verify_sample(resolve_cli_path(arguments.data_root, "data"), arguments.attestation)
		atomic_write(arguments.output, encode_json(report))
		print(json.dumps({"source_accepted": report["source_accepted"], "report": str(arguments.output)}))
		return 0 if report["source_accepted"] else 2
	except Exception:
		print(json.dumps({"failed": True, "error": "Archive source sampling could not complete"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
