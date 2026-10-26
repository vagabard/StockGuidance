from dataclasses import replace
from datetime import date, datetime, timezone
import json

import pytest

from support.ingestion import FakeDatabase

from stockguidance.database.repositories.simplified import (
	ActiveOperationError,
	DataStateRepository,
	IncompatibleImportError,
	PendingUpdateRepository,
	SessionCompletionRepository,
	SourceConfigurationRepository,
)
from stockguidance.database.simplified_contract import (
	ContractError,
	ClearScope,
	FailureDetail,
	OperationKind,
	PendingScope,
	WorkUnit,
)
from stockguidance.database.simplified_schema import SchemaVersionError, initialize_simplified_schema
from stockguidance.database.table_names import TableRole, load_table_names
from stockguidance.market_data.identity import data_configuration_key


def _scope(source, *, session_date: date = date(2024, 7, 1)) -> PendingScope:
	return PendingScope(
		1, OperationKind.INITIAL_IMPORT, data_configuration_key(source.data_configuration), source.calendar_id,
		datetime(2024, 7, 8, 19, 30, tzinfo=timezone.utc), datetime(2024, 7, 8, 19, 30, tzinfo=timezone.utc),
		(), ClearScope(), (WorkUnit("raw_session", 1, None, session_date),), (),
	)


def test_target_schema_is_explicit_and_repositories_are_independent(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "simplified.sqlite")
	initialize_simplified_schema(source_config, database, windows=(20,))

	assert database.sql("SELECT code FROM stockguidance_data_states ORDER BY id") == [("good",), ("bad",), ("missing",), ("interpolated",)]
	assert database.sql("SELECT calendar_id FROM stockguidance_source WHERE id = 1") == [("XNYS",)]
	assert DataStateRepository(source_config, database).load()[1].value == "good"

	configuration = SourceConfigurationRepository(source_config, database).load()
	assert configuration.configuration_key == data_configuration_key(source_config.data_configuration)
	assert configuration.source_key != configuration.configuration_key


def test_registry_customization_is_used_by_target_schema(source_config, tmp_path):
	base = load_table_names()
	registry = replace(base, schema="fixture_schema", data_states="fixture_states", pending_updates="fixture_pending", repair_metadata="fixture_repairs")
	database = FakeDatabase(tmp_path / "custom.sqlite")
	initialize_simplified_schema(source_config, database, windows=(20,), table_names=registry)
	assert database.sql("SELECT COUNT(*) FROM fixture_states") == [(4,)]
	assert database.sql("SELECT COUNT(*) FROM fixture_schema") == [(1,)]


def test_invalid_state_reference_is_rejected_by_database(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "state.sqlite")
	initialize_simplified_schema(source_config, database, windows=(20,))
	database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (date(2024, 7, 1).isoformat(),))
	database.sql("INSERT INTO ticker_symbol (label) VALUES (?)", ("AAA",))
	with pytest.raises(ValueError):
		DataStateRepository(source_config, database).require(999)


def test_singleton_and_incompatible_import_are_rejected(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "singleton.sqlite")
	initialize_simplified_schema(source_config, database, windows=(20,))
	repository = SourceConfigurationRepository(source_config, database)
	assert repository.ensure().configuration_key == repository.load().configuration_key
	database.sql("UPDATE stockguidance_source SET market = ? WHERE id = 1", ("wrong-market",))
	with pytest.raises(IncompatibleImportError):
		repository.assert_compatible()

	database.sql("UPDATE stockguidance_source SET market = ? WHERE id = 1", (source_config.data_configuration.market,))
	database.sql("UPDATE stockguidance_schema_version SET schema_version = ? WHERE id = 1", (99,))
	with pytest.raises(SchemaVersionError):
		initialize_simplified_schema(source_config, database, windows=(20,))


def test_pending_scope_is_immutable_and_transitions_are_atomic(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "pending.sqlite")
	initialize_simplified_schema(source_config, database, windows=(20,))
	repository = PendingUpdateRepository(source_config, database)
	scope = _scope(source_config)
	created = datetime(2024, 7, 8, 19, 30, tzinfo=timezone.utc)
	pending = repository.register(scope, created)
	assert pending.scope == scope
	with pytest.raises(ActiveOperationError):
		repository.register(scope, created)
	repository.record_failure(pending.operation_id, FailureDetail("fixture", "safe detail", created), created)
	failed = repository.load_active()
	assert failed is not None and failed.scope == scope and failed.failure is not None
	repository.mark_cleared(pending.operation_id, created)
	cleared = repository.load_active()
	assert cleared is not None and cleared.affected_rows_cleared and cleared.scope == scope
	assert database.sql("SELECT scope FROM stockguidance_pending_updates WHERE id = ?", (pending.operation_id,))[0][0] == scope.to_json()
	repository.delete_completed(pending.operation_id, created)
	assert repository.load_active() is None


def test_session_completion_keeps_raw_and_delta_independent(source_config, tmp_path):
	database = FakeDatabase(tmp_path / "completion.sqlite")
	initialize_simplified_schema(source_config, database, windows=(20,))
	database.sql("INSERT INTO transaction_dates (transaction_date) VALUES (?)", (date(2024, 7, 1).isoformat(),))
	repository = SessionCompletionRepository(source_config, database)
	completed_at = datetime(2024, 7, 8, 19, 30, tzinfo=timezone.utc)
	repository.set_raw_complete(1, "a" * 64, completed_at, completed_at)
	loaded = repository.load(1)
	assert loaded.raw_complete and not loaded.delta_complete
	repository.set_delta_complete(1, completed_at)
	assert repository.load(1).delta_complete
	repository.clear_raw(1)
	cleared = repository.load(1)
	assert not cleared.raw_complete and cleared.delta_complete


def test_scope_codec_rejects_duplicate_fields_and_unknown_versions():
	with pytest.raises(ContractError):
		PendingScope.from_json('{"scope_version":1,"scope_version":1}')
	with pytest.raises(ContractError):
		PendingScope.from_json(json.dumps({"scope_version": 2}))
