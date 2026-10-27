from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

from support.ingestion import FakeDatabase

from stockguidance.config.source import SourceConfig
from stockguidance.database.numerical_rebuild import SimplifiedNumericalRebuildRunner
from stockguidance.database.operation_recovery import RebuildResult
from stockguidance.database.simplified_contract import ClearScope, OperationKind, PendingScope, PendingUpdate, WorkUnit
from stockguidance.database.simplified_schema import initialize_simplified_schema
from stockguidance.database.write_connections import WriteConnection, WriteConnectionFactory
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.workflows.market_lock import MaintenanceWriterLock


NOW = datetime(2024, 7, 10, 23, tzinfo=timezone.utc)


class _NoRawRunner:
	def rebuild(self, _operation: PendingUpdate, _connection: WriteConnection) -> RebuildResult:
		return RebuildResult()


class _Calendar:
	calendar_id = "XNYS"


class _AlreadyCompleteRunner(SimplifiedNumericalRebuildRunner):
	def _delta_complete_for_resume(self, _unit: WorkUnit, _connection: WriteConnection) -> bool:
		return True

	def _delta_is_non_ready(self, _unit: WorkUnit, _operation: PendingUpdate, _connection: WriteConnection) -> bool:
		return False

	def _rebuild_zscore(self, _unit: WorkUnit, _operation: PendingUpdate, _connection: WriteConnection) -> Any:
		return None


def _resume_operation(source: SourceConfig) -> PendingUpdate:
	delta = WorkUnit("delta_session", 1, 1, date(2024, 7, 1))
	zscore = WorkUnit("zscore_session", 1, None, date(2024, 7, 1), "a" * 64, 20)
	scope = PendingScope(
		1,
		OperationKind.RECOVERY,
		data_configuration_key(source.data_configuration),
		"XNYS",
		NOW,
		NOW,
		(),
		ClearScope(),
		(delta, zscore),
		(),
	)
	return PendingUpdate(1, scope, NOW, NOW, True, None)


def test_resume_reports_every_durable_unit_to_the_coordinator(source_config: SourceConfig, tmp_path: Path) -> None:
	database = FakeDatabase(tmp_path / "resume-evidence.sqlite")
	initialize_simplified_schema(source_config, database, windows=(20,))
	runner = _AlreadyCompleteRunner(source_config, cast(Any, _Calendar()), _NoRawRunner())  # type: ignore[arg-type]
	result = runner.rebuild(_resume_operation(source_config), database(tmp_path / "ignored"))

	assert set(result.committed_units) == set(_resume_operation(source_config).scope.rebuild)
	assert result.failed_units == ()
	assert result.deferred_units == ()


class _LockCursor:
	def __init__(self) -> None:
		self.calls: list[tuple[str, tuple[object, ...]]] = []
		self.last_query = ""

	def execute(self, query: str, parameters: tuple[object, ...]) -> None:
		self.calls.append((query, parameters))
		self.last_query = query

	def fetchall(self) -> list[tuple[object, ...]]:
		if "CONNECTION_ID" in self.last_query:
			return [(42,)]
		return [(1,)]

	def close(self) -> None:
		return None


class _LockConnection:
	connection = object()

	def __init__(self, cursor: _LockCursor) -> None:
		self._cursor = cursor

	def cursor(self) -> _LockCursor:
		return self._cursor

	def commit(self) -> None:
		return None

	def rollback(self) -> None:
		return None

	def close(self) -> None:
		return None


def test_lock_timeout_is_parameterized_for_waiting_writers(source_config: SourceConfig) -> None:
	cursor = _LockCursor()
	connection = _LockConnection(cursor)
	factory = cast(WriteConnectionFactory, lambda _path: connection)
	with MaintenanceWriterLock(source_config, factory, timeout_seconds=17):
		pass

	assert cursor.calls[0] == ("SELECT GET_LOCK(%s, %s)", (MaintenanceWriterLock.NAME, 17))
