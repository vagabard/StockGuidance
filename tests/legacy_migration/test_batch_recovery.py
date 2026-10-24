from copy import deepcopy
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from stockguidance.market_data.identity import content_id
from stockguidance.performance import stage14_batches as batches
from stockguidance.performance.stage14 import Stage14Error, _json, _sha256, _write_json, build_zscore_manifest
from stockguidance.performance.stage14_progress import estimate


class Cursor:
	def __init__(self, cursor):
		self.cursor = cursor
	def execute(self, query, parameters=()):
		self.cursor.execute(query.replace("%s", "?"), parameters)
	def executemany(self, query, parameters):
		self.cursor.executemany(query.replace("%s", "?"), parameters)
	def fetchall(self):
		return self.cursor.fetchall()
	def __iter__(self):
		return iter(self.cursor)
	def close(self):
		self.cursor.close()


class Connection:
	def __init__(self, path):
		self.connection = sqlite3.connect(path)
	def cursor(self, **kwargs):
		return Cursor(self.connection.cursor())
	def rollback(self):
		self.connection.rollback()
	def commit(self):
		self.connection.commit()
	def close(self):
		self.connection.close()


def setup_journal(tmp_path, monkeypatch):
	tables = {window: SimpleNamespace(price=f"price_{window}", price_delta=f"delta_{window}") for window in (20, 60)}
	names = SimpleNamespace(resolve_zscore_tables=lambda windows: tables, zscore_series="definitions", zscore_sessions="metadata")
	monkeypatch.setattr(batches, "_manifest_names", lambda manifest: names)
	monkeypatch.setattr(batches, "_session_ids", lambda connection, names, sessions: {session: 1 for session in sessions})
	path = tmp_path / "fixture.sqlite"
	with sqlite3.connect(path) as connection:
		for table in ("price_20", "price_60", "delta_20", "delta_60"):
			connection.execute(f"CREATE TABLE {table} (date_id INT,ticker_id INT,zscore REAL,status INT, PRIMARY KEY(date_id,ticker_id))")
			connection.execute(f"INSERT INTO {table} VALUES (1,7,0.25,0),(2,7,9.5,0)")
		connection.execute("CREATE TABLE definitions (series_key TEXT, family TEXT, `window` INT)")
		for index, (family, window) in enumerate(((family, window) for family in ("price", "price_delta") for window in (20, 60))):
			connection.execute("INSERT INTO definitions VALUES (?,?,?)", (f"series-{index}", family, window))
		connection.execute("CREATE TABLE metadata (series_key TEXT,date_id INT,dependency_digest TEXT,availability_digest TEXT,actual_input_available INT,completed_at TEXT,row_count INT,ready_count INT,status_counts TEXT,content_digest TEXT)")
		connection.execute("INSERT INTO metadata VALUES ('series-0',1,'old-dep','availability',1,'receipt',1,1,'0:1','old-content')")
	journal = batches.BatchJournal(tmp_path / "dbinfo.id", tmp_path / "stage14")
	monkeypatch.setattr(journal, "_connection", lambda: Connection(path))
	monkeypatch.setattr(journal, "_guard", lambda connection: None)
	report = {"database_revision_after": "revision", "chunks": []}
	journal.initialize({"manifest_id": "a" * 64, "targets": [{"session": "2024-01-02"}]}, report, tmp_path / "checkpoints", 20)
	journal.checkpoints.mkdir()
	return journal, report, path


def test_partial_commits_are_restored_and_unrelated_dates_are_preserved(tmp_path, monkeypatch):
	journal, report, path = setup_journal(tmp_path, monkeypatch)
	journal.begin(0, ("2024-01-02",))
	journal.publishing()
	assert _json(journal.path)["batches"][0]["status"] == "in_progress"
	with sqlite3.connect(path) as connection:
		connection.execute("UPDATE price_20 SET zscore=123 WHERE date_id=1")
		connection.execute("DELETE FROM delta_60 WHERE date_id=1")
		connection.execute("UPDATE metadata SET content_digest='new-content'")
	checkpoint = journal.checkpoints / "2024-01-02-price-20.json"
	checkpoint.write_text("{}")
	unrelated = journal.checkpoints / "2024-01-03-price-20.json"
	unrelated.write_text("{}")
	journal.recover(report)
	with sqlite3.connect(path) as connection:
		assert connection.execute("SELECT zscore FROM price_20 WHERE date_id=1").fetchone() == (0.25,)
		assert connection.execute("SELECT zscore FROM price_20 WHERE date_id=2").fetchone() == (9.5,)
		assert connection.execute("SELECT zscore FROM delta_60 WHERE date_id=1").fetchone() == (0.25,)
		assert connection.execute("SELECT content_digest FROM metadata").fetchone() == ("old-content",)
	assert not checkpoint.exists()
	assert unrelated.exists()
	assert journal.state["batches"][0]["status"] == "pending"
	assert journal.state["batches"][0]["attempts"][-1]["status"] == "rolled_back"
	journal.recover(report)


