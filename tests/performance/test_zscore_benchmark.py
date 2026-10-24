from stockguidance.performance.stage9_zscores import (
	BACKENDS,
	BLOCKS,
	FAMILIES,
	SUPPORTED_WINDOWS,
	_series_jobs,
	_target_sessions,
	aggregate_stage9_study,
)


def _manifest():
	return {
		"target_blocks": {"fixed_25": ["2024-01-02", "2024-01-03"], "chronological_20": ["2024-01-04"]},
		"targets": [
			{"session": "2024-01-02", "roles": ["fixed_25"], "zscore_dependencies": ["2023-12-01", "2024-01-02"]},
			{"session": "2024-01-03", "roles": ["fixed_25"], "zscore_dependencies": ["2023-12-04", "2024-01-03"]},
			{"session": "2024-01-04", "roles": ["chronological_20"], "zscore_dependencies": ["2023-12-05", "2024-01-04"]},
		],
	}


def _report(status="complete"):
	return {
		"status": status,
		"manifest": {"id": "manifest", "path": "manifest.json"},
		"configurations": {
			f"{backend}-p1": {"backend": backend, "status": "measured", "warm_median_seconds": index + 1, "throughput_rows_per_second": 100 - index}
			for index, backend in enumerate(BACKENDS)
		},
	}


def test_stage9c_jobs_cover_all_series_per_target():
	j = _series_jobs(("2024-01-02", "2024-01-03"))
	assert len(j) == 2 * len(FAMILIES) * len(SUPPORTED_WINDOWS)
	assert j[0] == ("2024-01-02", "price", 20)
	assert j[-1] == ("2024-01-03", "price_delta", 60)


def test_stage9c_target_blocks_require_role_and_terminated_dependencies():
	manifest = _manifest()
	assert _target_sessions(manifest, "fixed_25") == ("2024-01-02", "2024-01-03")
	assert _target_sessions(manifest, "chronological_20") == ("2024-01-04",)
	manifest["targets"][0]["zscore_dependencies"][-1] = "2024-01-01"
	try:
		_target_sessions(manifest, "fixed_25")
	except ValueError as error:
		assert "target-terminated" in str(error)
	else:
		raise AssertionError("Invalid z-score dependency termination was accepted")


def test_stage9c_aggregate_requires_all_three_substages(tmp_path):
	complete = _report()
	study = aggregate_stage9_study(complete, complete, complete, tmp_path)
	assert study["status"] == "complete"
	assert len(study["comparison_table"]) == 9
	assert {row["workload"] for row in study["comparison_table"]} == {"raw_adoption_repair", "derived_computation_persistence", "zscores_persistence"}

	partial = _report("incomplete")
	study = aggregate_stage9_study(complete, complete, partial, tmp_path)
	assert study["status"] == "incomplete"
	assert all(row["status"] == "measured" for row in study["comparison_table"] if row["workload"] != "zscores_persistence")
	assert all(row["status"] == "measured" for row in study["comparison_table"] if row["workload"] == "zscores_persistence")


def test_streaming_digest_preserves_canonical_string_bytes():
	from stockguidance.market_data.identity import content_id
	from stockguidance.performance.zscore_digest import TextDigest
	lines = ['plain', 'quote"slash\\', 'caf\u00e9', 'control\t\r\n']
	digest = TextDigest()
	for line in lines:
		digest.append(line)
	assert digest.hexdigest() == content_id("\n".join(lines) + "\n")
	batched = TextDigest()
	batched.append_many(lines)
	assert batched.hexdigest() == digest.hexdigest()


