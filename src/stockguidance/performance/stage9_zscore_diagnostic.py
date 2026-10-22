from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import subprocess
from time import perf_counter
from uuid import uuid4

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.market_data.identity import content_id
from stockguidance.legacy_migration.precision import exact_records_id, read_exact_archive
from stockguidance.performance.mysql_candidates import load_manifest
from stockguidance.performance.stage9_zscores import (
	BACKENDS, _audit_series, _clear_target, _has_seeded_inputs, _input_fingerprint,
	_run_child, _seed_inputs, _series_jobs, _target_for,
	_file_hashes,
	PythonZScoreCandidate,
)
from stockguidance.performance.sql_zscore_candidate import SQLZScoreCandidate


def fixture_manifest(manifest: dict, directory: Path) -> Path:
	directory.mkdir(parents=True, exist_ok=True)
	fixture = deepcopy(manifest)
	calendar = ExchangeCalendar("XNYS", date(2024, 1, 1), date(2024, 5, 1))
	sessions = calendar.sessions(date(2024, 1, 2), date(2024, 5, 1))[:61]
	fixture["files"] = []
	mappings = [{"id": identifier, "label": f"FIX{identifier}"} for identifier in range(1, 34)]
	for index, session in enumerate(sessions):
		session_key = session.session_date.isoformat()
		path = directory / f"{session_key}.ndjson"
		bars = []
		for mapping in mappings:
			identifier = mapping["id"]
			if identifier == 2 or (identifier == 3 and index < 50):
				continue
			close = (25.0 if index == 0 else 26.69) if identifier == 1 else 20 + identifier + (index % 11) * 0.125
			bars.append({"T": mapping["label"], "o": close, "h": close + 1, "l": close - 1, "c": close,
				"v": 100, "vw": close, "n": 3, "t": int(datetime.combine(session.session_date, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000) + 16 * 3600000})
		path.write_text("".join(json.dumps(bar) + "\n" for bar in bars), encoding="utf-8")
		exact, file_hash = read_exact_archive(path)
		fixture["files"].append({"session": session_key, "path": str(path.resolve()), "valid": True, "file_sha256": file_hash, "records_id": exact_records_id(exact)})
	dependencies = [session.session_date.isoformat() for session in sessions]
	target = dependencies[-1]
	fixture["targets"] = [{"session": target, "roles": ["fixed_25", "chronological_20"], "zscore_dependencies": dependencies, "derived_dependencies": dependencies[-2:]}]
	fixture["target_blocks"] = {"fixed_25": [target], "chronological_20": [target]}
	fixture["mappings"] = {"ticker_mappings": mappings, "date_mappings": [{"id": index + 1, "session": session} for index, session in enumerate(dependencies)]}
	fixture.pop("manifest_id", None)
	fixture["manifest_id"] = content_id(fixture)
	path = directory / "manifest.json"
	path.write_text(json.dumps(fixture, sort_keys=True, indent=2) + "\n", encoding="utf-8")
	return path


def run_diagnostic(manifest_path: Path, dbinfo_file: Path, output: Path) -> dict:
	output.mkdir(parents=True, exist_ok=False)
	fixture_path = fixture_manifest(load_manifest(manifest_path), output / "fixture")
	manifest = load_manifest(fixture_path)
	schema = f"stockguidance_disposable_s9diag_{uuid4().hex[:8]}"
	target = _target_for(dbinfo_file.resolve(), schema, manifest)
	target.prepare()
	session = manifest["targets"][0]["session"]
	report = {"status": "incomplete", "comparison_eligible": False, "purpose": "bounded synthetic Stage 9c diagnostic", "schema": schema, "backends": {}, "source_code_hashes": _file_hashes(Path(__file__).resolve().parents[3])}
	keepalive = target.connect()
	try:
		_seed_inputs(target, manifest, (session,), output / "setup")
		_has_seeded_inputs(target, manifest)
		report["seed_recovery"] = _seed_recovery_checks(target, manifest)
		fingerprint = _input_fingerprint(target, manifest)
		for backend in BACKENDS:
			_clear_target(target, manifest)
			arguments = {"backend": backend, "manifest": str(fixture_path.resolve()), "dbinfo_file": str(dbinfo_file.resolve()),
				"schema": schema, "sessions": session, "checkpoint_directory": str((output / backend).resolve()), "block": "diagnostic", "repetition": "0"}
			trial = _run_child(arguments, 300, output)
			report["backends"][backend] = trial
			if trial["status"] == "complete":
				if trial["worker"]["verified_archive_count"] != len(manifest["files"]):
					raise AssertionError("Timed worker archive verification scope differs")
				trial["audits"] = [_audit_series(target, manifest, *job, cache_directory=output / "scalar-audit-cache", input_fingerprint=fingerprint) for job in _series_jobs((session,))]
				if backend == "python":
					cache_file = next((output / "scalar-audit-cache").glob("*.json"))
					original_cache = cache_file.read_bytes()
					try:
						cached = json.loads(original_cache)
						cached["rows"][0][2] = 99
						cache_file.write_text(json.dumps(cached), encoding="utf-8")
						rejected = False
						for job in _series_jobs((session,)):
							try:
								_audit_series(target, manifest, *job, cache_directory=output / "scalar-audit-cache", input_fingerprint=fingerprint)
							except ValueError:
								rejected = True
								break
						if not rejected:
							raise AssertionError("Changed scalar audit cache was accepted")
					finally:
						cache_file.write_bytes(original_cache)
					report["audit_cache_tamper_rejected"] = True
				trial["recovery"] = _recovery_checks(backend, target, manifest, fixture_path, dbinfo_file, output / backend)
			if _input_fingerprint(target, manifest) != fingerprint:
				raise ValueError("Diagnostic inputs changed")
			(output / "diagnostic.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
		report["status"] = "passed" if all(trial["status"] == "complete" for trial in report["backends"].values()) else "failed"
	finally:
		keepalive.close()
		(output / "diagnostic.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return report


def _seed_recovery_checks(target, manifest):
	connection = target.connect()
	cursor = connection.cursor()
	try:
		target.validate(connection)
		cursor.execute("SELECT value FROM stage6_disposable_metadata WHERE `key` = %s", ("stage9c_seed",))
		identity = cursor.fetchone()[0]
		cursor.execute("DELETE FROM stage6_disposable_metadata WHERE `key` = %s", ("stage9c_seed",))
		try:
			_has_seeded_inputs(target, manifest)
		except ValueError:
			pass
		else:
			raise AssertionError("Unverified disposable seed was accepted")
		finally:
			cursor.execute("INSERT INTO stage6_disposable_metadata (`key`, value) VALUES (%s, %s)", ("stage9c_seed", identity))
		from stockguidance.performance.candidates import _manifest_table_names
		from stockguidance.database.table_names import quote_identifier
		prices = quote_identifier(_manifest_table_names(manifest).prices)
		cursor.execute(f"SELECT date_id, ticker_id, `close` FROM {prices} ORDER BY date_id, ticker_id LIMIT 1")
		date_id, ticker_id, close = cursor.fetchone()
		cursor.execute(f"UPDATE {prices} SET `close` = `close` + 1 WHERE date_id = %s AND ticker_id = %s", (date_id, ticker_id))
		try:
			_has_seeded_inputs(target, manifest)
		except ValueError:
			pass
		else:
			raise AssertionError("Changed disposable inputs were accepted")
		finally:
			cursor.execute(f"UPDATE {prices} SET `close` = %s WHERE date_id = %s AND ticker_id = %s", (close, date_id, ticker_id))
		_has_seeded_inputs(target, manifest)
	finally:
		cursor.close()
		connection.close()
	return ["missing_seed_identity_rejected", "changed_seed_inputs_rejected", "restored_seed_verified"]


def _recovery_checks(backend, target, manifest, manifest_path, dbinfo_file, directory):
	session = manifest["targets"][0]["session"]
	reference = SQLZScoreCandidate(manifest, target, directory / "recovery.json", target_session=session)
	candidate = PythonZScoreCandidate(manifest, target, directory / "recovery.json", session) if backend == "python" else reference
	checkpoint = directory / "recovery-checkpoint.json"
	def invoke(interruption=None, fail=False):
		if backend == "java":
			from stockguidance.performance.java_zscore_trial import _java_paths
			command = ["java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "-cp", _java_paths(), "stockguidance.migration.JavaZScoreMain",
				"--manifest", str(manifest_path.resolve()), "--dbinfo-file", str(dbinfo_file.resolve()), "--disposable-schema", target.schema,
				"--session", session, "--family", "price", "--window", "20", "--checkpoint", str(checkpoint.resolve())]
			if interruption is not None:
				command.extend(("--interrupt", interruption))
			result = subprocess.run(command, capture_output=True, text=True, timeout=60)
			if (result.returncode != 0) != fail:
				raise AssertionError("Unexpected Java diagnostic recovery outcome")
			return
		try:
			candidate.write_zscores("price", 20, checkpoint=checkpoint, interruption=interruption)
		except (ValueError, RuntimeError):
			if not fail:
				raise
		else:
			if fail:
				raise AssertionError("Expected diagnostic recovery rejection")
	stable = reference.snapshot()
	invoke("before_commit", fail=True)
	if reference.snapshot() != stable or checkpoint.exists():
		raise AssertionError("Before-commit rollback failed")
	invoke("after_commit", fail=True)
	if checkpoint.exists():
		raise AssertionError("Checkpoint preceded commit acknowledgement")
	invoke()
	stable = reference.snapshot()
	invoke()
	if reference.snapshot() != stable:
		raise AssertionError("Idempotent recovery failed")
	checkpoint.write_text('{"manifest_id":"stale"}', encoding="utf-8")
	invoke(fail=True)
	if reference.snapshot() != stable:
		raise AssertionError("Stale checkpoint changed output")
	checkpoint.unlink()
	archive = Path(manifest["files"][-1]["path"])
	original = archive.read_bytes()
	try:
		archive.write_bytes(original + b"\n")
		invoke(fail=True)
	finally:
		archive.write_bytes(original)
	if reference.snapshot() != stable:
		raise AssertionError("Changed source changed output")
	return ["before_commit_rollback", "after_commit_restart", "idempotent_rerun", "stale_checkpoint_rejected", "changed_source_rejected"]


def main() -> int:
	import argparse
	parser = argparse.ArgumentParser(description="Bounded disposable diagnostic; does not produce Stage 9c ranking evidence")
	parser.add_argument("--manifest", type=Path, default=Path("output/legacy-migration/benchmark/candidates/manifest.json"))
	parser.add_argument("--dbinfo-file", type=Path, default=Path("dbinfo.id"))
	parser.add_argument("--output", type=Path, required=True)
	arguments = parser.parse_args()
	started = perf_counter()
	report = run_diagnostic(arguments.manifest, arguments.dbinfo_file, arguments.output)
	print(json.dumps({"status": report["status"], "seconds": perf_counter() - started, "report": str(arguments.output / "diagnostic.json")}))
	return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
	raise SystemExit(main())
