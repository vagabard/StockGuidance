from decimal import Decimal
import json

import pytest

from stockguidance.performance.stage15 import _raw_result, _semantic_score
from stockguidance.performance.archive_read import ArchiveBar
from stockguidance.market_data.records import DailyBar


@pytest.mark.parametrize("report,exit_code", [({"status": "complete", "acceptance": {"audit_passed": False}}, 2), ({"status": "partial"}, 0)])
def test_cli_reports_failed_completed_validation_and_intentional_partial_work(monkeypatch, capsys, report, exit_code):
	from stockguidance.cli.legacy_migration_benchmark import main

	monkeypatch.setattr("stockguidance.performance.stage15.validate_prerequisites", lambda *arguments: None)
	monkeypatch.setattr("stockguidance.performance.stage15.run_stage15", lambda *arguments, **keywords: report)
	assert main(["--run-stage15"]) == exit_code
	assert json.loads(capsys.readouterr().out)["successful"] == (exit_code == 0)


def _bar(ticker: str, close: str) -> tuple[ArchiveBar, DailyBar]:
	values = {"open": Decimal("9"), "high": Decimal(close), "low": Decimal("9"), "close": Decimal(close), "volume": Decimal("10"), "vwap": None, "transactions": 2}
	return ArchiveBar(ticker, **values), DailyBar(ticker, **values)


def test_raw_result_reports_exact_agreement_and_field_difference() -> None:
	expected, actual = _bar("ABC", "10")
	result = _raw_result("2024-01-02", (expected,), {"ABC": actual})
	assert result["matches"] is True
	changed = DailyBar("ABC", actual.open, Decimal("11"), actual.low, Decimal("11"), actual.volume, actual.vwap, actual.transactions)
	result = _raw_result("2024-01-02", (expected,), {"ABC": changed})
	assert result["matches"] is False
	assert result["field_difference_counts"]["close"] == 1


def test_semantic_price_score_uses_float32_and_statuses() -> None:
	dates = ("2024-01-01", "2024-01-02", "2024-01-03")
	date_ids = {value: index + 1 for index, value in enumerate(dates)}
	values = {(index + 1, 7): (Decimal(str(index + 1)), None) for index in range(3)}
	verified = {index + 1: True for index in range(3)}
	status, score = _semantic_score("price", 3, dates, values, verified, date_ids, 7)
	assert status == 0
	assert score is not None
	status, score = _semantic_score("price", 3, dates, values, {1: False, 2: True, 3: True}, date_ids, 7)
	assert status == 3
	assert score is None


def test_semantic_delta_score_rejects_a_stale_stored_ratio() -> None:
	dates = ("2024-01-01", "2024-01-02")
	date_ids = {value: index + 1 for index, value in enumerate(dates)}
	values = {(1, 7): (Decimal("10"), None), (2, 7): (Decimal("11"), Decimal("1.2"))}
	status, score = _semantic_score("price_delta", 2, dates, values, {1: True, 2: True}, date_ids, 7)
	assert status == 4
	assert score is None


