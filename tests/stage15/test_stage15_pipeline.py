from datetime import date, timedelta
import json
from pathlib import Path

import numpy as np
import pytest

from stockguidance.database.table_names import load_table_names
from stockguidance.performance.stage15 import _semantic_score
from stockguidance.performance.stage15_pipeline import CELL, SERIES, PipelineOptions, _plans, _reference, _reset, _validate, run_pipeline


def _fixture(ticker_count=9):
	dates = [(date(2024, 1, 1) + timedelta(days=index)).isoformat() for index in range(70)]
	tickers = tuple(range(1, ticker_count + 1))
	cache = np.empty((70, ticker_count), dtype=CELL)
	_reset(cache)
	for ticker_index in range(ticker_count):
		cache["close"][:, ticker_index] = 10 + ticker_index + np.sin(np.arange(70) / 8) + np.arange(70) * 0.01
	cache["close_present"].fill(True)
	cache["ratio"][1:] = cache["close"][1:] / cache["close"][:-1]
	cache["ratio_present"][1:] = True
	manifest = {"targets": [{"session": dates[index], "zscore_dependencies": dates[index - 60:index + 1]} for index in range(67, 70)]}
	date_ids = {session: index + 1 for index, session in enumerate(dates)}
	verified = {identifier: True for identifier in date_ids.values()}
	return cache, tickers, dates, date_ids, verified, manifest


@pytest.mark.parametrize("family,window", SERIES)
def test_vectorized_reference_matches_scalar_with_missing_invalid_and_stale_inputs(family, window):
	cache, tickers, dates, date_ids, verified, _ = _fixture()
	cache["close_present"][65, 1] = False
	cache["close"][65, 1] = np.nan
	cache["close"][65, 2] = -1
	cache["ratio"][66, 3] = 9
	cache["ratio_present"][66, 4] = False
	cache["close"][:, 5] = 10
	cache["ratio"][1:, 5] = 1
	cache["close"][:, 6] = 1e10 + np.arange(70) * 1e-5
	cache["ratio"][1:, 6] = cache["close"][1:, 6] / cache["close"][:-1, 6]
	values = {(index + 1, ticker): (float(cache["close"][index, column]) if cache["close_present"][index, column] else None, float(cache["ratio"][index, column]) if cache["ratio_present"][index, column] else None) for index in range(70) for column, ticker in enumerate(tickers)}
	for verified_flags in (np.ones(70, dtype=bool), np.arange(70) != 66):
		for dependency_indexes in (list(range(9, 70)), list(range(7))):
			status, scores = _reference(cache, dependency_indexes, verified_flags, family, window)
			for ticker_index, ticker in enumerate(tickers):
				expected_status, expected_score = _semantic_score(family, window, tuple(dates[index] for index in dependency_indexes), values, {index + 1: bool(value) for index, value in enumerate(verified_flags)}, date_ids, ticker)
				assert int(status[ticker_index]) == expected_status
				if expected_score is None:
					assert np.isnan(scores[ticker_index])
				else:
					assert float(scores[ticker_index]) == pytest.approx(expected_score, rel=1e-5, abs=1e-6)


def test_comparison_detects_missing_rows_nonnull_unavailable_scores_and_keeps_all_findings():
	cache, tickers, dates, _, _, manifest = _fixture(300)
	plan = _plans(manifest, PipelineOptions())[0]
	indexes = [dates.index(session) for session in plan["dates"]]
	result = _validate(cache[indexes], plan, tickers, np.ones(len(indexes), dtype=bool))
	assert result["mismatch_count"] == 3 * 300 * 4
	assert len(result["mismatches"]) > 1000
	cache["close_present"].fill(False)
	cache["statuses"].fill(2)
	cache["scores"].fill(0)
	result = _validate(cache[indexes], plan, tickers, np.ones(len(indexes), dtype=bool))
	assert result["mismatch_count"] == 3 * 300 * 4


