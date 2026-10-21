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
	assert digest.hexdigest() == content_id("\n".join(lines) + "\n")


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
