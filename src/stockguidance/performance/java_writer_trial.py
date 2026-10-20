from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import traceback

from stockguidance.database.table_names import quote_identifier
from stockguidance.legacy_migration.precision import exact_records_id, read_exact_archive
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, LegacyDerivedRow, derived_id
from stockguidance.performance.candidates import _exact_derived_rows, _file_hash, _manifest_table_names
from stockguidance.performance.mysql_candidates import MySQLPythonBaseline, MySQLTarget, load_manifest
from stockguidance.validation.derived import _equal


def fixture_manifest(manifest: dict, directory: Path) -> Path:
	directory.mkdir(parents=True, exist_ok=True)
	fixture = deepcopy(manifest)
	fixture["files"] = []
	for session in ("2024-07-01", "2024-07-02", "2024-07-03"):
		timestamp = int(datetime.fromisoformat(session).replace(tzinfo=timezone.utc).timestamp() * 1000) + 16 * 3600000
		path = directory / f"{session}.ndjson"
		path.write_text(
			'{"T":"AAP","o":1702500000000000.0,"h":1702500000000000.1,"l":1702500000000000.0,"c":1702500000000000.1,"v":61812.9999,"vw":null,"n":10,"t":' + str(timestamp) + '}\n'
			+ '{"T":"AAp","o":10,"h":12,"l":9,"c":11,"v":14982.4,"vw":10.5,"n":null,"t":' + str(timestamp) + '}\n', encoding="utf-8")
		bars, digest = read_exact_archive(path)
		fixture["files"].append({"session": session, "path": str(path.resolve()), "valid": True, "file_sha256": digest, "records_id": exact_records_id(bars)})
	fixture["targets"] = [{"session": "2024-07-02", "derived_dependencies": ["2024-07-01", "2024-07-03"], "roles": ["fixture"]}]
	fixture["mappings"] = {"ticker_mappings": [{"id": 11, "label": "AAP"}, {"id": 13, "label": "AAp"}],
		"date_mappings": [{"id": 1001 + index, "session": item["session"]} for index, item in enumerate(fixture["files"])]}
	fixture.pop("manifest_id")
	fixture["manifest_id"] = content_id(fixture)
	path = directory / "manifest.json"
	path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	return path