def test_derived_vector_comparison_uses_decimal_reference_and_detects_corruption(monkeypatch):
	from decimal import Decimal
	from stockguidance.market_data.legacy_derived import legacy_rows
	from stockguidance.market_data.records import DailyBar
	from stockguidance.performance.stage15_derived import _validate_derived

	dates = ("2024-01-01", "2024-01-02", "2024-01-03")
	archives = {session: tuple(DailyBar(ticker, Decimal("10"), Decimal("15"), Decimal("8"), Decimal(str(11 + index)), Decimal("100"), None, None) for ticker in ("ABC", "xyz")) for index, session in enumerate(dates)}
	monkeypatch.setattr("stockguidance.performance.stage15_derived._archive", lambda path, digest: archives[path])
	files = {session: {"path": session, "file_sha256": "fixture"} for session in dates}
	target = {"session": dates[1], "derived_dependencies": [dates[0], dates[2]]}
	stored = [(row.ticker, *row.values) for row in legacy_rows(*(archives[session] for session in dates))]
	assert _validate_derived(target, files, stored)["matches"] is True
	corrupted = [(stored[0][0], 999.0, *stored[0][2:]), stored[1]]
	result = _validate_derived(target, files, corrupted)
	assert result["matches"] is False
	assert result["column_difference_counts"]["open_ratio_to_prior"] == 1
	assert result["differences"] == [{"ticker": "ABC", "columns": ["open_ratio_to_prior"]}]


class Cursor:
	def __init__(self, connection):
		self.connection = connection
		self.rows = []

	def execute(self, query, parameters=()):
		if "SELECT id FROM" in query:
			self.rows = [(ticker,) for ticker in self.connection.tickers]
			return
		self.connection.reads += 1
		table = query.split(" FROM `", 1)[1].split("`", 1)[0]
		if "ticker_id IN (" in query:
			count = query.rsplit("ticker_id IN (", 1)[1].count("%s")
			dates, tickers = parameters[:-count], parameters[-count:]
			self.connection.filtered_queries += 1
		else:
			dates, tickers = parameters, self.connection.tickers
		self.rows = [row for row in self.connection.tables[table] if row[0] in dates and row[1] in tickers]

	def fetchall(self):
		return self.rows

	def fetchmany(self, count):
		result, self.rows = self.rows[:count], self.rows[count:]
		return result

	def close(self):
		pass


class Connection:
	def __init__(self):
		cache, self.tickers, dates, self.date_ids, self.verified, self.manifest = _fixture()
		self.reads = 0
		self.filtered_queries = 0
		names = load_table_names()
		self.tables = {names.prices: [], names.deltas: []}
		for date_index, _ in enumerate(dates):
			for column, ticker in enumerate(self.tickers):
				self.tables[names.prices].append((date_index + 1, ticker, float(cache["close"][date_index, column])))
				self.tables[names.deltas].append((date_index + 1, ticker, None if date_index == 0 else float(cache["ratio"][date_index, column])))
		for series_index, (family, window) in enumerate(SERIES):
			resolved = names.resolve_zscore_tables((window,))[window]
			table = resolved.price if family == "price" else resolved.price_delta
			self.tables[table] = []
			for target_index in range(67, 70):
				statuses, scores = _reference(cache, list(range(target_index - 60, target_index + 1)), np.ones(70, dtype=bool), family, window)
				for column, ticker in enumerate(self.tickers):
					self.tables[table].append((target_index + 1, ticker, float(scores[column]), int(statuses[column])))

	def cursor(self):
		return Cursor(self)


