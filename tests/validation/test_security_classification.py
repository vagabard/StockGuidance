from datetime import date
import json

from stockguidance.market_data.securities import security_metadata, is_test_security


def test_verified_test_symbols_are_excluded_without_name_guessing(tmp_path):
	metadata = security_metadata(tmp_path)
	assert is_test_security(metadata, "NTEST", date(2024, 7, 30))
	assert is_test_security(metadata, "ZVZZT", date(2024, 7, 30))
	assert is_test_security(metadata, "MTEST.A", date(2026, 5, 7))
	assert is_test_security(metadata, "NTEST.H", date(2026, 5, 7))
	assert is_test_security(metadata, "ZXZZT", date(2026, 5, 7))
	assert not is_test_security(metadata, "NTEST", date(2010, 1, 1))
	assert not is_test_security(metadata, "MYTEST", date(2024, 7, 30))
	assert metadata["NTEST"]["evidence"].startswith("https://www.nyse.com/")


def test_security_metadata_has_effective_dates_and_retains_raw_files(tmp_path):
	root = tmp_path / "market_summary"
	root.mkdir()
	(root / "securities.json").write_text(json.dumps({"ABC": {"is_test": True, "exchange": "XNAS", "asset_type": "test_security", "effective_from": "2024-07-01", "effective_to": "2024-07-31", "evidence": "verified fixture"}}))
	metadata = security_metadata(tmp_path)
	assert not is_test_security(metadata, "ABC", date(2024, 6, 28))
	assert is_test_security(metadata, "ABC", date(2024, 7, 15))
	assert not is_test_security(metadata, "ABC", date(2024, 8, 1))
