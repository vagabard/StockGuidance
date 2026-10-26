from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from support.ingestion import FakeDatabase

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.numerical_rebuild import SimplifiedNumericalRebuildRunner
from stockguidance.database.operation_recovery import RebuildResult
from stockguidance.database.simplified_contract import ClearScope, OperationKind, PendingScope, PendingUpdate, WorkUnit
from stockguidance.database.simplified_schema import initialize_simplified_schema
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.zscores.specification import ZScoreFamily
from stockguidance.zscores.storage import ZScoreSeriesDefinition


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
		ordered = tuple(sorted(self.sessions_by_date.values(), key=lambda item: item.sequence))
		index = ordered.index(session) + offset
		if index < 0 or index >= len(ordered):
			raise IndexError("fixture calendar boundary")
		return ordered[index]


def _session(day: date, sequence: int) -> TradingSession:
	return TradingSession("XNYS", day, sequence, datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc), datetime(day.year, day.month, day.day, 20, tzinfo=timezone.utc))


def _source(tmp_path: Path) -> SourceConfig:
	return SourceConfig(SourceIdentity("fixture", "fixture-endpoint", "us", "all", PriceAdjustment.UNADJUSTED), tmp_path / "data", tmp_path / "db.id", "fixture")


def _operation(source: SourceConfig, calendar: FixtureCalendar, *, price_delta: bool = False, date_id: int = 2) -> PendingUpdate:
	target = calendar.session(date(2024, 7, date_id))
	delta = WorkUnit("delta_session", date_id, 1, target.session_date)
	definition = ZScoreSeriesDefinition(
		data_configuration_key(source.data_configuration), "XNYS",
		ZScoreFamily.PRICE_DELTA if price_delta else ZScoreFamily.PRICE,
		2, "rolling-population-zscore-v1", "float32",
		"close_ratio_to_prior_minus_one" if price_delta else "not_applicable",
	)
	zscore = WorkUnit("zscore_session", date_id, None, target.session_date, definition.series_key, 2)
	clear = ClearScope(deltas=(delta,), zscores=(WorkUnit("zscore_session", date_id, 1, target.session_date, definition.series_key, 2),))
	scope = PendingScope(1, OperationKind.DERIVED_REPAIR, data_configuration_key(source.data_configuration), "XNYS", NOW, NOW, (), clear, (delta, zscore), ())
	return PendingUpdate(1, scope, NOW, NOW, True, None)


def _seed_database(source: SourceConfig, database: FakeDatabase, calendar: FixtureCalendar) -> None:
	initialize_simplified_schema(source, database, windows=(2,))
	for session in sorted(calendar.sessions_by_date.values(), key=lambda item: item.sequence):
		database.sql("INSERT INTO transaction_dates (transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision) VALUES (?, 1, ?, ?, ?)", (session.session_date.isoformat(), NOW.isoformat(), NOW.isoformat(), "a" * 64))
	database.sql("INSERT OR IGNORE INTO ticker_symbol (label, exchange) VALUES (?, ?)", ("AAA", "XNYS"))
	for date_id, close in ((1, "10.00000000"), (2, "12.00000000"), (3, "15.00000000")):
		database.sql(
		"INSERT INTO raw_stock_data (ticker_id, date_id, open, high, low, close, volume, vwap, transactions, state_id, observed_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
		(date_id, close, str(float(close) + 1), str(float(close) - 1), close, "100.00000000", close, 5, NOW.isoformat()),
	)


def _calendar() -> FixtureCalendar:
	days = (date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3))
	return FixtureCalendar({day: _session(day, index) for index, day in enumerate(days)})


class _NoRawRunner:
	def rebuild(self, _operation: PendingUpdate, _connection: object) -> RebuildResult:
		return RebuildResult()


def test_deltas_then_zscores_commit_explicit_rows_and_completion(tmp_path: Path) -> None:
	source = _source(tmp_path)
	calendar = _calendar()
	database = FakeDatabase(tmp_path / "stage7.sqlite")
	_seed_database(source, database, calendar)
	runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]
	result = runner.rebuild(_operation(source, calendar), database(tmp_path / "ignored"))

	assert {unit.unit_kind for unit in result.committed_units} == {"delta_session", "zscore_session"}
	assert result.deferred_units == ()
	assert database.sql("SELECT delta_complete FROM transaction_dates WHERE id = 2") == [(1,)]
	assert database.sql("SELECT close_ratio_to_prior, calculation_status, quality_flags FROM daily_delta_stock_data WHERE date_id = 2 AND ticker_id = 1") == [(pytest.approx(1.2), "ready", 0)]
	assert database.sql("SELECT status, zscore FROM zscores_price_2 WHERE date_id = 2 AND ticker_id = 1") == [(0, pytest.approx(1.0))]
	assert database.sql("SELECT row_count, ready_count, status_counts FROM stockguidance_zscore_sessions WHERE date_id = 2") == [(1, 1, '{"0":1,"1":0,"2":0,"3":0,"4":0,"5":0}')]