def test_spawn_pipeline_partial_resume_matches_uninterrupted_and_rejects_changed_checkpoint(tmp_path, monkeypatch):
	monkeypatch.setattr("stockguidance.performance.stage15._zscore_metadata", lambda *arguments: {"checked": True})
	connection = Connection()
	names = load_table_names()
	options = PipelineOptions(workers=2, vector_size=3, fetch_size=2, sessions_per_batch=2, max_batches=2)
	partial = run_pipeline(connection, names, connection.manifest, connection.date_ids, connection.verified, tmp_path, {"revision": "fixture"}, options)
	assert partial["status"] == "partial"
	assert partial["batch_count"] == 2
	assert partial["mismatch_count"] == 0
	options = PipelineOptions(workers=2, vector_size=3, fetch_size=2, sessions_per_batch=2)
	resumed = run_pipeline(connection, names, connection.manifest, connection.date_ids, connection.verified, tmp_path, {"revision": "fixture"}, options)
	assert resumed["status"] == "complete"
	assert resumed["batch_count"] == 6
	assert resumed["mismatch_count"] == 0
	assert set(resumed["actual_rows"].values()) == {27}
	reads = connection.reads
	again = run_pipeline(connection, names, connection.manifest, connection.date_ids, connection.verified, tmp_path, {"revision": "fixture"}, options)
	assert connection.reads == reads
	assert again["actual_rows"] == resumed["actual_rows"]
	fresh = run_pipeline(connection, names, connection.manifest, connection.date_ids, connection.verified, tmp_path / "fresh", {"revision": "fixture"}, options)
	assert fresh["actual_rows"] == resumed["actual_rows"]
	assert fresh["mismatch_count"] == resumed["mismatch_count"]
	checkpoint = next(Path(resumed["checkpoint_directory"]).glob("batch-*.json"))
	payload = json.loads(checkpoint.read_text())
	payload["mismatch_count"] = 123
	checkpoint.write_text(json.dumps(payload))
	with pytest.raises(ValueError, match="Invalid Stage 15 batch checkpoint"):
		run_pipeline(connection, names, connection.manifest, connection.date_ids, connection.verified, tmp_path, {"revision": "fixture"}, options)


def test_loader_failure_reaches_single_result_consumer_and_cleans_up(tmp_path, monkeypatch):
	connection = Connection()
	def fail(*arguments, **keywords):
		raise RuntimeError("fixture database read failed")
	monkeypatch.setattr("stockguidance.performance.stage15_pipeline._load", fail)
	with pytest.raises(RuntimeError, match="fixture database read failed"):
		run_pipeline(connection, load_table_names(), connection.manifest, connection.date_ids, connection.verified, tmp_path, {}, PipelineOptions(workers=2, vector_size=3))
	assert not list(tmp_path.glob("*/batch-*.json"))


def test_small_partial_scope_filters_database_reads_and_resumes_nonzero_ticker_offset(tmp_path, monkeypatch):
	monkeypatch.setattr("stockguidance.performance.stage15._zscore_metadata", lambda *arguments: {})
	connection = Connection()
	options = PipelineOptions(workers=2, vector_size=3, sessions_per_batch=2, max_batches=1)
	first = run_pipeline(connection, load_table_names(), connection.manifest, connection.date_ids, connection.verified, tmp_path, {}, options)
	assert first["batch_count"] == 1
	assert first["mismatch_count"] == 0
	assert connection.filtered_queries == 6
	second = run_pipeline(connection, load_table_names(), connection.manifest, connection.date_ids, connection.verified, tmp_path, {}, options)
	assert second["batch_count"] == 2
	assert second["mismatch_count"] == 0
	assert connection.filtered_queries == 12


def test_raw_worker_keeps_exact_decimals_and_source_identity(monkeypatch):
	from decimal import Decimal

	from stockguidance.legacy_migration.precision import ExactArchiveBar, exact_records_id
	from stockguidance.performance.stage15_derived import _validate_raw

	bar = ExactArchiveBar("AbC", Decimal("10.12345678"), Decimal("11"), Decimal("9"), Decimal("10"), Decimal("12.5"), Decimal("10.1234567890123456789012"), 3)
	monkeypatch.setattr("stockguidance.performance.stage15_derived.read_exact_archive", lambda path: ((bar,), "fixture-hash"))
	item = {"session": "2024-01-01", "path": "fixture", "file_sha256": "fixture-hash", "records_id": exact_records_id((bar,))}
	stored = [(1, bar.ticker, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)]
	assert _validate_raw(item, stored)["matches"] is True
	changed = [(1, bar.ticker, Decimal("10.12345679"), bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)]
	assert _validate_raw(item, changed)["matches"] is False
	with pytest.raises(ValueError, match="source archive changed"):
		_validate_raw({**item, "file_sha256": "changed"}, stored)
