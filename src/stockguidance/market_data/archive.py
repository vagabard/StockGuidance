from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from stockguidance.calendar.sessions import TradingSession
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import encode_json
from stockguidance.market_data.providers.contracts import DownloadedSession, ProviderResponse, ResponseState
from stockguidance.market_data.quality import invalid_fields
from stockguidance.market_data.records import bar_payload, provider_bar


FIELDS = ("o", "h", "l", "c", "v", "vw", "n")


def archive_path(data_root: Path, session_date: date) -> Path:
	return data_root / "market_summary" / str(session_date.year) / f"{session_date.isoformat()}.ndjson"


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
	result: dict[str, object] = {}
	for name, value in pairs:
		if name in result:
			raise ValueError(f"Duplicate archive field: {name}")
		result[name] = value
	return result


def archive_content(download: DownloadedSession) -> bytes:
	response = download.response
	if response.state is not ResponseState.COMPLETE:
		raise ValueError("Only complete trading responses can be published as daily archives")
	timestamps = dict(response.source_timestamps)
	rows = {bar.ticker: bar_payload(bar) for bar in response.records}
	for ticker, values in response.bad_records:
		if ticker in rows:
			raise ValueError("Duplicate archive ticker")
		rows[ticker] = {"T": ticker, **dict(zip(FIELDS, (None if value is None else str(value) for value in values)))}
	if not rows:
		raise ValueError("An empty archive does not establish no trading")
	content = []
	for ticker, row in sorted(rows.items()):
		if ticker not in timestamps:
			raise ValueError(f"Source timestamp is missing for {download.session.session_date}: {ticker}")
		row["t"] = timestamps[ticker]
		content.append(encode_json(row))
	result = b"".join(content)
	decode_archive(result, download.session)
	return result


def decode_archive(content: bytes, session: TradingSession) -> ProviderResponse:
	records = []
	bad = []
	timestamps = []
	tickers: set[str] = set()
	for line in content.splitlines():
		row = json.loads(line, parse_float=Decimal, object_pairs_hook=unique_object)
		if not isinstance(row, dict):
			raise ValueError("Archive record must be an object")
		ticker, timestamp = row.get("T"), row.get("t")
		if not isinstance(ticker, str) or not ticker or len(ticker) > 16 or any(character.isspace() for character in ticker) or ticker in tickers:
			raise ValueError("Invalid or duplicate archive ticker")
		if type(timestamp) is not int or datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone(ZoneInfo("America/New_York")).date() != session.session_date:
			raise ValueError("Archive timestamp and session disagree")
		tickers.add(ticker)
		timestamps.append((ticker, timestamp))
		values = tuple(row.get(field) for field in FIELDS)
		if invalid_fields(values):
			bad.append((ticker, values))
		else:
			records.append(provider_bar(row, session))
	if not tickers:
		raise ValueError("Empty archive does not establish no trading")
	return ProviderResponse(ResponseState.COMPLETE, tuple(sorted(records, key=lambda bar: bar.ticker)), 200, "OK", (("resultsCount", len(tickers)),), source_timestamps=tuple(timestamps), bad_records=tuple(bad))


def publish_archive(data_root: Path, download: DownloadedSession) -> tuple[Path, str]:
	content = archive_content(download)
	path = archive_path(data_root, download.session.session_date)
	atomic_write(path, content)
	return path, sha256(content).hexdigest()
