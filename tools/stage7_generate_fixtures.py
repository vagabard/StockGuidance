from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.interchange import canonical_json_bytes, canonical_sha256
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.intervals import ObservationInterval


def utc_text(value: datetime) -> str:
	return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_fixture() -> dict[str, object]:
	source = SourceIdentity(
		provider="polygon",
		endpoint="aggregates",
		market="us_equities",
		universe="stage7-fixture",
		adjustment=PriceAdjustment.ADJUSTED,
		interval=ObservationInterval.DAILY,
	)
	availability = AvailabilityPolicy.STRICT.value
	return {
		"schema_version": 1,
		"source": {
			"provider": source.provider,
			"endpoint": source.endpoint,
			"market": source.market,
			"universe": source.universe,
			"adjustment": source.adjustment.value,
			"interval": source.interval.value,
		},
		"calendar_id": "XNYS",
		"calendar_version": "fixture-2026",
		"ticker_ids": [7, 2, 11],
		"sessions": [
			{"exchange_date": "2024-07-03", "close_at": "2024-07-03T20:00:00Z"},
			{"exchange_date": "2024-07-05", "close_at": "2024-07-05T20:00:00Z"},
		],
		"coverage": {
			"observed_dates": ["2024-07-03", "2024-07-05"],
			"verified_dates": ["2024-07-03"],
			"holes": [],
			"provenance": "python-fixture-source-manifest",
			"provider_state": "verified",
		},
		"observations": [
			{
				"exchange_date": "2024-07-03",
				"ticker_id": 7,
				"value": Decimal("-1.25e2"),
				"receipt": {
					"observed_at": "2024-07-03T20:00:00Z",
					"available_at": "2024-07-03T20:05:00Z",
					"content_id": "0" * 64,
				},
				"availability_policy": availability,
				"revision": "revision-1",
			},
			{
				"exchange_date": "2024-07-05",
				"ticker_id": 2,
				"value": None,
				"receipt": {
					"observed_at": "2024-07-05T20:00:00Z",
					"available_at": "2024-07-05T20:10:00Z",
					"content_id": "1" * 64,
				},
				"availability_policy": availability,
				"revision": None,
			},
		],
		"errors": [
			{"code": "late_receipt", "message": "Receipt is after the decision cutoff", "exchange_date": None},
		],
		"findings": [
			{"category": "missing", "check": "source_manifest", "detail": "Ticker evidence is absent", "session_date": "2024-07-05", "ticker_ids": [11]},
		],
	}


def main() -> None:
	fixture = build_fixture()
	output = PROJECT_ROOT / "StockGuidance" / "java" / "fixtures" / "interchange-v1.json"
	output.parent.mkdir(parents=True, exist_ok=True)
	output.write_bytes(canonical_json_bytes(fixture))
	print(canonical_sha256(fixture))


if __name__ == "__main__":
	main()
