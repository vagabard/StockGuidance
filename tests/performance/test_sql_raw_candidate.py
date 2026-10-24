import json

import pytest

from stockguidance.database.table_names import load_table_names
from stockguidance.market_data.identity import content_id
from stockguidance.performance.sql_raw_candidate import STAGE_TABLE, _fits_decimal, sql_raw_objects, sql_raw_schema_statements


def _manifest(tmp_path):
	names = load_table_names()
	columns = {"open": "DECIMAL(28,8)", "high": "DECIMAL(28,8)", "low": "DECIMAL(28,8)", "close": "DECIMAL(28,8)", "volume": "DECIMAL(20,8)", "vwap": "DECIMAL(42,22)"}
	precision = tmp_path / "precision.json"
	precision.write_text(json.dumps({"schema_selection": {"schema_hash": content_id(columns), "columns": columns, "ddl": columns}}), encoding="utf-8")
	return {"authoritative_registry": {"tables": names.tables(), "zscore_templates": names.templates()}, "corrected_schema_hash": content_id(columns), "inputs": {"precision_report": str(precision)}}


def test_sql_raw_objects_are_set_based_and_registry_bound(tmp_path):
	manifest = _manifest(tmp_path)
	manifest["authoritative_registry"]["tables"]["tickers"] = "custom_tickers"
	statement = sql_raw_schema_statements(manifest)[0]
	routine = sql_raw_objects(manifest)[0]
	assert STAGE_TABLE in statement
	assert "ROW_NUMBER() OVER" in routine
	assert "INSERT INTO" in routine and "ON DUPLICATE KEY UPDATE" in routine
	assert " COMMIT;" not in routine.upper()
	assert "`custom_tickers`" in routine


@pytest.mark.parametrize("value,definition,expected", [
		("1702500000000000.1", "DECIMAL(28,8)", True),
		("0.123456789", "DECIMAL(28,8)", False),
		("61812.9999", "DECIMAL(20,8)", True),
		("170250000000000000000000000000", "DECIMAL(28,8)", False),
])
def test_sql_loader_rejects_precision_loss_before_mysql(value, definition, expected):
	from decimal import Decimal

	assert _fits_decimal(Decimal(value), definition) is expected