def test_latest_delta_is_completed_with_not_yet_available_labels(tmp_path: Path) -> None:
	source = _source(tmp_path)
	calendar = _calendar()
	database = FakeDatabase(tmp_path / "latest.sqlite")
	_seed_database(source, database, calendar)
	# The latest session has no following calendar session in this fixture.
	target = calendar.session(date(2024, 7, 3))
	definition = ZScoreSeriesDefinition(data_configuration_key(source.data_configuration), "XNYS", ZScoreFamily.PRICE, 2, "rolling-population-zscore-v1", "float32", "not_applicable")
	delta = WorkUnit("delta_session", 3, 1, target.session_date)
	zscore = WorkUnit("zscore_session", 3, None, target.session_date, definition.series_key, 2)
	clear = ClearScope(deltas=(delta,), zscores=(WorkUnit("zscore_session", 3, 1, target.session_date, definition.series_key, 2),))
	scope = PendingScope(1, OperationKind.DERIVED_REPAIR, data_configuration_key(source.data_configuration), "XNYS", NOW, NOW, (), clear, (delta, zscore), ())
	operation = PendingUpdate(1, scope, NOW, NOW, True, None)
	runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]
	runner.rebuild(operation, database(tmp_path / "ignored"))

	assert database.sql("SELECT calculation_status, close_ratio_to_prior, next_day_change_in_price FROM daily_delta_stock_data WHERE date_id = 3") == [("not_yet_available", pytest.approx(15 / 12), None)]


def test_price_delta_zscore_reads_only_completed_delta_sessions(tmp_path: Path) -> None:
	source = _source(tmp_path)
	calendar = _calendar()
	database = FakeDatabase(tmp_path / "price-delta.sqlite")
	_seed_database(source, database, calendar)
	runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]
	runner.rebuild(_operation(source, calendar, price_delta=True, date_id=2), database(tmp_path / "ignored"))
	runner.rebuild(_operation(source, calendar, price_delta=True, date_id=3), database(tmp_path / "ignored"))

	assert database.sql("SELECT delta_complete FROM transaction_dates WHERE id = 3") == [(1,)]
	assert database.sql("SELECT status, zscore FROM zscores_price_delta_2 WHERE date_id = 3 AND ticker_id = 1") == [(0, pytest.approx(1.0))]


def test_restart_preserves_delta_and_retries_zscore_atomically(tmp_path: Path) -> None:
	source = _source(tmp_path)
	calendar = _calendar()
	database = FakeDatabase(tmp_path / "restart.sqlite")
	_seed_database(source, database, calendar)
	runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]
	database.fail_on = "INSERT INTO daily_delta_stock_data"
	with pytest.raises(RuntimeError):
		runner.rebuild(_operation(source, calendar), database(tmp_path / "ignored"))
	assert database.sql("SELECT delta_complete FROM transaction_dates WHERE id = 2") == [(0,)]
	database.fail_on = None
	assert runner.rebuild(_operation(source, calendar), database(tmp_path / "ignored")).committed_units

	# A later failure rolls back the z-score row and metadata, while the delta
	# completion remains committed and is skipped on the retry.
	database.sql("DELETE FROM stockguidance_zscore_sessions WHERE date_id = 2")
	database.sql("DELETE FROM zscores_price_2 WHERE date_id = 2")
	database.fail_on = "INSERT INTO zscores_price_2"
	with pytest.raises(RuntimeError):
		runner.rebuild(_operation(source, calendar), database(tmp_path / "ignored"))
	assert database.sql("SELECT delta_complete FROM transaction_dates WHERE id = 2") == [(1,)]
	assert database.sql("SELECT COUNT(*) FROM stockguidance_zscore_sessions WHERE date_id = 2") == [(0,)]
	database.fail_on = None
	assert runner.rebuild(_operation(source, calendar), database(tmp_path / "ignored")).committed_units


