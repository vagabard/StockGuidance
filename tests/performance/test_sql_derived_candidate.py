import json

from stockguidance.database.table_names import load_table_names
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS
from stockguidance.performance.sql_derived_candidate import (
	ROUTINE_NAME,
	STAGE_TABLE,
	sql_derived_objects,
	sql_derived_schema_statements,
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
		"authoritative_registry": {"tables": names.tables(), "zscore_templates": names.templates()},
		"corrected_schema_hash": content_id(columns),
		"inputs": {"precision_report": str(precision)},
	}


def test_sql_derived_candidate_is_registry_bound_and_set_based(tmp_path):
	manifest = _manifest(tmp_path)
	statement = sql_derived_schema_statements(manifest)[0]
	routine = sql_derived_objects(manifest)[0]

	assert STAGE_TABLE in statement
	assert ROUTINE_NAME in routine
	assert "WITH base_rows AS" in routine
	assert "ROW_NUMBER() OVER" in routine
	assert "DELETE FROM" in routine
	assert "INSERT INTO" in routine
	assert " COMMIT;" not in routine.upper()
	for column in DERIVED_COLUMNS:
		assert column in routine


def test_sql_derived_staging_identity_is_not_a_registry_table(tmp_path):
	manifest = _manifest(tmp_path)
	manifest["authoritative_registry"]["tables"]["deltas"] = STAGE_TABLE

	try:
		sql_derived_schema_statements(manifest)
	except ValueError as error:
		assert str(error) == "Stage 8b staging table collides with the selected registry"
	else:
		raise AssertionError("Stage 8b staging table collision was not rejected")