class WriterTrial:
	def __init__(self, manifest_path: Path, dbinfo_file: Path, schema: str, directory: Path) -> None:
		self.manifest_path, self.dbinfo_file, self.directory = manifest_path, dbinfo_file, directory
		self.manifest = load_manifest(manifest_path)
		self.target = MySQLTarget(dbinfo_file, schema, self.manifest)
		self.schema = self.target.prepare()
		self.names = _manifest_table_names(self.manifest)
		self.python = MySQLPythonBaseline(self.manifest, self.target, directory / "python-reference.json")
		java_root = Path(__file__).resolve().parents[3] / "StockGuidance" / "java"
		self.classpath = ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))

	def java(self, workload: str, session: str, *, checkpoint: Path | None = None, interruption: str | None = None, fail: bool = False, manifest_path: Path | None = None) -> dict:
		command = ["java", "-Xmx512m", "-cp", self.classpath, "stockguidance.migration.JavaWriterMain",
			"--manifest", str(manifest_path or self.manifest_path), "--dbinfo-file", str(self.dbinfo_file),
			"--disposable-schema", self.target.schema, "--workload", workload, "--session", session]
		if checkpoint is not None:
			command.extend(("--checkpoint", str(checkpoint)))
		if interruption is not None:
			command.extend(("--interrupt", interruption))
		result = subprocess.run(command, capture_output=True, text=True, timeout=120)
		payload = json.loads(result.stdout)
		if (result.returncode != 0) != fail:
			raise AssertionError(f"Unexpected Java outcome for {workload}/{session}: {payload.get('failure_type')}")
		if interruption is not None and payload.get("failure_type") != "InterruptionException":
			raise AssertionError(f"Expected injected interruption, got {payload.get('failure_type')}")
		return payload

	def query(self, sql: str, parameters=(), *, write: bool = False):
		connection = self.target.connect()
		try:
			self.target.validate(connection)
			cursor = connection.cursor()
			cursor.execute(sql, parameters)
			return cursor.rowcount if write else cursor.fetchall()
		finally:
			connection.close()

	def snapshot(self) -> str:
		values = []
		for table in (self.names.prices, self.names.deltas, self.names.imports, "stage6_completion"):
			rows = self.query(f"SELECT * FROM {quote_identifier(table)} ORDER BY 1, 2")
			values.append([[None if value is None else str(value) for value in row] for row in rows])
		return content_id(values)

	def check_raw(self, session: str) -> int:
		expected = self.python._bars(session)
		rows = self.query(f"SELECT t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM {quote_identifier(self.names.prices)} p JOIN {quote_identifier(self.names.tickers)} t ON t.id = p.ticker_id JOIN {quote_identifier(self.names.sessions)} s ON s.id = p.date_id WHERE s.transaction_date = %s", (session,))
		actual = {row[0]: row[1:] for row in rows}
		assert len(actual) == len(expected)
		for bar in expected:
			assert actual[bar.ticker] == (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)
		return len(expected)

	def check_derived(self, session: str) -> int:
		target = next(item for item in self.manifest["targets"] if item["session"] == session)
		prior, following = target["derived_dependencies"]
		expected = _exact_derived_rows(self.python._bars(prior), self.python._bars(session), self.python._bars(following))
		columns = ", ".join("d." + quote_identifier(column) for column in DERIVED_COLUMNS)
		rows = self.query(f"SELECT t.label, {columns} FROM {quote_identifier(self.names.deltas)} d JOIN {quote_identifier(self.names.tickers)} t ON t.id = d.ticker_id JOIN {quote_identifier(self.names.sessions)} s ON s.id = d.date_id WHERE s.transaction_date = %s", (session,))
		actual = {row[0]: row[1:] for row in rows}
		assert len(actual) == len(expected)
		for row in expected:
			for stored, value in zip(actual[row.ticker], row.values, strict=True):
				assert _equal(stored, value)
		dependency = content_id({"prior": prior, "following": following, "raw": [exact_records_id(self.python._bars(value)) for value in (prior, following)]})
		assert self.query(f"SELECT derived_dependency_id FROM {quote_identifier(self.names.imports)} WHERE session_date = %s", (session,)) == [(dependency,)]
		stored_digest = derived_id(tuple(LegacyDerivedRow(ticker, values) for ticker, values in actual.items()))
		assert self.query(f"SELECT derived_content_id FROM {quote_identifier(self.names.imports)} WHERE session_date = %s", (session,)) == [(stored_digest,)]
		return len(expected)

	def recovery(self) -> list[str]:
		checks = []
		session = "2024-07-02"
		checkpoint = self.directory / "raw-checkpoint.json"
		before = self.snapshot()
		self.java("raw", session, checkpoint=checkpoint, interruption="before_commit", fail=True)
		assert self.snapshot() == before and not checkpoint.exists()
		checks.append("raw_before_commit_rollback")
		self.java("raw", session, checkpoint=checkpoint, interruption="after_commit", fail=True)
		self.check_raw(session)
		assert not checkpoint.exists()
		checks.append("raw_after_commit_durable_without_checkpoint")
		assert self.java("raw", session, checkpoint=checkpoint)["changed_rows"] == 0
		assert checkpoint.exists()
		checks.append("raw_restart_idempotent")
		self.query(f"DELETE FROM {quote_identifier(self.names.prices)} WHERE ticker_id = 11", write=True)
		self.query(f"UPDATE {quote_identifier(self.names.prices)} SET close = 10 WHERE ticker_id = 13", write=True)
		assert self.java("raw", session)["changed_rows"] == 2
		self.check_raw(session)
		checks.append("missing_and_differing_raw_repair_exact_decimals_and_case")
		for dependency in ("2024-07-01", "2024-07-03"):
			self.java("raw", dependency)
		before = self.snapshot()
		derived_checkpoint = self.directory / "derived-checkpoint.json"
		self.java("derived", session, checkpoint=derived_checkpoint, interruption="before_commit", fail=True)
		assert self.snapshot() == before and not derived_checkpoint.exists()
		checks.append("derived_before_commit_rollback")
		self.java("derived", session, checkpoint=derived_checkpoint, interruption="after_commit", fail=True)
		self.check_derived(session)
		assert not derived_checkpoint.exists()
		before = self.snapshot()
		self.java("derived", session, checkpoint=derived_checkpoint)
		assert self.snapshot() == before and derived_checkpoint.exists()
		checks.append("derived_after_commit_restart_idempotent")
		self.java("raw", session, checkpoint=checkpoint)
		assert self.snapshot() == before
		checks.append("matching_adoption_preserves_derived_metadata")
		stale = self.directory / "stale-checkpoint.json"
		stale.write_text('{"schema_version":1,"manifest_id":"stale"}', encoding="utf-8")
		self.java("raw", session, checkpoint=stale, fail=True)
		assert self.snapshot() == before
		checks.append("stale_checkpoint_rejected")
		archive = next(Path(item["path"]) for item in self.manifest["files"] if item["session"] == session)
		original = archive.read_bytes()
		try:
			archive.write_bytes(original.replace(b"14982.4", b"14982.5"))
			self.java("raw", session, fail=True)
			assert self.snapshot() == before
		finally:
			archive.write_bytes(original)
		checks.append("changed_input_rejected")
		self.query(f"UPDATE {quote_identifier(self.names.prices)} SET close = 10 WHERE ticker_id = 13 AND date_id = 1001", write=True)
		corrupt = self.snapshot()
		self.java("derived", session, fail=True)
		assert self.snapshot() == corrupt
		self.java("raw", "2024-07-01")
		checks.append("stored_dependency_corruption_rejected")
		missing = deepcopy(self.manifest)
		next(item for item in missing["files"] if item["session"] == "2024-07-01")["valid"] = False
		missing.pop("manifest_id")
		missing["manifest_id"] = content_id(missing)
		missing_path = self.directory / "missing-manifest.json"
		missing_path.write_text(json.dumps(missing), encoding="utf-8")
		before = self.snapshot()
		result = self.java("derived", session, manifest_path=missing_path)
		assert result["status"] == "missing_dependency" and not result["committed"] and self.snapshot() == before
		checks.append("missing_session_not_published")
		return checks