def test_metadata_checks_ready_counts_and_historical_availability() -> None:
	from stockguidance.database.table_names import load_table_names
	from stockguidance.market_data.identity import content_id
	from stockguidance.performance.stage15 import _zscore_metadata

	series_rows = []
	metadata_rows = []
	expected_counts = {}
	for family in ("price", "price_delta"):
		for window in (20, 60):
			definition = {"schema_version": 1, "source_scope": content_id({}), "calendar_id": "XNYS", "family": family, "window": window, "calculation_version": "rolling-population-zscore-v1", "delta_definition": "close_ratio_to_prior_minus_one" if family == "price_delta" else "not_applicable", "precision": "float32", "missing_value_policy": "null_status_v1"}
			key = content_id(definition)
			series_rows.append((key, *definition.values()))
			counts = {code: 2 if code == 0 else 1 if code == 2 else 0 for code in range(6)}
			metadata_rows.append([key, 123, 3, 2, ",".join(f"{code}:{value}" for code, value in counts.items()), "a" * 64, "b" * 64, "c" * 64, False])
			expected_counts[(123, family, window)] = counts

	class Cursor:
		def execute(self, query):
			self.rows = metadata_rows if "row_count" in query else series_rows

		def fetchall(self):
			return self.rows

		def close(self):
			pass

	class Connection:
		def cursor(self):
			return Cursor()

	manifest = {"source": {"archive_source": {}}}
	result = _zscore_metadata(Connection(), load_table_names(), manifest, {123: {}}, expected_counts, 3)
	assert not result["series_findings"]
	assert not result["metadata_findings"]
	published = [{"session": "2024-01-01", "family": row[4], "window": row[5], "content_digest": "a" * 64, "dependency_digest": "b" * 64, "availability_digest": "c" * 64} for row in series_rows]
	result = _zscore_metadata(Connection(), load_table_names(), manifest, {123: {"session": "2024-01-01"}}, expected_counts, 3, published)
	assert result["validation_version"] == 2
	assert not result["metadata_findings"]
	published[0]["content_digest"] = "d" * 64
	result = _zscore_metadata(Connection(), load_table_names(), manifest, {123: {"session": "2024-01-01"}}, expected_counts, 3, published)
	assert [item["finding"] for item in result["metadata_findings"]] == ["published_digest_binding_mismatch"]
	metadata_rows[0][3] = 3
	metadata_rows[1][8] = True
	result = _zscore_metadata(Connection(), load_table_names(), manifest, {123: {}}, expected_counts, 3)
	assert {item["finding"] for item in result["metadata_findings"]} == {"metadata_count_or_digest_shape_mismatch", "archive_snapshot_must_not_claim_historical_availability"}


@pytest.mark.parametrize("global_raw_rows,proven,cached_count", [(2, True, None), (3, False, None), (3, False, 3), (3, False, 4)])
def test_integrity_uses_complete_keyspace_proof_and_scans_on_count_drift(global_raw_rows, proven, cached_count):
	from stockguidance.database.table_names import load_table_names
	from stockguidance.performance.stage15 import _integrity

	names = load_table_names()
	files = tuple({"session": session, "valid": True} for session in ("2024-01-01", "2024-01-02"))
	audit = {"raw_derived": {"raw_engine": "exact-archive-v2", "raw_sessions": [{"session": item["session"], "database_count": 1, "matches": True} for item in files], "derived_sessions": [{"session": "2024-01-01", "state": "complete", "stored_count": 1, "matches": True}]}, "zscore": {"status": "complete", "mismatch_count": 0, "expected_rows": 2, "actual_rows": {f"{family}/{window}": 2 for family in ("price", "price_delta") for window in (20, 60)}}}
	counts = {names.prices: global_raw_rows, names.deltas: 1}
	for window in (20, 60):
		resolved = names.resolve_zscore_tables((window,))[window]
		counts.update({resolved.price: 2, resolved.price_delta: 2})
	if cached_count is not None:
		audit["revision_before"] = "fixture-revision"
		audit["database"] = {"read_only": True, "revision_before": "fixture-revision", "revision_after": "fixture-revision", "table_populations": {"counts": {**counts, names.prices: cached_count}}, "integrity": {"orphan_counts": {"raw_without_derived_key": 17}}}
	queries = []

	class Cursor:
		def execute(self, query, parameters=()):
			queries.append(query)
			self.query = query

		def fetchall(self):
			return [(7, "ABC")] if "label" in self.query else [(1, "2024-01-01"), (2, "2024-01-02")]

		def fetchone(self):
			return (1,) if f"FROM `{names.prices}` r LEFT JOIN `{names.tickers}`" in self.query else (0,)

		def close(self):
			pass

	class Connection:
		def cursor(self):
			return Cursor()

	result = _integrity(Connection(), names, files, {"2024-01-01": 1, "2024-01-02": 2}, {"counts": counts}, audit)
	assert result["scope_proofs"]["raw"] is proven
	assert all(result["scope_proofs"]["zscores"].values())
	if proven:
		assert len(queries) == 2
		assert result["orphan_counts"]["raw_without_derived_key"] == 1
	else:
		assert len(queries) > 2
		assert result["orphan_counts"]["raw_without_ticker"] == 1
		assert result["orphan_counts"]["raw_without_derived_key"] == (17 if cached_count == global_raw_rows else 0)
		assert bool(result["scope_proofs"]["reused_sql_checks"]) == (cached_count == global_raw_rows)