def test_archive_validation_cache_is_worker_local_and_strict_by_default():
	from types import SimpleNamespace
	from stockguidance.performance.sql_zscore_candidate import SQLZScoreCandidate
	verified = []
	candidate = object.__new__(SQLZScoreCandidate)
	candidate.dependencies = ("2024-01-02", "2024-01-03")
	candidate.files = {session: {"valid": True} for session in candidate.dependencies}
	candidate.raw = SimpleNamespace(_archive=verified.append)
	cache = set()
	candidate._verify_source_identities(cache)
	candidate._verify_source_identities(cache)
	assert verified == list(candidate.dependencies)
	candidate._verify_source_identities(set())
	assert verified == list(candidate.dependencies) * 2
	candidate._verify_source_identities()
	assert verified == list(candidate.dependencies) * 3


def test_progress_retains_committed_series(tmp_path):
	import json
	from stockguidance.performance.stage9_zscores import _write_progress
	series = [{"session": "2024-01-02", "committed": True}]
	_write_progress(tmp_path, "python", series)
	assert json.loads((tmp_path / "progress.json").read_text()) == series


def test_report_fingerprints_include_kernels_and_validation_dependencies():
	from stockguidance.performance.stage9_zscores import _file_hashes
	from pathlib import Path
	hashes = _file_hashes(Path(__file__).resolve().parents[2])
	assert "src/stockguidance/zscores/vectorized.py" in hashes
	assert "java/src/main/java/stockguidance/migration/ZScoreVectorKernel.java" in hashes
	assert "src/stockguidance/legacy_migration/precision.py" in hashes


def test_compact_profile_preserves_regression_and_consecutive_tail():
	from stockguidance.performance.stage9_zscores import _selected_sessions

	fixed = [f"2000-01-{index:02d}" for index in range(1, 26)]
	fixed[15] = "2018-08-29"
	chronological = [f"2026-04-{index:02d}" for index in range(1, 21)]
	manifest = {"target_blocks": {"fixed_25": fixed, "chronological_20": chronological}}
	manifest["targets"] = [{"session": session, "roles": [block], "zscore_dependencies": [session]} for block, sessions in manifest["target_blocks"].items() for session in sessions]
	selected = _selected_sessions(manifest, "compact")
	assert selected["fixed_25"] == tuple(fixed[index] for index in (0, 6, 15, 18, 24))
	assert selected["chronological_20"] == tuple(chronological[-3:])
	assert _selected_sessions(manifest, "full")["fixed_25"] == tuple(fixed)


def _fake_runner(monkeypatch):
	from types import SimpleNamespace
	from stockguidance.performance import stage9_zscores as runner

	manifest = _manifest()
	manifest.update(manifest_id="manifest", repetitions={"candidate_orders": [list(BACKENDS), ["java", "sql", "python"], ["sql", "python", "java"], list(BACKENDS)]})
	lock_cursor = SimpleNamespace(execute=lambda *args: None, fetchone=lambda: (1,), close=lambda: None)
	target = SimpleNamespace(prepare=lambda: {"version": "test"}, connect=lambda: SimpleNamespace(close=lambda: None, cursor=lambda: lock_cursor))
	monkeypatch.setattr(runner, "load_manifest", lambda path: manifest)
	monkeypatch.setattr(runner, "_file_hash", lambda path: "hash")
	monkeypatch.setattr(runner, "_file_hashes", lambda path: {"candidate": "hash"})
	monkeypatch.setattr(runner, "_target_for", lambda *args: target)
	monkeypatch.setattr(runner, "_has_seeded_inputs", lambda *args: True)
	monkeypatch.setattr(runner, "_seed_identity", lambda *args: {"fingerprint": "seed"})
	monkeypatch.setattr(runner, "_input_guard", lambda *args: "inputs")
	monkeypatch.setattr(runner, "_output_guard", lambda *args: "outputs")
	monkeypatch.setattr(runner, "_clear_target", lambda *args: None)
	monkeypatch.setattr(runner, "_verify_archive_files", lambda *args: None)
	measured = []
	audits = []
	def measure(arguments, *args):
		measured.append(arguments)
		return {"status": "complete", "end_to_end_seconds": 2, "worker": {"rows": 100}}
	def audit(target, manifest, session, family, window, **kwargs):
		audits.append((session, family, window))
		return {"status": "passed", "session": session, "family": family, "window": window}
	monkeypatch.setattr(runner, "_run_child", measure)
	monkeypatch.setattr(runner, "_audit_series", audit)
	return runner, measured, audits