def test_warmup_missing_predecessor_and_zero_variance_are_explicit(tmp_path: Path) -> None:
	source = _source(tmp_path)
	calendar = _calendar()
	runner_database = FakeDatabase(tmp_path / "edge-cases.sqlite")
	_seed_database(source, runner_database, calendar)
	runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]

	runner.rebuild(_operation(source, calendar, date_id=1), runner_database(tmp_path / "ignored"))
	assert runner_database.sql("SELECT calculation_status, close_ratio_to_prior FROM daily_delta_stock_data WHERE date_id = 1") == [("insufficient_history", None)]
	assert runner_database.sql("SELECT status, zscore FROM zscores_price_2 WHERE date_id = 1") == [(1, None)]

	runner_database.sql("DELETE FROM raw_stock_data WHERE date_id = 1 AND ticker_id = 1")
	runner_database.sql("DELETE FROM daily_delta_stock_data WHERE date_id = 1 AND ticker_id = 1")
	runner_database.sql("DELETE FROM zscores_price_2 WHERE date_id = 1 AND ticker_id = 1")
	runner_database.sql("DELETE FROM stockguidance_zscore_sessions WHERE date_id = 1")
	runner.rebuild(_operation(source, calendar, date_id=2), runner_database(tmp_path / "ignored"))
	assert runner_database.sql("SELECT calculation_status, close_ratio_to_prior FROM daily_delta_stock_data WHERE date_id = 2") == [("missing_input", None)]
	assert runner_database.sql("SELECT status, zscore FROM zscores_price_2 WHERE date_id = 2") == [(2, None)]

	runner_database.sql("UPDATE raw_stock_data SET close = ?, open = ?, high = ?, low = ?, vwap = ? WHERE date_id = 2", ("10.00000000", "10.00000000", "11.00000000", "9.00000000", "10.00000000"))
	runner_database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, open, high, low, close, volume, vwap, transactions, state_id, observed_at) VALUES (1, 1, ?, ?, ?, ?, ?, ?, ?, 1, ?)", ("10.00000000", "11.00000000", "9.00000000", "10.00000000", "100.00000000", "10.00000000", 5, NOW.isoformat()))
	runner_database.sql("DELETE FROM daily_delta_stock_data WHERE date_id = 2 AND ticker_id = 1")
	runner_database.sql("DELETE FROM zscores_price_2 WHERE date_id = 2 AND ticker_id = 1")
	runner_database.sql("DELETE FROM stockguidance_zscore_sessions WHERE date_id = 2")
	runner_database.sql("UPDATE transaction_dates SET delta_complete = 0, delta_completed_at = NULL WHERE id = 2")
	runner.rebuild(_operation(source, calendar, date_id=2), runner_database(tmp_path / "ignored"))
	assert runner_database.sql("SELECT status, zscore FROM zscores_price_2 WHERE date_id = 2") == [(5, None)]


def test_interpolated_provenance_is_aggregated_and_quality_is_preserved(tmp_path: Path) -> None:
	source = _source(tmp_path)
	calendar = _calendar()
	database = FakeDatabase(tmp_path / "interpolated.sqlite")
	_seed_database(source, database, calendar)
	database.sql("UPDATE raw_stock_data SET state_id = 4 WHERE date_id = 2 AND ticker_id = 1")
	database.sql(
		"INSERT INTO stockguidance_repair_metadata (date_id, ticker_id, revision, state_id, original_values, effective_values, invalid_fields, interpolation_method, prior_date_id, following_date_id, endpoint_values, observed_at, available_at, endpoints_available_at, recorded_at) VALUES (2, 1, ?, 4, ?, ?, ?, ?, 1, 3, ?, ?, ?, ?, ?)",
		("b" * 64, "[\"12\",\"13\",\"11\",\"12\",\"100\",\"12\",\"5\"]", "[\"12\",\"13\",\"11\",\"12\",\"100\",\"12\",\"5\"]", "[\"close\"]", "linear-trading-session-v1", "[[\"10\"],[\"15\"]]", NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
	)
	runner = SimplifiedNumericalRebuildRunner(source, calendar, _NoRawRunner(), clock=lambda: NOW)  # type: ignore[arg-type]
	runner.rebuild(_operation(source, calendar), database(tmp_path / "ignored"))

	delta_rows = database.sql("SELECT calculation_status, quality_flags, gap_length, provenance FROM daily_delta_stock_data WHERE date_id = 2")
	assert len(delta_rows) == 1 and delta_rows[0][0:3] == ("ready", 3, 1)
	provenance = delta_rows[0][3]
	assert "raw:2:1:interpolated" in provenance and "repair_revision:" + "b" * 64 in provenance
	assert database.sql("SELECT status, quality_flags, zscore FROM zscores_price_2 WHERE date_id = 2") == [(1, 3, None)]
