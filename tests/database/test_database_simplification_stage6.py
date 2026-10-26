from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from support.ingestion import FakeDatabase

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.raw_rebuild import (
	AdjustmentMismatchError,
	FilePreparedPayloadReader,
	RawPayloadError,
	RawPayloadPreparer,
	RawRebuildRunner,
	read_canonical_file,
)
from stockguidance.database.simplified_contract import ClearScope, OperationKind, PendingScope, PendingUpdate, PreparedPayload, WorkUnit
from stockguidance.database.simplified_schema import initialize_simplified_schema
from stockguidance.market_data.archive import archive_content
from stockguidance.market_data.providers.contracts import DownloadedSession, ProviderResponse, ResponseState
from stockguidance.market_data.records import DailyBar, records_id


NOW = datetime(2024, 7, 10, 23, tzinfo=timezone.utc)


@dataclass
class FixtureCalendar:
	sessions_by_date: dict[date, TradingSession]

	calendar_id = "XNYS"
	version = "fixture"
	timezone = timezone.utc
	start = date(2024, 7, 1)
	end = date(2024, 7, 10)

	def session(self, session_date: date) -> TradingSession:
		return self.sessions_by_date[session_date]

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		return tuple(item for item in self.sessions_by_date.values() if start <= item.session_date <= end)

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		return self.sessions_by_date[date.fromordinal(session.session_date.toordinal() + offset)]


def _session(day: date, sequence: int) -> TradingSession:
	return TradingSession("XNYS", day, sequence, datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc), datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc))


def _source(tmp_path: Path) -> SourceConfig:
	return SourceConfig(SourceIdentity("fixture", "fixture-endpoint", "us", "all", PriceAdjustment.UNADJUSTED), tmp_path / "data", tmp_path / "db.id", "fixture")


def _bar(close: str, ticker: str = "AAA") -> DailyBar:
	return DailyBar(ticker, Decimal("10.00000000"), Decimal("12.00000000"), Decimal("9.00000000"), Decimal(close), Decimal("100.00000000"), Decimal("10.00000000000000000000"), 5)


def _complete(session: TradingSession, bar: DailyBar, *, bad: tuple[tuple[str, tuple[object, ...]], ...] = ()) -> ProviderResponse:
	timestamps = ((bar.ticker, int(session.closes_at.timestamp() * 1000)),) + tuple((ticker, int(session.closes_at.timestamp() * 1000)) for ticker, _ in bad)
	return ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1 + len(bad)),), source_timestamps=timestamps, bad_records=bad)


def _operation(source: SourceConfig, payload: PreparedPayload, session: TradingSession, *, repair: bool = False, date_id: int = 1) -> PendingUpdate:
	units = [WorkUnit("raw_session", date_id, None, session.session_date)]
	if repair:
		units.append(WorkUnit("repair_row", date_id, 1, session.session_date))
	scope = PendingScope(1, OperationKind.INITIAL_IMPORT, "a" * 64, "XNYS", NOW, NOW, (payload,), ClearScope(), tuple(units), ())
	return PendingUpdate(1, scope, NOW, NOW, True, None)


def test_preparer_reads_exact_canonical_path_and_forced_refresh_uses_provider(tmp_path: Path) -> None:
	source = _source(tmp_path)
	session = _session(date(2024, 7, 8), 1)
	response = _complete(session, _bar("11.00000000"))
	path = source.data_root / "market_summary" / "2024" / "2024-07-08.ndjson"
	path.parent.mkdir(parents=True)
	path.write_bytes(archive_content(DownloadedSession(session, response, NOW)))

	class Provider:
		calls = 0

		def fetch(self, _source: SourceConfig, requested: TradingSession) -> ProviderResponse:
			self.calls += 1
			return _complete(requested, _bar("12.00000000"))

	provider = Provider()
	preparer = RawPayloadPreparer(source, tmp_path / "prepared", provider, clock=lambda: NOW)
	local = preparer.prepare(session)
	assert provider.calls == 0
	assert FilePreparedPayloadReader().read(local, session).raw_id == records_id(response.records)
	forced = preparer.prepare(session, force_refresh=True)
	assert provider.calls == 1
	assert forced.temporary_path != local.temporary_path
	assert FilePreparedPayloadReader().read(forced, session).response.records[0].close == Decimal("12.00000000")


def test_preparer_rejects_adjusted_source_from_canonical_file(tmp_path: Path) -> None:
	source = SourceConfig(SourceIdentity("fixture", "fixture-endpoint", "us", "all", PriceAdjustment.ADJUSTED), tmp_path / "data", tmp_path / "db.id", "fixture")
	session = _session(date(2024, 7, 8), 1)
	path = source.data_root / "market_summary" / "2024" / "2024-07-08.ndjson"
	path.parent.mkdir(parents=True)
	path.write_bytes(archive_content(DownloadedSession(session, _complete(session, _bar("11.00000000")), NOW)))
	with pytest.raises(AdjustmentMismatchError):
		RawPayloadPreparer(source, tmp_path / "prepared", clock=lambda: NOW).prepare(session)


