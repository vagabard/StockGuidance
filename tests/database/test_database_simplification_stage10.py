import json
from dataclasses import replace

import pytest

from support.ingestion import FakeDatabase

from stockguidance.cli import preview_schema
from stockguidance.database.simplified_schema import (
	SchemaVersionError,
	initialize_simplified_schema,
	registry_digest,
)
from stockguidance.database.table_names import load_table_names
from stockguidance.database.update_schema import initialize_update_schema


RETIRED = (
	"stockguidance_imports",
	"stockguidance_archive_files",
	"stockguidance_daily_source_files",
	"stockguidance_daily_import_in_progress",
	"stockguidance_row_quality",
	"stockguidance_quality_revisions",
	"stockguidance_output_quality",
	"stockguidance_quality_scans",
	"stockguidance_data_verifications",
	"stockguidance_data_verification_samples",
	"stockguidance_migration_receipts",
	"stockguidance_migration_completion",
)


def test_target_registry_and_ddl_exclude_retired_operational_storage():
	names = load_table_names()
	assert set(names.target_tables()) == {
		"source", "sessions", "tickers", "prices", "deltas", "schema", "data_states",
		"pending_updates", "repair_metadata", "zscore_series", "zscore_sessions",
	}
	ddl = "\n".join(preview_schema.simplified_schema_statements(names, (20, 60)))
	assert not any(table in ddl for table in RETIRED)


def test_target_registry_digest_does_not_depend_on_legacy_names():
	names = load_table_names()
	legacy_renamed = replace(names, imports="fixture_imports", data_verifications="fixture_verifications")
	assert registry_digest(names) == registry_digest(legacy_renamed)


def test_fresh_target_schema_has_no_retired_operational_tables(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "target.sqlite")
	initialize_simplified_schema(source_config, database, windows=(2,))
	physical = {row[0] for row in database.sql("SELECT name FROM sqlite_master WHERE type = 'table'")}
	assert not physical.intersection(RETIRED)


def test_target_initializer_rejects_mixed_legacy_storage(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "mixed.sqlite")
	database.sql("CREATE TABLE stockguidance_imports (session_date TEXT PRIMARY KEY)")
	with pytest.raises(SchemaVersionError, match="retired operational tables"):
		initialize_simplified_schema(source_config, database, windows=(2,))


def test_legacy_initializer_rejects_versioned_target(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "target-guard.sqlite")
	initialize_simplified_schema(source_config, database, windows=(2,))
	with pytest.raises(SchemaVersionError, match="retired for the versioned simplified schema"):
		initialize_update_schema(source_config, database)


def test_preview_is_target_only(capsys):
	assert preview_schema.main(["--windows", "2"]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["schema"] == "stockguidance_simplified"
	assert not set(payload["tables"]).intersection({"imports", "data_verifications", "data_verification_samples"})
	assert not any(table in "\n".join(payload["ddl"]) for table in RETIRED)
