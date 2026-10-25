from datetime import date
import json
from pathlib import Path
from typing import Any


_NYSE_TEST_NOTICE = "https://www.nyse.com/publicdocs/ctaplan/notifications/trader-update/CTS_CQS%20%20NEW%20DEDICATED%20TEST%20SYMBOLS_06022017.pdf"
_NASDAQ_TEST_NOTICE = "https://nasdaqtrader.com/MicroNews.aspx?id=ERA2016-5"


def security_metadata(data_root: Path) -> dict[str, Any]:
	result = {ticker: {"exchange": exchange, "asset_type": "test_security", "is_test": True,
		"effective_from": "2017-08-28", "evidence": _NYSE_TEST_NOTICE, "allow_suffixes": True}
		for ticker, exchange in (("ATEST", "XASE"), ("CBO", "XNYS"), ("CBX", "XNYS"), ("IBO", "XASE"), ("IGZ", "ARCX"), ("NTEST", "XNYS"), ("PTEST", "ARCX"), ("ZVV", "ARCX"), ("ZZK", "ARCX"), ("ZZZ", "ARCX"), ("ZBZX", "BATS"), ("ZTEST", "BATS"), ("ZTST", "BATS"), ("ZIEXT", "IEXG"), ("ZEXIT", "IEXG"), ("ZXIET", "IEXG"))}
	result["ZVZZT"] = {"exchange": "XNAS", "asset_type": "test_security", "is_test": True, "effective_from": "2016-05-25", "evidence": _NASDAQ_TEST_NOTICE}
	for ticker in ("ZXZZT", "ZWZZT"):
		result[ticker] = {"exchange": "XNAS", "asset_type": "test_security", "is_test": True, "effective_from": "2006-07-24", "evidence": "https://www.nasdaqtrader.com/TraderNews.aspx?id=hta2006-109"}
	for ticker, exchange in (("MTEST", None), ("CTEST", None), ("ZAZZT", "XNAS"), ("ZBZZT", "XNAS"), ("ZCZZT", "XNAS"), ("ZJZZT", "XNAS")):
		result[ticker] = {"exchange": exchange, "asset_type": "test_security", "is_test": True, "effective_from": "2024-01-29",
			"evidence": "https://cdn.opraplan.com/documents/notices/OPRA_Intraday_Current_Test_Symbol_012924.pdf",
			"allow_suffixes": ticker in {"MTEST", "CTEST"}, "suffix_evidence": _NYSE_TEST_NOTICE}
	path = data_root / "market_summary" / "securities.json"
	if path.exists():
		payload = json.loads(path.read_text(encoding="utf-8"))
		if not isinstance(payload, dict):
			raise ValueError("Security metadata must be keyed by ticker")
		for ticker, metadata in payload.items():
			if not isinstance(metadata, dict) or type(metadata.get("is_test")) is not bool or not metadata.get("evidence"):
				raise ValueError("Security classification requires an explicit test flag and evidence")
			date.fromisoformat(metadata["effective_from"])
			if metadata.get("effective_to") is not None:
				date.fromisoformat(metadata["effective_to"])
			result[ticker] = metadata
	return result


def is_test_security(metadata: dict[str, Any], ticker: str, session_date: date) -> bool:
	entry = classification(metadata, ticker, session_date)
	return entry.get("is_test") is True


def classification(metadata: dict[str, Any], ticker: str, session_date: date) -> dict[str, Any]:
	entry = metadata.get(ticker)
	if entry is None and "." in ticker:
		base, suffix = ticker.split(".", 1)
		candidate = metadata.get(base, {})
		if candidate.get("allow_suffixes") is True and suffix.isalnum():
			entry = candidate
	if entry is None or (entry.get("effective_from") is not None and date.fromisoformat(entry["effective_from"]) > session_date) or (entry.get("effective_to") is not None and date.fromisoformat(entry["effective_to"]) < session_date):
		return {}
	return dict(entry)