def test_missing_canonical_uses_provider_fallback_and_malformed_duplicate_files_fail(tmp_path: Path) -> None:
	source = _source(tmp_path)
	session = _session(date(2024, 7, 8), 1)

	class Provider:
		calls = 0

		def fetch(self, _source: SourceConfig, requested: TradingSession) -> ProviderResponse:
			self.calls += 1
			return _complete(requested, _bar("11.00000000"))

	provider = Provider()
	payload = RawPayloadPreparer(source, tmp_path / "prepared", provider, clock=lambda: NOW).prepare(session)
	assert provider.calls == 1
	assert FilePreparedPayloadReader().read(payload, session).response.state is ResponseState.COMPLETE

	path = tmp_path / "duplicate.ndjson"
	row = {"T": "AAA", "t": int(session.closes_at.timestamp() * 1000), "o": "10", "h": "12", "l": "9", "c": "11", "v": "100", "vw": "10", "n": 5}
	path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
	with pytest.raises(RawPayloadError, match="Malformed"):
		read_canonical_file(path, session, NOW)


def test_raw_session_and_interpolation_commit_decimal_values_and_states(tmp_path: Path) -> None:
	source = _source(tmp_path)
	days = (date(2024, 7, 5), date(2024, 7, 8), date(2024, 7, 9))
	calendar = FixtureCalendar({day: _session(day, index) for index, day in enumerate(days)})
	database = FakeDatabase(tmp_path / "raw.sqlite")
	initialize_simplified_schema(source, database, windows=(20,))
	for day in days:
		database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (day.isoformat(),))
	# The middle response is a complete response with one explicitly invalid close.
	bad_values = ("10.00000000", "20.00000000", "9.00000000", "0", "100.00000000", "10.00000000000000000000", 5)
	middle = calendar.session(days[1])
	response = ProviderResponse(
		ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 1),),
		source_timestamps=(("AAA", int(middle.closes_at.timestamp() * 1000)),), bad_records=(("AAA", bad_values),),
	)
	content = archive_content(DownloadedSession(middle, response, NOW))
	prepared_path = tmp_path / "prepared.ndjson"
	prepared_path.write_bytes(content)
	payload = PreparedPayload("middle", days[1], prepared_path, hashlib.sha256(content).hexdigest(), records_id(response.records), NOW, NOW)
	# Seed bounded valid interpolation endpoints.
	database.sql("INSERT INTO ticker_symbol (label, exchange) VALUES (?, ?)", ("AAA", "XNYS"))
	database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, open, high, low, close, volume, vwap, transactions, state_id, observed_at) VALUES (1,1,?,?,?,?,?,?,?,1,?)", ("10.00000000", "12.00000000", "9.00000000", "10.00000000", "100.00000000", "10.00000000000000000000", 5, NOW.isoformat()))
	database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, open, high, low, close, volume, vwap, transactions, state_id, observed_at) VALUES (1,3,?,?,?,?,?,?,?,1,?)", ("20.00000000", "22.00000000", "19.00000000", "20.00000000", "100.00000000", "20.00000000000000000000", 5, NOW.isoformat()))
	runner = RawRebuildRunner(source, calendar, clock=lambda: NOW)
	result = runner.rebuild(_operation(source, payload, middle, repair=True, date_id=2), database(tmp_path / "ignored"))
	assert len(result.committed_units) == 2
	assert result.explicit_non_ready == ()
	row = database.sql("SELECT state_id, close, raw_complete, raw_revision FROM raw_stock_data JOIN transaction_dates ON transaction_dates.id = raw_stock_data.date_id WHERE date_id = 2")[0]
	assert row[0] == 4 and Decimal(str(row[1])) == Decimal("15.00000000") and row[2:] == (1, records_id(response.records))
	assert database.sql("SELECT state_id, interpolation_method, prior_date_id, following_date_id FROM stockguidance_repair_metadata WHERE date_id = 2 AND ticker_id = 1") == [(4, "linear-trading-session-v1", 1, 3)]