def test_stage9c_measure_audit_and_resume_are_independent(tmp_path, monkeypatch):
	runner, measured, audits = _fake_runner(monkeypatch)
	arguments = (tmp_path / "manifest.json", tmp_path / "dbinfo.id", tmp_path / "run")
	options = {"profile": "compact", "disposable_schema": "stockguidance_disposable_test", "max_trials": 1}
	first = runner.run_stage9_zscores(*arguments, **options, phase="measure")
	assert len(measured) == 1 and not audits
	trial = first["configurations"]["python-p1"]["blocks"]["fixed_25"]["repetitions"][0]
	assert trial["status"] == "awaiting_audit"
	partial = runner.run_stage9_zscores(*arguments, **options, resume=True, phase="audit", audit_series_limit=2)
	assert len(measured) == 1 and len(audits) == 2
	assert partial["status"] == "incomplete"
	resumed = runner.run_stage9_zscores(*arguments, **options, resume=True, phase="audit")
	assert len(measured) == 1 and len(audits) == 8
	assert resumed["configurations"]["python-p1"]["blocks"]["fixed_25"]["repetitions"][0]["status"] == "complete"
	final = runner.run_stage9_zscores(*arguments, profile="compact", resume=True)
	assert len(measured) == 24
	assert final["status"] == "complete"
	assert len({item["checkpoint_directory"] for item in measured}) == 24
	assert all(item["warm_median_seconds"] == 4 for item in final["configurations"].values())
	runner.run_stage9_zscores(*arguments, profile="compact", resume=True)
	assert len(measured) == 24


def test_stage9c_resume_rejects_changed_code_profile_and_pending_output(tmp_path, monkeypatch):
	import pytest
	runner, measured, audits = _fake_runner(monkeypatch)
	arguments = (tmp_path / "manifest.json", tmp_path / "dbinfo.id", tmp_path / "run")
	runner.run_stage9_zscores(*arguments, disposable_schema="stockguidance_disposable_test", profile="compact", phase="measure")
	path = arguments[2] / "stage9c-zscores.json"
	before = path.read_bytes()
	with pytest.raises(ValueError, match="protocol"):
		runner.run_stage9_zscores(*arguments, resume=True, profile="full")
	assert path.read_bytes() == before
	monkeypatch.setattr(runner, "_file_hashes", lambda path: {"candidate": "changed"})
	with pytest.raises(ValueError, match="source_code_hashes"):
		runner.run_stage9_zscores(*arguments, resume=True, profile="compact")
	assert path.read_bytes() == before
	monkeypatch.setattr(runner, "_file_hashes", lambda path: {"candidate": "hash"})
	monkeypatch.setattr(runner, "_output_guard", lambda *args: "changed")
	with pytest.raises(ValueError, match="Pending audit target changed"):
		runner.run_stage9_zscores(*arguments, resume=True, profile="compact")
	assert len(measured) == 1 and not audits


def test_stage9c_retry_retains_failure_and_restarts_entire_trial(tmp_path, monkeypatch):
	import pytest
	runner, measured, audits = _fake_runner(monkeypatch)
	arguments = (tmp_path / "manifest.json", tmp_path / "dbinfo.id", tmp_path / "run")
	successful_child = runner._run_child
	monkeypatch.setattr(runner, "_run_child", lambda *args: {"status": "timed_out", "end_to_end_seconds": 60})
	runner.run_stage9_zscores(*arguments, disposable_schema="stockguidance_disposable_test")
	with pytest.raises(ValueError, match="retry-failed"):
		runner.run_stage9_zscores(*arguments, resume=True)
	monkeypatch.setattr(runner, "_run_child", successful_child)
	report = runner.run_stage9_zscores(*arguments, resume=True, retry_failed=True, max_trials=1)
	assert len(measured) == 1
	assert report["attempt_history"][0]["trial"]["status"] == "timed_out"
	assert report["configurations"]["python-p1"]["blocks"]["fixed_25"]["repetitions"][0]["status"] == "complete"


