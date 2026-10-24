from decimal import Decimal
import sqlite3

import pytest

from stockguidance.database.table_names import load_table_names
from stockguidance.legacy_migration.precision import ExactArchiveBar
from stockguidance.performance.candidates import DisposableTargetGuard, _exact_derived_rows, create_disposable_database, disposable_schema_statements


def test_disposable_target_guard_rejects_source_and_configuration_paths(tmp_path):
	data_root = tmp_path / "data"
	data_root.mkdir()
	dbinfo = tmp_path / "dbinfo.id"
	dbinfo.write_text("{}", encoding="utf-8")
	with pytest.raises(ValueError, match="immutable source"):
		DisposableTargetGuard(data_root / "candidate.sqlite", data_root=data_root, dbinfo_file=dbinfo).validate()
	with pytest.raises(ValueError, match="configuration"):
		DisposableTargetGuard(dbinfo, data_root=data_root, dbinfo_file=dbinfo).validate()


def test_disposable_schema_uses_registry_and_keeps_decimal_columns_as_text(tmp_path):
	names = load_table_names()
	statements = disposable_schema_statements(names, (20, 60))
	assert any("COLLATE BINARY" in statement for statement in statements)
	assert any("volume TEXT" in statement and "vwap TEXT" in statement for statement in statements)
	assert any("stage6_zscore_stage" in statement for statement in statements)
	metadata = create_disposable_database(tmp_path / "candidate.sqlite", names)
	assert metadata["production_target"] is False
	with sqlite3.connect(tmp_path / "candidate.sqlite") as connection:
		tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
	assert names.prices in tables
	assert "zscores_price_20" in tables
	assert "zscores_price_delta_60" in tables


def test_exact_derived_oracle_preserves_fractional_volume_and_nullable_fields():
	def bar(ticker, volume, close, vwap=None):
		return ExactArchiveBar(ticker, Decimal("10.00000000"), Decimal("12.00000000"), Decimal("9.00000000"), Decimal(close), Decimal(volume), None if vwap is None else Decimal(vwap), 10)

	prior = (bar("AAP", "100.40000000", "10.00000000"), bar("AAp", "200.00000000", "10.00000000"))
	current = (bar("AAP", "101.40000000", "11.00000000"), bar("AAp", "200.40000000", "11.00000000"))
	following = (bar("AAP", "102.40000000", "11.50000000"), bar("AAp", "201.40000000", "11.50000000"))
	rows = _exact_derived_rows(prior, current, following)
	assert {row.ticker for row in rows} == {"AAP", "AAp"}
	assert rows[0].values[4] == pytest.approx(101.4 / 100.4)