def test_rollback_failure_is_atomic_and_remains_recoverable(tmp_path, monkeypatch):
	journal, report, path = setup_journal(tmp_path, monkeypatch)
	journal.begin(0, ("2024-01-02",))
	journal.publishing()
	with sqlite3.connect(path) as connection:
		connection.execute("UPDATE price_20 SET zscore=123 WHERE date_id=1")
		connection.execute("CREATE TRIGGER fail_restore BEFORE INSERT ON delta_60 BEGIN SELECT RAISE(ABORT,'interruption'); END")
	with pytest.raises(sqlite3.IntegrityError):
		journal.recover(report)
	assert journal.state["batches"][0]["status"] == "in_progress"
	with sqlite3.connect(path) as connection:
		assert connection.execute("SELECT zscore FROM price_20 WHERE date_id=1").fetchone() == (123,)
		connection.execute("DROP TRIGGER fail_restore")
	journal.recover(report)
	assert journal.state["batches"][0]["status"] == "pending"


def test_report_acceptance_finishes_interrupted_journal_commit_without_rollback(tmp_path, monkeypatch):
	journal, report, path = setup_journal(tmp_path, monkeypatch)
	journal.begin(0, ("2024-01-02",))
	java_report = tmp_path / "java.json"
	_write_json(java_report, {"status": "complete"})
	report["chunks"] = [{"index": 0, "sessions": ["2024-01-02"], "java_report": str(java_report)}]
	monkeypatch.setattr(journal, "_restore", lambda *args: pytest.fail("An accepted batch was rolled back"))
	journal.recover(report)
	assert journal.state["batches"][0]["status"] == "complete"


def test_before_image_drift_blocks_recovery_without_touching_database(tmp_path, monkeypatch):
	journal, report, path = setup_journal(tmp_path, monkeypatch)
	journal.begin(0, ("2024-01-02",))
	journal.publishing()
	backup = Path(journal.state["batches"][0]["attempts"][-1]["before_image"])
	backup.write_text("{}")
	monkeypatch.setattr(journal, "_connection", lambda: pytest.fail("Corrupt rollback reached the database"))
	with pytest.raises(Stage14Error, match="identity changed"):
		journal.recover(report)


def test_dependency_migration_archives_old_checkpoint_and_rejects_unrelated_changes(tmp_path):
	from legacy_migration.test_zscore_manifest import _base_manifest
	new, scope = build_zscore_manifest(_base_manifest())
	old = deepcopy(new)
	for target in old["targets"]:
		target["zscore_dependencies"] = target["zscore_dependencies"][-60:]
	old["zscore_population"]["dependency_sessions"] = 60
	old["manifest_id"] = content_id({key: value for key, value in old.items() if key != "manifest_id"})
	output = tmp_path / "zscores.json"
	manifest_path = tmp_path / "stage14/zscore-manifest.json"
	_write_json(manifest_path, old)
	state = {"manifest_id": old["manifest_id"], "manifest_sha256": _sha256(manifest_path), "database_revision_after": "revision", "configuration": {}, "scope": {}, "chunks": [{"sessions": ["2020-01-02"]}], "series": []}
	_write_json(output, state)
	result = batches.migrate_dependencies(state, old, new, output, "revision")
	assert result["manifest_id"] == new["manifest_id"]
	assert result["chunks"] == []
	assert _json(Path(result["dependency_migration"]["history"]) / "zscores.json") == state
	assert _json(manifest_path) == old
	changed = deepcopy(new)
	changed["source"] = {"unexpected": True}
	changed["manifest_id"] = content_id({key: value for key, value in changed.items() if key != "manifest_id"})
	with pytest.raises(Stage14Error, match="beyond"):
		batches.migrate_dependencies(state, old, changed, output, "revision")


def test_estimator_uses_observed_progress_and_does_not_invent_an_eta():
	assert estimate(20, 20, 100, 100)["remaining_seconds"] is None
	value = estimate(40, 20, 10, 100)
	assert value["series_per_second"] == 2
	assert value["remaining_seconds"] == 30
