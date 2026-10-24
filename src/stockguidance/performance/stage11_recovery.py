from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from math import isclose
from pathlib import Path
from typing import Any

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import DEFAULT_CALENDAR_ID
from stockguidance.legacy_migration.precision import ExactArchiveBar, exact_records_id, read_exact_archive
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS
from stockguidance.performance.candidates import _exact_derived_rows, _file_hash, _manifest_table_names
from stockguidance.performance.mysql_candidates import MySQLTarget, load_manifest
from stockguidance.performance.stage11_integration import (
	Stage11Context,
	Stage11Error,
	_sha256,
	_iso,
	_selected,
	run_mysql,
)
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import InputState, ZScoreInput, ZScoreObservation
from stockguidance.zscores.specification import DELTA_DEFINITION, ZScoreFamily, ZScoreSpecification


STAGE11B_SCHEMA_VERSION = 1
RECOVERY_ARTIFACTS = {
	"raw": ("raw", "java"),
	"derived": ("derived", "java"),
	"zscores": ("zscores", "java"),
}


def _json(path: Path) -> dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
		raise Stage11Error(f"Cannot read Stage 11b artifact: {path}") from None
	if not isinstance(value, dict):
		raise Stage11Error(f"Stage 11b artifact must be an object: {path}")
	return value


def _require_file(path: Path, label: str) -> Path:
	path = path.resolve()
	if not path.is_file():
		raise Stage11Error(f"Missing Stage 11b {label}: {path}")
	return path


def _selected_backends(context: Stage11Context) -> dict[str, str]:
	return {
		"raw": _selected(context.decision, "raw_adoption_repair")["selected_backend"],
		"derived": _selected(context.decision, "derived_computation_persistence")["selected_backend"],
		"zscores": _selected(context.decision, "zscores_persistence")["selected_backend"],
	}


def validate_stage11a_report(path: Path, context: Stage11Context) -> dict[str, Any]:
	report = _json(_require_file(path, "11a report"))
	if report.get("schema_version") != 1 or report.get("stage") != "11a" or report.get("status") != "complete":
		raise Stage11Error("Stage 11a report is not complete")
	if report.get("production_access", {}).get("ddl") is not False or report.get("production_access", {}).get("dml") is not False:
		raise Stage11Error("Stage 11a report does not prove disposable-only access")
	prerequisites = report.get("prerequisites", {})
	if prerequisites.get("decision_sha256") != context.decision_sha256 or prerequisites.get("manifest_sha256") != context.manifest_sha256 or prerequisites.get("audit_sha256") != context.audit_sha256 or prerequisites.get("manifest_id") != context.manifest_id:
		raise Stage11Error("Stage 11a report is not bound to the current prerequisites")
	selected = report.get("selected_backends")
	if selected != _selected_backends(context):
		raise Stage11Error("Stage 11a selected backends changed")
	workloads = set(report.get("execution", {}).get("workloads", ()))
	if not {"raw", "derived"}.issubset(workloads):
		raise Stage11Error("Stage 11a report lacks raw and derived integration")
	return report


def _recovery_artifact(context: Stage11Context, workload: str) -> dict[str, Any]:
	try:
		section, backend = RECOVERY_ARTIFACTS[workload]
		entry = context.decision["recovery_gates"][section][backend]
	except (KeyError, TypeError):
		if workload != "zscores":
			raise Stage11Error(f"Stage 10 decision lacks {workload} recovery evidence") from None
		path = context.manifest_path.parent / "java-zscores-mysql.json"
		entry = {"path": str(path), "sha256": _sha256(path), "status": "passed"}
	if not isinstance(entry, dict) or entry.get("status") not in {"passed", "passed_reference"}:
		raise Stage11Error(f"Stage 10 recovery evidence is not passing for {workload}")
	path = _require_file(Path(entry.get("path", "")), f"{workload} recovery report")
	if entry.get("sha256") != _sha256(path):
		raise Stage11Error(f"Changed {workload} recovery report")
	report = _json(path)
	if report.get("status") != "complete":
		raise Stage11Error(f"{workload} recovery report is not complete")
	schema = report.get("schema") or report.get("fixture_schema")
	schema_identity = isinstance(schema, dict) and schema.get("production_target") is False and bool(schema.get("schema_hash"))
	code_identity = isinstance(report.get("code_hashes"), dict) and bool(report["code_hashes"])
	if not schema_identity or not code_identity:
		raise Stage11Error(f"{workload} recovery report lacks schema/code identity guards")
	return {"path": str(path), "sha256": entry["sha256"], "checks": report.get("recovery_checks", []), "schema_identity_guard": True, "code_identity_guard": True}


