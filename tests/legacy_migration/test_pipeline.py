import io
import json
from types import SimpleNamespace

import pytest

from stockguidance.performance.stage14 import Stage14Error, _output_lock
from stockguidance.performance.stage14_pipeline import JavaPipeline
from stockguidance.performance import stage14


class Process:
	def __init__(self, responses):
		self.stdin = io.StringIO()
		self.stdout = io.StringIO(responses)
		self.returncode = None
		self.terminated = False

	def poll(self):
		return self.returncode

	def wait(self, timeout):
		self.returncode = 0
		return 0

	def terminate(self):
		self.terminated = True
		self.returncode = -1


def test_one_jvm_handles_multiple_chunks_and_closes_on_exit(monkeypatch, tmp_path):
	response = json.dumps({"status": "complete", "series": []}) + "\n"
	process = Process(response * 2)
	commands = []
	def start(command, **kwargs):
		commands.append(command)
		return process
	monkeypatch.setattr("stockguidance.performance.stage14_pipeline.subprocess.Popen", start)
	with JavaPipeline(3, 1, "64g") as pipeline:
		for session in ("2024-01-02", "2024-01-03"):
			assert pipeline(tmp_path / "dbinfo.id", tmp_path / "manifest.json", (session,), tmp_path / "checkpoints", 1)["status"] == "complete"
		assert process.stdin.getvalue() == "2024-01-02\n2024-01-03\n"
	assert len(commands) == 1
	assert "-Xmx64g" in commands[0]
	assert process.stdin.closed
	assert process.stdout.closed


def test_worker_failure_is_not_accepted_as_completed_chunk(monkeypatch, tmp_path):
	monkeypatch.setattr("stockguidance.performance.stage14_pipeline.subprocess.Popen", lambda *args, **kwargs: Process('{"failed":true}\n'))
	with JavaPipeline() as pipeline:
		with pytest.raises(Stage14Error, match="rejected"):
			pipeline(tmp_path / "dbinfo.id", tmp_path / "manifest.json", ("2024-01-02",), tmp_path / "checkpoints", 1)


def test_source_cache_cannot_be_reused_for_a_different_manifest(monkeypatch, tmp_path):
	monkeypatch.setattr("stockguidance.performance.stage14_pipeline.subprocess.Popen", lambda *args, **kwargs: Process('{"status":"complete"}\n'))
	with JavaPipeline() as pipeline:
		pipeline(tmp_path / "dbinfo.id", tmp_path / "manifest.json", ("2024-01-02",), tmp_path / "checkpoints", 1)
		with pytest.raises(Stage14Error, match="different inputs"):
			pipeline(tmp_path / "dbinfo.id", tmp_path / "different.json", ("2024-01-03",), tmp_path / "checkpoints", 1)


def test_concurrent_coordinators_are_rejected_and_lock_is_reusable(tmp_path):
	output = tmp_path / "zscores.json"
	with _output_lock(output):
		with pytest.raises(Stage14Error, match="run lock"):
			with _output_lock(output):
				pytest.fail("Second coordinator acquired the same output")
	with _output_lock(output):
		pass


@pytest.mark.parametrize("workers,writers,heap", [(0, 1, "64g"), (3, 0, "64g"), (True, 1, "64g"), (3, 1, "64g --other")])
def test_invalid_worker_and_heap_settings_fail_before_starting_java(workers, writers, heap):
	with pytest.raises(Stage14Error):
		JavaPipeline(workers, writers, heap)


def fixture_context(monkeypatch):
	from legacy_migration.test_zscore_manifest import _base_manifest
	class Cursor:
		def execute(self, *args):
			pass
		def fetchall(self):
			return [(1, "AAP")]
		def close(self):
			pass
	class Connection:
		def cursor(self):
			return Cursor()
		def rollback(self):
			pass
		def close(self):
			pass
	monkeypatch.setattr(stage14, "_connect", lambda *args: Connection())
	monkeypatch.setattr(stage14, "load_database_configuration", lambda *args: SimpleNamespace(database="fixture"))
	monkeypatch.setattr(stage14, "_manifest_names", lambda *args: SimpleNamespace(tickers="fixture_tickers"))
	monkeypatch.setattr(stage14, "_active_writers", lambda *args: [])
	monkeypatch.setattr(stage14, "_database_revision", lambda *args: "stable")
	monkeypatch.setattr(stage14, "_session_ids", lambda connection, names, sessions: {session: index for index, session in enumerate(sessions)})
	monkeypatch.setattr(stage14, "_series_record", lambda connection, names, item, date_id, ticker_count: {**item, "row_count": ticker_count})
	return SimpleNamespace(stage13_manifest=_base_manifest(), decision_sha256="decision", stage12_sha256="pilot", stage13_sha256="population", stage13_manifest_sha256="manifest", snapshot_sha256="snapshot", snapshot={"cutoff": "cutoff"})