def test_missing_and_no_trading_do_not_create_cartesian_placeholders(tmp_path: Path) -> None:
	source = _source(tmp_path)
	session = _session(date(2024, 7, 8), 1)
	database = FakeDatabase(tmp_path / "missing.sqlite")
	initialize_simplified_schema(source, database, windows=(20,))
	database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (session.session_date.isoformat(),))
	bad = (("AAA", (0, 0, 0, 0, 0, None, 0)),)
	response = _complete(session, _bar("11.00000000", "BBB"), bad=bad)
	content = archive_content(DownloadedSession(session, response, NOW))
	path = tmp_path / "payload.ndjson"
	path.write_bytes(content)
	payload = PreparedPayload("missing", session.session_date, path, hashlib.sha256(content).hexdigest(), records_id(response.records), NOW, NOW)
	calendar = FixtureCalendar({session.session_date: session})
	result = RawRebuildRunner(source, calendar, clock=lambda: NOW).rebuild(_operation(source, payload, session), database(tmp_path / "ignored"))
	assert len(result.explicit_non_ready) == 1
	rows = database.sql("SELECT label, state_id, open, close FROM ticker_symbol JOIN raw_stock_data ON raw_stock_data.ticker_id = ticker_symbol.id ORDER BY label")
	assert rows[0][:2] == ("AAA", 3) and rows[0][2:] == (None, None)
	assert rows[1][0:2] == ("BBB", 1) and Decimal(str(rows[1][2])) == Decimal("10.00000000") and Decimal(str(rows[1][3])) == Decimal("11.00000000")
	assert database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(2,)]


def test_gap_without_endpoints_is_a_completed_bad_raw_result(tmp_path: Path) -> None:
	source = _source(tmp_path)
	session = _session(date(2024, 7, 8), 1)
	database = FakeDatabase(tmp_path / "gap.sqlite")
	initialize_simplified_schema(source, database, windows=(20,))
	database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (session.session_date.isoformat(),))
	bad_values = ("10.00000000", "20.00000000", "9.00000000", "0", "100.00000000", "10.00000000000000000000", 5)
	response = ProviderResponse(ResponseState.COMPLETE, (), 200, "OK", (("resultsCount", 1),), source_timestamps=(("AAA", int(session.closes_at.timestamp() * 1000)),), bad_records=(("AAA", bad_values),))
	content = archive_content(DownloadedSession(session, response, NOW))
	path = tmp_path / "gap.ndjson"
	path.write_bytes(content)
	payload = PreparedPayload("gap", session.session_date, path, hashlib.sha256(content).hexdigest(), records_id(response.records), NOW, NOW)
	result = RawRebuildRunner(source, FixtureCalendar({session.session_date: session}), clock=lambda: NOW).rebuild(_operation(source, payload, session, repair=True), database(tmp_path / "ignored"))
	assert len(result.explicit_non_ready) == 2
	assert database.sql("SELECT state_id, raw_complete FROM raw_stock_data JOIN transaction_dates ON transaction_dates.id = raw_stock_data.date_id") == [(2, 1)]


def test_no_trading_completes_without_creating_raw_rows(tmp_path: Path) -> None:
	source = _source(tmp_path)
	session = _session(date(2024, 7, 8), 1)
	database = FakeDatabase(tmp_path / "no-trading.sqlite")
	initialize_simplified_schema(source, database, windows=(20,))
	database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (session.session_date.isoformat(),))
	response = ProviderResponse(ResponseState.NO_TRADING, (), 200, "OK", (("resultsCount", 0),), evidence="Fixture-authoritative no trading")

	class Provider:
		def fetch(self, _source: SourceConfig, _session: TradingSession) -> ProviderResponse:
			return response

	payload = RawPayloadPreparer(source, tmp_path / "prepared", Provider(), clock=lambda: NOW).prepare(session)
	result = RawRebuildRunner(source, FixtureCalendar({session.session_date: session}), clock=lambda: NOW).rebuild(_operation(source, payload, session), database(tmp_path / "ignored"))
	assert result.committed_units and result.explicit_non_ready == ()
	assert database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(0,)]
	assert database.sql("SELECT raw_complete FROM transaction_dates WHERE id = 1") == [(1,)]


def test_raw_batch_interruption_rolls_back_rows_and_completion(tmp_path: Path) -> None:
	source = _source(tmp_path)
	session = _session(date(2024, 7, 8), 1)
	database = FakeDatabase(tmp_path / "interrupt.sqlite")
	initialize_simplified_schema(source, database, windows=(20,))
	database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (session.session_date.isoformat(),))
	response = _complete(session, _bar("11.00000000"))
	content = archive_content(DownloadedSession(session, response, NOW))
	path = tmp_path / "interrupt.ndjson"
	path.write_bytes(content)
	payload = PreparedPayload("interrupt", session.session_date, path, hashlib.sha256(content).hexdigest(), records_id(response.records), NOW, NOW)
	database.fail_on = "INSERT INTO raw_stock_data"
	runner = RawRebuildRunner(source, FixtureCalendar({session.session_date: session}), clock=lambda: NOW)
	with pytest.raises(RuntimeError):
		runner.rebuild(_operation(source, payload, session), database(tmp_path / "ignored"))
	assert database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(0,)]
	assert database.sql("SELECT raw_complete FROM transaction_dates WHERE id = 1") == [(0,)]
	database.fail_on = None
	assert len(runner.rebuild(_operation(source, payload, session), database(tmp_path / "ignored")).committed_units) == 1