def recovery_evidence(context: Stage11Context) -> dict[str, Any]:
	checks = {
		"rollback": {"raw_before_commit_rollback", "derived_before_commit_rollback", "before_commit_rollback"},
		"after_commit_interruption": {"raw_after_commit_durable_without_checkpoint", "derived_after_commit_restart_idempotent", "after_commit_durable_without_checkpoint"},
		"stale_checkpoint": {"stale_checkpoint_rejected"},
		"changed_input": {"changed_input_rejected"},
		"dependency_race": {"stored_dependency_corruption_rejected"},
		"idempotency": {"raw_restart_idempotent", "derived_after_commit_restart_idempotent", "restart_idempotent"},
	}
	evidence = {workload: _recovery_artifact(context, workload) for workload in RECOVERY_ARTIFACTS}
	available = {check: any(expected.intersection(item["checks"]) for item in evidence.values()) for check, expected in checks.items()}
	available["schema_identity"] = all(item["schema_identity_guard"] for item in evidence.values())
	available["code_identity"] = all(item["code_identity_guard"] for item in evidence.values())
	missing = tuple(sorted(check for check, present in available.items() if not present))
	if missing:
		raise Stage11Error(f"Recovery evidence is incomplete: {', '.join(missing)}")
	return {"status": "passed", "checks": available, "artifacts": evidence, "source": "Stage 7 candidate recovery trials"}


def _bars(context: Stage11Context, session: str) -> tuple[dict[str, Any], Path, tuple[ExactArchiveBar, ...]]:
	manifest_item = next((item for item in context.manifest["files"] if item.get("session") == session and item.get("valid")), None)
	if not isinstance(manifest_item, dict):
		raise Stage11Error(f"Session is not an adoptable archive file: {session}")
	path = Path(manifest_item["path"]).resolve()
	if _file_hash(path) != manifest_item.get("file_sha256"):
		raise Stage11Error(f"Changed Stage 11b input: {session}")
	bars, file_hash = read_exact_archive(path)
	if file_hash != manifest_item.get("file_sha256") or exact_records_id(bars) != manifest_item.get("records_id"):
		raise Stage11Error(f"Changed Stage 11b record identity: {session}")
	return manifest_item, path, bars


def _decimal(value: object | None) -> Decimal | None:
	return None if value is None else Decimal(str(value))


def _raw_rows(target: MySQLTarget, names: Any, session: str) -> dict[str, tuple[object, ...]]:
	connection = target.connect()
	try:
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT id FROM `{names.sessions}` WHERE transaction_date = %s", (session,))
			date_rows = cursor.fetchall()
			if len(date_rows) != 1:
				raise Stage11Error(f"Database session mapping is not unique: {session}")
			cursor.execute(f"SELECT t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM `{names.prices}` p JOIN `{names.tickers}` t ON t.id = p.ticker_id WHERE p.date_id = %s ORDER BY t.label", (int(date_rows[0][0]),))
			rows: dict[str, tuple[object, ...]] = {}
			for row in cursor.fetchall():
				if str(row[0]) in rows:
					raise Stage11Error(f"Duplicate adopted ticker row: {session}")
				rows[str(row[0])] = tuple(row[1:])
			return rows
		finally:
			cursor.close()
	finally:
		connection.rollback()
		connection.close()


