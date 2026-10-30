from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from stockguidance.cli.migrate_database import parser
from stockguidance.database.migration_sql import Copy, SQLMigration, after, bounded, metadata_equal, numeric_difference, status_counts
from stockguidance.database.simplified_migration import verify_backup_manifest


def test_heartbeat_reports_session_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
	messages: list[str] = []
	migration = SQLMigration.__new__(SQLMigration)
	migration.owner = SimpleNamespace(progress=messages.append)
	migration.activity = "completion:sessions session=12"
	migration.activity_started = 10.0
	migration.started = 0.0
	migration.stop = MagicMock()
	migration.stop.wait.side_effect = [False, True]
	monkeypatch.setattr("stockguidance.database.migration_sql.time.monotonic", lambda: 25.0)
	migration.heartbeat()
	assert messages == ["completion:sessions session=12: working; session_elapsed=15.0s"]


@pytest.mark.parametrize("has_receipt", [False, True])
def test_completion_timing_preserves_checkpoint_and_commit(has_receipt: bool) -> None:
	from datetime import datetime
	from unittest.mock import call

	messages: list[str] = []
	migration = SQLMigration.__new__(SQLMigration)
	migration.owner = SimpleNamespace(progress=messages.append)
	migration.connection = MagicMock()
	migration.migration_time = datetime(2024, 1, 1)
	migration.target = lambda name: name
	migration.table = lambda role: str(role)
	migration.execute = MagicMock()
	migration.rows = MagicMock(return_value=[(12, "revision", datetime(2024, 1, 1), has_receipt)])
	migration.scalar = MagicMock(return_value=0)
	migration.checkpoint = MagicMock(side_effect=[([], 0, False), ([12], 1, True)])
	migration.assert_lock = MagicMock()
	migration.save = MagicMock()
	migration.hook = MagicMock()
	migration.check_warnings = MagicMock()
	migration.expected_delta_sql = MagicMock(return_value=("SELECT 1", ()))
	migration._completion()
	migration.save.assert_called_once_with("completion:sessions", [12], 1, False)
	migration.connection.commit.assert_called_once_with()
	assert migration.hook.call_args_list == [call("before_commit", "completion:sessions"), call("after_commit", "completion:sessions")]
	assert len(messages) == 1
	assert messages[0].startswith("completion:sessions rows=1 last_key=[12] session_elapsed=")
	assert messages[0].endswith("s committed")
	statements = [call.args[0] for call in migration.execute.call_args_list]
	assert any(sql.startswith("DROP TEMPORARY TABLE IF EXISTS") for sql in statements) == has_receipt
	assert any(sql.startswith("CREATE TEMPORARY TABLE") for sql in statements) == has_receipt
	assert not any(sql.startswith("DELETE FROM `stage12_expected_delta`") for sql in statements)


@pytest.mark.parametrize("verify", [False, True])
def test_completion_rotates_only_after_commits(verify: bool, monkeypatch: pytest.MonkeyPatch) -> None:
	from datetime import datetime

	monkeypatch.setattr("stockguidance.database.migration_sql.COMPLETION_CONNECTION_SESSIONS", 2)
	messages: list[str] = []
	migration = SQLMigration.__new__(SQLMigration)
	migration.owner = SimpleNamespace(progress=messages.append)
	migration.connection = MagicMock()
	migration.migration_time = datetime(2024, 1, 1)
	migration.target = lambda name: name
	migration.table = lambda role: str(role)
	migration.execute = MagicMock()
	row = lambda date: (date, None, None, False)
	if verify:
		migration.rows = MagicMock(side_effect=[[row(12)], [(0, None, None, None, 0, None)], [row(13)], [(0, None, None, None, 0, None)]])
	else:
		migration.rows = MagicMock(side_effect=[[row(12)], [row(13)]])
	migration.scalar = MagicMock(return_value=0)
	migration.checkpoint = MagicMock(side_effect=[([], 7, False), ([12], 8, False), ([13], 9, True)])
	migration.assert_lock = MagicMock()
	migration.save = MagicMock()
	migration.hook = MagicMock()
	migration.rotate_completion_connection = MagicMock(side_effect=lambda: assert_two_commits(migration.connection))
	migration._completion(verify=verify)
	migration.rotate_completion_connection.assert_called_once_with()
	assert migration.checkpoint.call_count == 3
	assert any("reset after 2 committed sessions" in message for message in messages)