def test_interrupted_audit_resumes_without_remeasuring_or_reauditing(tmp_path, monkeypatch):
	import pytest
	runner, measured, audits = _fake_runner(monkeypatch)
	arguments = (tmp_path / "manifest.json", tmp_path / "dbinfo.id", tmp_path / "run")
	working_audit = runner._audit_series
	def interrupted_audit(*args, **kwargs):
		if audits:
			raise KeyboardInterrupt()
		return working_audit(*args, **kwargs)
	monkeypatch.setattr(runner, "_audit_series", interrupted_audit)
	with pytest.raises(KeyboardInterrupt):
		runner.run_stage9_zscores(*arguments, disposable_schema="stockguidance_disposable_test")
	monkeypatch.setattr(runner, "_audit_series", working_audit)
	runner.run_stage9_zscores(*arguments, resume=True, phase="audit", max_trials=1)
	assert len(measured) == 1
	assert len(audits) == 8
	assert len(set(audits)) == 8


def test_correctness_failure_is_saved_and_stops_further_trials(tmp_path, monkeypatch):
	runner, measured, audits = _fake_runner(monkeypatch)
	def failing_audit(*args, **kwargs):
		raise ValueError("wrong status")
	monkeypatch.setattr(runner, "_audit_series", failing_audit)
	report = runner.run_stage9_zscores(tmp_path / "manifest.json", tmp_path / "dbinfo.id", tmp_path / "run", disposable_schema="stockguidance_disposable_test")
	assert len(measured) == 1
	assert report["status"] == "incomplete"
	assert report["execution"]["status"] == "failed"
	trial = report["configurations"]["python-p1"]["blocks"]["fixed_25"]["repetitions"][0]
	assert trial["status"] == "failed_correctness"
	assert trial["correctness_error"] == "wrong status"
	assert trial["end_to_end_seconds"] == 2


def test_worker_interrupt_kills_child_and_stops_monitor(tmp_path, monkeypatch):
	from types import SimpleNamespace
	import pytest
	from stockguidance.performance import stage9_zscores as runner

	events = []
	def communicate(**kwargs):
		if kwargs:
			raise KeyboardInterrupt()
		events.append("drained")
		return "", ""
	child = SimpleNamespace(pid=123, communicate=communicate, kill=lambda: events.append("killed"))
	monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: child)
	monkeypatch.setattr(runner, "_ProcessTreeMonitor", lambda pid: SimpleNamespace(start=lambda: None, stop=lambda: events.append("monitor_stopped")))
	arguments = {key: "unused" for key in ("manifest", "dbinfo_file", "schema", "sessions", "checkpoint_directory")}
	arguments["backend"] = "python"
	with pytest.raises(KeyboardInterrupt):
		runner._run_child(arguments, 60, tmp_path)
	assert events == ["killed", "drained", "monitor_stopped"]


def test_concurrent_orchestrator_is_rejected_before_measurement(tmp_path, monkeypatch):
	import pytest
	runner, measured, audits = _fake_runner(monkeypatch)
	target = runner._target_for(None, None, None)
	connection = target.connect()
	connection.cursor().fetchone = lambda: (0,)
	with pytest.raises(ValueError, match="Another Stage 9c orchestrator"):
		runner.run_stage9_zscores(tmp_path / "manifest.json", tmp_path / "dbinfo.id", tmp_path / "run", disposable_schema="stockguidance_disposable_test")
	assert not measured and not audits
