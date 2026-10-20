import json

from stockguidance.database.table_names import load_table_names
from stockguidance.market_data.identity import content_id
from stockguidance.performance.sql_zscore_candidate import (
	COMPUTE_ROUTINE_NAME,
	DEPENDENCY_TABLE,
	PUBLISH_ROUTINE_NAME,
	SCORE_TABLE,
	TICKER_SCOPE_TABLE,
	_series_definition,
	_series_key,
	sql_zscore_objects,
	sql_zscore_schema_statements,
)


def _manifest(tmp_path):
	names = load_table_names()
	columns = {
		"open": "DECIMAL(28,8)",
		"high": "DECIMAL(28,8)",
		"low": "DECIMAL(28,8)",
		"close": "DECIMAL(28,8)",
		"volume": "DECIMAL(20,8)",
		"vwap": "DECIMAL(42,22)",
	}
	precision = tmp_path / "precision.json"
	precision.write_text(json.dumps({"schema_selection": {"schema_hash": content_id(columns), "columns": columns, "ddl": columns}}), encoding="utf-8")
	return {
		"source": {"archive_source": {"provider": "polygon", "endpoint": "grouped", "market": "us-stocks", "universe": "us-stocks-excluding-otc", "adjustment": "adjusted", "interval": "1d"}},
		"authoritative_registry": {"tables": names.tables(), "zscore_templates": names.templates()},
		"corrected_schema_hash": content_id(columns),
		"inputs": {"precision_report": str(precision)},
	}


def test_sql_zscore_candidate_is_set_based_and_atomic(tmp_path):
	manifest = _manifest(tmp_path)
	statements = sql_zscore_schema_statements(manifest)
	compute, publish = sql_zscore_objects(manifest)

	assert DEPENDENCY_TABLE in statements[0]
	assert any(SCORE_TABLE in statement for statement in statements)
	assert any(TICKER_SCOPE_TABLE in statement for statement in statements)
	assert COMPUTE_ROUTINE_NAME in compute
	assert PUBLISH_ROUTINE_NAME in publish
	assert "STDDEV_POP" in compute
	assert "expected_raw_id" in compute
	assert "sequence_no" in compute
	assert "INSERT INTO" in publish and "SELECT v_date_id" in publish
	assert "SUM(status = 0)" in publish
	assert " COMMIT;" not in (compute + publish).upper()
	assert "LOOP" not in (compute + publish).upper()
	for table in names_for(manifest):
		assert table in publish


def names_for(manifest):
	names = load_table_names()
	resolved = names.resolve_zscore_tables((20, 60))
	return (resolved[20].price, resolved[60].price, resolved[20].price_delta, resolved[60].price_delta)


def test_sql_zscore_series_identity_matches_the_shared_contract(tmp_path):
	manifest = _manifest(tmp_path)
	definition = _series_definition(manifest, "price_delta", 60)
	assert definition["delta_definition"] == "close_ratio_to_prior_minus_one"
	assert definition["precision"] == "float32"
	assert _series_key(manifest, "price_delta", 60) == content_id(definition)


def test_sql_zscore_staging_names_cannot_shadow_registry(tmp_path):
	manifest = _manifest(tmp_path)
	manifest["authoritative_registry"]["tables"]["deltas"] = TICKER_SCOPE_TABLE
	try:
		sql_zscore_schema_statements(manifest)
	except ValueError as error:
		assert str(error) == "Stage 8c staging table collides with the selected registry"
	else:
		raise AssertionError("Stage 8c staging-table collision was not rejected")
