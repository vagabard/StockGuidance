from datetime import date
from decimal import Decimal
import json

from stockguidance.config.source import DataConfiguration, VerificationMode, load_data_configuration
from stockguidance.database.verification import compare_daily_bars, sample_dates, verification_allows
from stockguidance.market_data.records import DailyBar


def configuration(**changes):
	values = {
		"source_family": "polygon",
		"endpoint": "grouped-daily",
		"market": "us-stocks",
		"universe": "all-us-stocks",
		"raw_price_split_adjusted": True,
		"raw_price_dividend_adjusted": False,
	}
	values.update(changes)
	return DataConfiguration(**values)


def bar(close: str = "10.0000") -> DailyBar:
	price = Decimal(close)
	return DailyBar("AAA", price, price, price, price, 100, price, 5)


def test_source_family_aliases_and_endpoint_changes_share_semantic_key():
	from stockguidance.market_data.identity import data_configuration_key

	assert configuration(source_family="polygon").source_family == "polygon_massive"
	assert data_configuration_key(configuration(source_family="polygon")) == data_configuration_key(configuration(source_family="massive"))
	assert data_configuration_key(configuration(endpoint="java-client")) == data_configuration_key(configuration())


def test_configuration_loader_reads_adjustment_policy_and_verification_policy(tmp_path):
	path = tmp_path / "data-configuration.json"
	path.write_text(json.dumps({
		"schema_version": 1,
		"source_family": "massive",
		"endpoint": "grouped-daily",
		"market": "us-stocks",
		"universe": "all-us-stocks",
		"calendar_id": "XNAS",
		"interval": "1d",
		"raw_price_split_adjusted": True,
		"raw_price_dividend_adjusted": True,
		"raw_price_vendor_defined_adjustment": "massive-v2",
		"verification_policy": {
			"source_family_mismatch": "gate_writes",
			"endpoint_mismatch": "advisory",
			"source_family_sample_days": 100,
			"endpoint_sample_pulls": 50,
		},
	}), encoding="utf-8")

	loaded = load_data_configuration(path)
	assert loaded.source_family == "polygon_massive"
	assert loaded.raw_price_split_adjusted and loaded.raw_price_dividend_adjusted
	assert loaded.raw_price_vendor_defined_adjustment == "massive-v2"
	assert loaded.verification_policy.source_family_mismatch is VerificationMode.GATE_WRITES
	assert loaded.calendar_id == "XNAS"


def test_calendar_does_not_change_the_semantic_data_configuration():
	from stockguidance.market_data.identity import data_configuration_key

	assert configuration().calendar_id == "XNYS"
	assert configuration(calendar_id="NASDAQ").calendar_id == "XNAS"
	assert data_configuration_key(configuration(calendar_id="XNYS")) == data_configuration_key(configuration(calendar_id="XNAS"))


def test_sample_dates_are_deterministic_and_comparison_is_exact():
	dates = tuple(date(2024, 1, day) for day in range(1, 11))
	assert sample_dates(dates, 4, 17) == sample_dates(dates, 4, 17)
	assert compare_daily_bars((bar(),), (bar(),)).passed
	assert not compare_daily_bars((bar(),), (bar("11.0000"),)).passed


def test_verification_modes_apply_to_pending_state():
	from stockguidance.database.verification import VerificationStatus

	assert not verification_allows(VerificationStatus.PENDING, VerificationMode.BLOCK, False)
	assert verification_allows(VerificationStatus.PENDING, VerificationMode.GATE_WRITES, False)
	assert not verification_allows(VerificationStatus.PENDING, VerificationMode.GATE_WRITES, True)
	assert verification_allows(VerificationStatus.PENDING, VerificationMode.ADVISORY, True)
