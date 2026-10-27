from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from stockguidance.cli.migrate_database import parser
from stockguidance.database.migration_sql import after, bounded, metadata_equal, numeric_difference, status_counts
from stockguidance.database.simplified_migration import verify_backup_manifest


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