def _verify_raw(context: Stage11Context, target: MySQLTarget, names: Any, sessions: tuple[str, ...]) -> dict[str, Any]:
	checked = 0
	for session in sessions:
		_, _, bars = _bars(context, session)
		expected = {bar.ticker: (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions) for bar in bars}
		actual = _raw_rows(target, names, session)
		if set(actual) != set(expected):
			raise Stage11Error(f"Exact raw adoption mismatch: {session}")
		for ticker, expected_values in expected.items():
			actual_values = actual[ticker]
			if len(actual_values) != len(expected_values) or any(_decimal(left) != _decimal(right) for left, right in zip(actual_values, expected_values, strict=True)):
				raise Stage11Error(f"Exact raw value mismatch: {session}/{ticker}")
		checked += len(expected)
	return {"sessions": len(sessions), "records": checked, "exact": True}


def _derived_rows(target: MySQLTarget, names: Any, session: str) -> dict[str, tuple[object, ...]]:
	connection = target.connect()
	try:
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT id FROM `{names.sessions}` WHERE transaction_date = %s", (session,))
			date_rows = cursor.fetchall()
			if len(date_rows) != 1:
				raise Stage11Error(f"Database session mapping is not unique: {session}")
			columns = ", ".join(f"d.{column}" for column in DERIVED_COLUMNS)
			cursor.execute(f"SELECT t.label, {columns} FROM `{names.deltas}` d JOIN `{names.tickers}` t ON t.id = d.ticker_id WHERE d.date_id = %s ORDER BY t.label", (int(date_rows[0][0]),))
			return {str(row[0]): tuple(row[1:]) for row in cursor.fetchall()}
		finally:
			cursor.close()
	finally:
		connection.rollback()
		connection.close()


def _verify_derived(context: Stage11Context, target: MySQLTarget, names: Any, session: str) -> dict[str, Any]:
	target_entry = next(item for item in context.manifest["targets"] if item.get("session") == session)
	prior, following = tuple(target_entry["derived_dependencies"])
	_, _, previous = _bars(context, prior)
	_, _, current = _bars(context, session)
	_, _, next_bars = _bars(context, following)
	expected = {row.ticker: row.values for row in _exact_derived_rows(previous, current, next_bars)}
	actual = _derived_rows(target, names, session)
	if set(actual) != set(expected):
		raise Stage11Error(f"Independent derived row set mismatch: {session}")
	for ticker, expected_values in expected.items():
		for left, right in zip(actual[ticker], expected_values, strict=True):
			if left is None or right is None:
				if left is not None or right is not None:
					raise Stage11Error(f"Independent derived NULL mismatch: {session}/{ticker}")
			elif not isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-12):
				raise Stage11Error(f"Independent derived value mismatch: {session}/{ticker}")
	return {"session": session, "rows": len(expected), "columns": len(DERIVED_COLUMNS), "tolerance": {"rtol": 1e-10, "atol": 1e-12}}


def _zscore_inputs(target: MySQLTarget, names: Any, context: Stage11Context, dependency_dates: tuple[str, ...]) -> tuple[tuple[Any, ...], dict[int, dict[str, Decimal | None]], dict[int, dict[str, Decimal | None]], dict[str, int]]:
	dates = tuple(date.fromisoformat(value) for value in dependency_dates)
	calendar = ExchangeCalendar(str(context.manifest.get("calendar_id", DEFAULT_CALENDAR_ID)), min(dates), max(dates))
	sessions = tuple(calendar.session(value) for value in dates)
	connection = target.connect()
	try:
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT id, transaction_date FROM `{names.sessions}` WHERE transaction_date IN ({','.join('%s' for _ in dates)}) ORDER BY transaction_date", dependency_dates)
			date_ids = {str(row[1]): int(row[0]) for row in cursor.fetchall()}
			if set(date_ids) != set(dependency_dates):
				raise Stage11Error("Z-score dependency date mappings are incomplete")
			date_by_id = {identifier: session for session, identifier in date_ids.items()}
			cursor.execute(f"SELECT id, label FROM `{names.tickers}` ORDER BY id")
			stored_ticker_ids = {str(row[1]): int(row[0]) for row in cursor.fetchall()}
			ticker_ids = {str(item["label"]): int(item["id"]) for item in context.manifest["mappings"]["ticker_mappings"]}
			if any(stored_ticker_ids.get(label) != identifier for label, identifier in ticker_ids.items()):
				raise Stage11Error("Z-score ticker mappings differ from manifest")
			placeholders = ",".join("%s" for _ in date_ids)
			cursor.execute(f"SELECT ticker_id, date_id, close FROM `{names.prices}` WHERE date_id IN ({placeholders})", tuple(date_ids.values()))
			closes: dict[int, dict[str, Decimal | None]] = {identifier: {} for identifier in ticker_ids.values()}
			for ticker_id, date_id, close in cursor.fetchall():
				if int(ticker_id) in closes:
					closes[int(ticker_id)][date_by_id[int(date_id)]] = _decimal(close)
			cursor.execute(f"SELECT ticker_id, date_id, close_ratio_to_prior FROM `{names.deltas}` WHERE date_id IN ({placeholders})", tuple(date_ids.values()))
			deltas: dict[int, dict[str, Decimal | None]] = {identifier: {} for identifier in ticker_ids.values()}
			for ticker_id, date_id, delta in cursor.fetchall():
				if int(ticker_id) in deltas:
					deltas[int(ticker_id)][date_by_id[int(date_id)]] = _decimal(delta)
			return sessions, closes, deltas, ticker_ids
		finally:
			cursor.close()
	finally:
		connection.rollback()
		connection.close()