def run_trial(manifest_path: Path, dbinfo_file: Path, schema: str, output: Path) -> dict:
	manifest = load_manifest(manifest_path)
	output.parent.mkdir(parents=True, exist_ok=True)
	repository = Path(__file__).resolve().parents[3]
	code_files = [Path(__file__), Path(__file__).with_name("mysql_candidates.py"), Path(__file__).with_name("candidates.py")]
	code_files.extend((repository / "StockGuidance/java/src/main/java/stockguidance/migration").glob("*.java"))
	report = {"schema_version": 1, "stage": "7a", "backend": "java", "storage_engine": "mysql",
		"status": "incomplete", "manifest_id": manifest["manifest_id"], "recovery_checks": [], "sessions": [],
		"comparison_eligible": False, "production_access": {"ddl": False, "dml": False},
		"code_hashes": {str(path.relative_to(repository)): _file_hash(path) for path in code_files},
		"protocol": {"writers": 1, "preparers": 1, "queued_sessions_max": 3, "java_heap": "-Xmx512m", "purpose": "correctness and recovery; timings deferred to Stage 9"}}
	def save():
		output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
	try:
		fixture_path = fixture_manifest(manifest, output.parent / (schema + "_fixtures"))
		fixture = WriterTrial(fixture_path, dbinfo_file, schema + "_fx", fixture_path.parent)
		report["fixture_schema"] = fixture.schema
		report["recovery_checks"] = fixture.recovery()
		save()
		trial = WriterTrial(manifest_path, dbinfo_file, schema, output.parent)
		report["schema"] = trial.schema
		files = {item["session"]: item for item in manifest["files"]}
		for target in manifest["targets"]:
			if "fixed_25" not in target["roles"]:
				continue
			session = target["session"]
			dependencies = target["derived_dependencies"]
			for dependency in sorted(set([session, *dependencies])):
				if files[dependency]["valid"]:
					trial.python._raw_session(dependency)
			matching = trial.java("raw", session)
			assert matching["changed_rows"] == 0
			rows = trial.check_raw(session)
			derived = trial.java("derived", session)
			derived_rows = trial.check_derived(session) if derived["committed"] else 0
			report["sessions"].append({"session": session, "raw_rows": rows, "matching_changed_rows": 0,
				"derived_rows": derived_rows, "derived_status": derived["status"]})
			save()
		print(json.dumps({"checked_session": session, "raw_rows": rows, "derived_rows": derived_rows}), flush=True)
		report["status"] = "complete"
	except Exception as error:
		report["status"] = "failed"
		report["failure_type"] = type(error).__name__
		report["failure_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
		save()
		raise RuntimeError("Disposable MySQL writer trial failed; inspect its sanitized report") from None
	save()
	return report