def test_bounded_resume_skips_completed_chunks_and_preserves_existing_series(monkeypatch, tmp_path):
	context = fixture_context(monkeypatch)
	calls = []
	def run_java(dbinfo, manifest, sessions, checkpoint, timeout):
		calls.append(sessions)
		return {"status": "complete", "series": [{"session": session, "family": family, "window": window} for session in sessions for family in stage14.FAMILIES for window in stage14.WINDOWS]}
	output = tmp_path / "zscores.json"
	first = stage14.run_stage14(context, tmp_path / "dbinfo.id", output, java_runner=run_java, max_chunks=1)
	second = stage14.run_stage14(context, tmp_path / "dbinfo.id", output, java_runner=run_java, max_chunks=2)
	assert len(calls) == 2
	assert not set(calls[0]) & set(calls[1])
	assert len(second["chunks"]) == 2
	assert second["series"][:80] == first["series"]
	assert second["coverage"]["completed_sessions"] == 40
	assert second["coverage"]["completed_series"] == 160


def test_changed_inputs_during_publication_do_not_advance_durable_report(monkeypatch, tmp_path):
	context = fixture_context(monkeypatch)
	revisions = iter(("before", "before", "changed"))
	monkeypatch.setattr(stage14, "_database_revision", lambda *args: next(revisions))
	def run_java(dbinfo, manifest, sessions, checkpoint, timeout):
		return {"status": "complete", "series": [{"session": session, "family": family, "window": window} for session in sessions for family in stage14.FAMILIES for window in stage14.WINDOWS]}
	output = tmp_path / "zscores.json"
	with pytest.raises(Stage14Error, match="not accepted"):
		stage14.run_stage14(context, tmp_path / "dbinfo.id", output, java_runner=run_java, max_chunks=1)
	assert not output.exists()


def test_duplicate_results_cannot_hide_a_missing_series(monkeypatch, tmp_path):
	context = fixture_context(monkeypatch)
	def run_java(dbinfo, manifest, sessions, checkpoint, timeout):
		series = [{"session": session, "family": family, "window": window} for session in sessions for family in stage14.FAMILIES for window in stage14.WINDOWS]
		series[-1] = series[0]
		return {"status": "complete", "series": series}
	output = tmp_path / "zscores.json"
	with pytest.raises(Stage14Error, match="duplicate, missing"):
		stage14.run_stage14(context, tmp_path / "dbinfo.id", output, java_runner=run_java, max_chunks=1)
	assert not output.exists()


def test_pipeline_rejects_missing_delta_predecessor_before_touching_mysql_or_durable_files(monkeypatch, tmp_path):
	context = fixture_context(monkeypatch)
	def unexpected_connection(*args):
		pytest.fail("Invalid dependency manifest reached MySQL")
	monkeypatch.setattr(stage14, "_connect", unexpected_connection)
	build = stage14.build_zscore_manifest
	def broken_manifest(base):
		manifest, scope = build(base)
		manifest["targets"][-1]["zscore_dependencies"] = manifest["targets"][-1]["zscore_dependencies"][-60:]
		return manifest, scope
	monkeypatch.setattr(stage14, "build_zscore_manifest", broken_manifest)
	output = tmp_path / "zscores.json"
	with pytest.raises(Stage14Error, match="needs 61 raw sessions"):
		stage14.run_stage14(context, tmp_path / "dbinfo.id", output, pipeline_configuration={"pipeline": "shared_java_db_copy_v1"})
	assert not output.exists()
	assert not (tmp_path / "stage14/zscore-manifest.json").exists()


def test_pipeline_dependency_gate_accepts_sufficient_predecessor_context():
	from legacy_migration.test_zscore_manifest import _base_manifest
	manifest, _ = stage14.build_zscore_manifest(_base_manifest())
	manifest["targets"][-1]["zscore_dependencies"] = [item["session"] for item in manifest["files"]]
	stage14._validate_pipeline_dependencies(manifest)