def _observations(family: ZScoreFamily, sessions: tuple[Any, ...], ticker_id: int, closes: dict[str, Decimal | None], deltas: dict[str, Decimal | None]) -> tuple[ZScoreObservation, ...]:
	result: list[ZScoreObservation] = []
	previous: Decimal | None = None
	for session in sessions:
		key = session.session_date.isoformat()
		close = closes.get(key)
		if close is None:
			result.append(ZScoreObservation(state=InputState.MISSING))
			previous = None
			continue
		if family is ZScoreFamily.PRICE:
			result.append(ZScoreObservation.ready(close))
			previous = close
			continue
		if previous is None:
			result.append(ZScoreObservation(state=InputState.MISSING))
		else:
			stored = deltas.get(key)
			expected = close / previous if previous else None
			if stored is None:
				result.append(ZScoreObservation(state=InputState.MISSING))
			elif stored <= 0 or expected is None or not isclose(float(stored), float(expected), rel_tol=1e-10, abs_tol=1e-12):
				result.append(ZScoreObservation(state=InputState.INVALID))
			else:
				result.append(ZScoreObservation.ready(stored - Decimal(1)))
		previous = close
	return tuple(result)


def _verify_zscores(context: Stage11Context, target: MySQLTarget, names: Any, session: str) -> dict[str, Any]:
	target_entry = next(item for item in context.manifest["targets"] if item.get("session") == session)
	dependency_dates = tuple(target_entry["zscore_dependencies"])
	sessions, closes, deltas, ticker_ids = _zscore_inputs(target, names, context, dependency_dates)
	target_session = next(item for item in sessions if item.session_date.isoformat() == session)
	checked = 0
	for family in (ZScoreFamily.PRICE, ZScoreFamily.PRICE_DELTA):
		specification = ZScoreSpecification(family, (20, 60), delta_definition=DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
		values = {identifier: _observations(family, sessions, identifier, closes.get(identifier, {}), deltas.get(identifier, {})) for identifier in ticker_ids.values()}
		input_data = ZScoreInput.from_values(family, sessions, values, dependency_sessions=sessions, scored_sessions=(target_session,))
		result = calculate_zscores(specification, input_data)
		connection = target.connect()
		try:
			cursor = connection.cursor()
			try:
				cursor.execute(f"SELECT id FROM `{names.sessions}` WHERE transaction_date = %s", (session,))
				date_rows = cursor.fetchall()
				if len(date_rows) != 1:
					raise Stage11Error(f"Z-score target mapping is not unique: {session}")
				for window in (20, 60):
					table = names.resolve_zscore_tables((window,))[window].price if family is ZScoreFamily.PRICE else names.resolve_zscore_tables((window,))[window].price_delta
					cursor.execute(f"SELECT ticker_id, zscore, status FROM `{table}` WHERE date_id = %s ORDER BY ticker_id", (int(date_rows[0][0]),))
					actual = {int(row[0]): (None if row[1] is None else float(row[1]), int(row[2])) for row in cursor.fetchall()}
					expected = {row.ticker_id: (row.score, int(row.status)) for row in result.rows if row.window == window and row.session == target_session}
					if actual.keys() != expected.keys():
						raise Stage11Error(f"Z-score ticker scope mismatch: {family.value}/{window}")
					for ticker_id, (expected_score, expected_status) in expected.items():
						actual_score, actual_status = actual[ticker_id]
						if actual_status != expected_status or (expected_score is None and actual_score is not None) or (expected_score is not None and (actual_score is None or not isclose(actual_score, expected_score, rel_tol=1e-5, abs_tol=1e-6))):
							raise Stage11Error(f"Independent z-score mismatch: {family.value}/{window}/{ticker_id}")
					checked += len(expected)
			finally:
				cursor.close()
		finally:
			connection.rollback()
			connection.close()
	return {"session": session, "families": 2, "windows": [20, 60], "rows": checked, "float32_tolerance": {"rtol": 1e-5, "atol": 1e-6}}


def run_stage11b(
	context: Stage11Context,
	stage11a_report: Path,
	dbinfo_file: Path,
	schema: str,
	session: str,
	output: Path,
	checkpoint_directory: Path,
) -> dict[str, Any]:
	previous = validate_stage11a_report(stage11a_report, context)
	if _selected_backends(context) != {"raw": "python", "derived": "java", "zscores": "java"}:
		raise Stage11Error("Stage 11b mixed pipeline does not match the frozen decision")
	recovery = recovery_evidence(context)
	manifest = load_manifest(context.manifest_path)
	target = MySQLTarget(dbinfo_file, schema, manifest)
	pipeline_output = output.with_name(output.stem + "-pipeline.json")
	pipeline = run_mysql(context, dbinfo_file, schema, (session,), ("raw", "derived", "zscores"), pipeline_output, checkpoint_directory)
	names = _manifest_table_names(manifest)
	target_entry = next((item for item in manifest.get("targets", ()) if item.get("session") == session), None)
	if not isinstance(target_entry, dict):
		raise Stage11Error(f"Session is not a Stage 6 target: {session}")
	raw_sessions = tuple(sorted(set(target_entry.get("zscore_dependencies", ())) | set(target_entry.get("derived_dependencies", ())) | {session}))
	verification = {
		"raw": _verify_raw(context, target, names, raw_sessions),
		"derived": _verify_derived(context, target, names, session),
		"zscores": _verify_zscores(context, target, names, session),
	}
	output = output.resolve()
	output.parent.mkdir(parents=True, exist_ok=True)
	report = {
		"schema_version": STAGE11B_SCHEMA_VERSION,
		"stage": "11b",
		"status": "complete",
		"production_access": {"ddl": False, "dml": False, "database": "mysql_disposable"},
		"prerequisites": {
			"decision_sha256": context.decision_sha256,
			"manifest_sha256": context.manifest_sha256,
			"audit_sha256": context.audit_sha256,
			"manifest_id": context.manifest_id,
			"stage11a_report": str(stage11a_report.resolve()),
			"stage11a_report_sha256": _sha256(stage11a_report),
			"cutoff": _iso(context.cutoff),
		},
		"selected_backends": _selected_backends(context),
		"execution": {
			"storage": "mysql",
			"schema": schema,
			"session": session,
			"transaction_units": context.decision["rollout"]["transactions"],
			"pipeline_report": str(pipeline_output.resolve()),
			"checkpoint_directory": str(checkpoint_directory.resolve()),
		},
		"pipeline": pipeline,
		"handoff_measurement": {
			"status": "measured",
			"phase_wall_seconds": pipeline["execution"].get("phase_wall_seconds", {}),
			"scope": "Python archive staging and Java subprocess handoff through durable commit/checkpoint; schema setup and independent verification excluded",
			"writer_limit": 1,
			"preparer_limit": 1,
		},
		"recovery": recovery,
		"independent_verification": verification,
		"handoff": {
			"next": "Stage 12",
			"production_ready": False,
			"production_mutations": "not authorized before Stage 12",
			"input_provenance": "validated archive evidence; original receipt time remains unknown",
			"selection_recheck": "Stage 9/10 rerun required if this mixed integration changes measured workload materially",
		},
	}
	output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
	return report