def assert_two_commits(connection: MagicMock) -> None:
	assert connection.commit.call_count == 2


@pytest.mark.parametrize("failure", ["lock", "work"])
def test_completion_failure_rolls_back_worker_and_restores_lock_connection(failure: str) -> None:
	migration = SQLMigration.__new__(SQLMigration)
	lock_connection, lock_cursor, worker, worker_cursor = (MagicMock() for _ in range(4))
	migration.connection, migration.cursor = lock_connection, lock_cursor
	migration.open_completion_connection = MagicMock(return_value=(worker, worker_cursor))
	migration.assert_lock = MagicMock(side_effect=RuntimeError("lost lock") if failure == "lock" else None)
	migration._completion = MagicMock(side_effect=RuntimeError("interrupted"))
	with pytest.raises(RuntimeError):
		migration.completion()
	worker.rollback.assert_called_once_with()
	worker.close.assert_called_once_with()
	worker_cursor.close.assert_called_once_with()
	assert migration.connection is lock_connection and migration.cursor is lock_cursor
	assert migration.lock_cursor is None


def test_completion_setup_failure_restores_lock_connection() -> None:
	migration = SQLMigration.__new__(SQLMigration)
	lock_connection, lock_cursor = MagicMock(), MagicMock()
	migration.connection, migration.cursor = lock_connection, lock_cursor
	migration.open_completion_connection = MagicMock(side_effect=RuntimeError("connection unavailable"))
	with pytest.raises(RuntimeError, match="connection unavailable"):
		migration.completion()
	assert migration.connection is lock_connection and migration.cursor is lock_cursor
	assert migration.lock_cursor is None
	lock_connection.close.assert_not_called()


def test_completion_connection_closes_on_wrong_server() -> None:
	migration = SQLMigration.__new__(SQLMigration)
	connection, cursor = MagicMock(), MagicMock()
	connection.cursor.return_value = cursor
	cursor.fetchall.return_value = [("other-server",)]
	migration.owner = SimpleNamespace(target_connect=lambda _: connection, target=SimpleNamespace(database_config="config"))
	migration.server_uuid = "expected-server"
	with pytest.raises(Exception, match="different MySQL server"):
		migration.open_completion_connection()
	cursor.close.assert_called_once_with()
	connection.close.assert_called_once_with()


def test_filtered_extra_validation_places_filter_in_where_clause() -> None:
	migration = SQLMigration.__new__(SQLMigration)
	migration.target = lambda name: f"`stock_data`.`{name}`"
	migration.source = lambda name: f"`stockforecasting`.`{name}`"
	migration.connection = MagicMock()
	migration.owner = SimpleNamespace(progress=lambda _: None)
	spec = Copy("stockguidance_zscore_sessions", ("series_key", "date_id"), (), (), source_filter=(
		"s.series_key IN (SELECT z.series_key FROM `stockforecasting`.`stockguidance_zscore_series` z WHERE z.family='price')"))
	migration.checkpoint = MagicMock(side_effect=[([], 0, True), ([], 0, False), (["a" * 64, 1], 1, True)])
	migration.assert_lock = MagicMock()
	migration.boundary = MagicMock(return_value=(["a" * 64, 1], 1))
	migration.scalar = MagicMock(return_value=0)
	migration.rows = MagicMock()
	migration.save = MagicMock()
	migration.hook = MagicMock()
	migration.progress = MagicMock()
	migration.validate_copy(spec)
	query = migration.scalar.call_args.args[0]
	assert "LEFT JOIN `stockforecasting`.`stockguidance_zscore_sessions` t ON" in query
	assert "WHERE (((" in query and "AND (s.series_key IN (SELECT z.series_key" in query
	assert "ON AND " not in query
	assert "WHERE (AND " not in query


