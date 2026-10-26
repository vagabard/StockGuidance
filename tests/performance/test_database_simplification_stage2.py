from __future__ import annotations

from stockguidance.performance.database_simplification_stage2 import (
	_query_case,
	_decision,
	_schema_statements,
	build_population,
)


def test_stage2_population_is_deterministic_and_contains_required_states() -> None:
	first = build_population(session_count=64, ticker_count=100)
	second = build_population(session_count=64, ticker_count=100)

	assert first.rows == second.rows
	assert first.state_counts == second.state_counts
	assert set(first.state_counts) == {"good", "bad", "missing", "interpolated"}
	assert first.state_counts["missing"] > 0
	assert len(_query_case(first)) == 3


def test_stage2_layouts_have_equivalent_covering_state_indexes() -> None:
	raw_ddl = " ".join(_schema_statements("raw_column"))
	separate_ddl = " ".join(_schema_statements("separate_state"))

	assert "state_id SMALLINT UNSIGNED NOT NULL" in raw_ddl
	assert "stage2_raw_date_ticker_state (date_id, ticker_id, state_id)" in raw_ddl
	assert "stage2_state_date_ticker (date_id, ticker_id, state_id)" in separate_ddl
	assert "stage2_raw_data_states" in separate_ddl


def test_stage2_fallback_is_raw_column_when_measurement_is_incomplete() -> None:
	decision = _decision({}, {}, {}, measurement_complete=False)

	assert decision["selected_layout"] == "raw_column"
	assert decision["selection_status"] == "inconclusive"


def test_stage2_measured_rule_selects_separate_only_with_all_guards() -> None:
	cases = ("narrow_ticker_lookback", "broad_training_range", "quality_filtered_recent")
	query_results = {
		"raw_column": {case: {"summary": {"median_seconds": 1.0, "p95_seconds": 1.0}} for case in cases},
		"separate_state": {case: {"summary": {"median_seconds": 0.8, "p95_seconds": 1.0}} for case in cases},
	}
	writes = {
		"raw_column": {operation: {"summary": {"median_seconds": 1.0, "p95_seconds": 1.0}} for operation in ("insert", "update")},
		"separate_state": {operation: {"summary": {"median_seconds": 0.8, "p95_seconds": 1.0}} for operation in ("insert", "update")},
	}
	verification = {case: {"identical": True} for case in cases}

	decision = _decision(query_results, writes, verification, measurement_complete=True)

	assert decision["selected_layout"] == "separate_state"
	assert decision["selected_by_rule"] is True