def test_composite_key_batches_visit_sparse_keys_once() -> None:
	# Different orderings matter: raw/deltas use ticker,date; z-scores date,ticker.
	keys = [(1, 2), (1, 40000), (8, 1), (8, 9), (90000, 1), (90000, 3), (999999, 2)]
	with sqlite3.connect(":memory:") as c:
		c.execute("CREATE TABLE observations (ticker_id INTEGER, date_id INTEGER, PRIMARY KEY(ticker_id,date_id))")
		c.executemany("INSERT INTO observations VALUES (?,?)", keys)
		for ordering in (("ticker_id", "date_id"), ("date_id", "ticker_id")):
			low: tuple[int, ...] = ()
			seen = []
			while True:
				where, parameters = after(ordering, low)
				fields = ",".join(ordering)
				batch = c.execute(f"SELECT {fields} FROM observations s WHERE {where.replace('%s', '?')} ORDER BY {fields} LIMIT 3", parameters).fetchall()
				if not batch:
					break
				high = batch[-1]
				where, parameters = bounded(ordering, low, high)
				copied = c.execute(f"SELECT {fields} FROM observations s WHERE {where.replace('%s', '?')} ORDER BY {fields}", parameters).fetchall()
				assert copied == batch
				seen.extend(copied)
				low = high
			expected = sorted(keys if ordering[0] == "ticker_id" else [(d, t) for t, d in keys])
			assert seen == expected


@pytest.mark.parametrize("left,right,different", [(None, None, False), (None, 0, True), (0, None, True), (1, 1, False), (1, 1 + 1e-11, False), (1, 1.01, True), (0, 1e-13, False)])
def test_sql_numeric_reference_tolerance_preserves_nulls(left: float | None, right: float | None, different: bool) -> None:
	with sqlite3.connect(":memory:") as c:
		c.create_function("GREATEST", -1, lambda *args: max(args))
		c.execute("CREATE TABLE numbers (a REAL,b REAL)")
		c.execute("INSERT INTO numbers VALUES (?,?)", (left, right))
		value = c.execute("SELECT CASE WHEN " + numeric_difference("a", "b") + " THEN 1 ELSE 0 END FROM numbers").fetchone()[0]
		assert bool(value) == different


def test_metadata_comparison_is_structural_and_status_counts_are_normalized() -> None:
	assert status_counts("0:2,1:3") == {"0": 2, "1": 3}
	assert status_counts('{"1":3,"0":2}') == {"0": 2, "1": 3}
	assert metadata_equal(("status_counts",), ('{"0":2,"1":3}',), ('{"1":3,"0":2}',))
	assert not metadata_equal(("effective_values",), ('["1.00",null]',), ('["0",null]',))


def test_backup_verification_streams_without_read_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	backup, restore = tmp_path / "backup.sql", tmp_path / "restore.sql"
	for p in (backup, restore):
		p.write_bytes(b"fixture")
	digest = hashlib.sha256(b"fixture").hexdigest()
	manifest = tmp_path / "manifest.json"
	manifest.write_text(json.dumps({"schema_version": 1, "backup_path": str(backup), "restore_path": str(restore), "backup_sha256": digest, "restore_sha256": digest, "restore_verified": True}), encoding="utf-8")
	def forbidden(path: Path) -> bytes:
		raise AssertionError("whole-file allocation")
	monkeypatch.setattr(Path, "read_bytes", forbidden)
	assert verify_backup_manifest(manifest).backup_sha256 == digest


@pytest.mark.parametrize("phase", ["plan", "apply", "status", "validate", "cutover", "retire"])
def test_operator_commands_accept_explicit_batch_size(phase: str) -> None:
	a = parser().parse_args([phase, "--legacy-dbinfo-file", "legacy.json", "--target-dbinfo-file", "target.json", "--batch-size", "10000"])
	assert a.phase == phase and a.batch_size == 10000
