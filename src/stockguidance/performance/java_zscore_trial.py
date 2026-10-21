from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import math
from pathlib import Path
import subprocess
import traceback

from stockguidance.database.table_names import quote_identifier
from stockguidance.market_data.identity import content_id
from stockguidance.performance.candidates import _file_hash, _manifest_table_names
from stockguidance.performance.mysql_candidates import MySQLTarget, load_manifest
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import InputState, ZScoreInput, ZScoreObservation
from stockguidance.zscores.specification import DELTA_DEFINITION, ZScoreFamily, ZScoreSpecification
from stockguidance.calendar.exchange import ExchangeCalendar


def _java_paths() -> str:

    java_root = Path(__file__).resolve().parents[3] / "java"
    return ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))


class ZScoreTrial:
    def __init__(self, manifest_path: Path, dbinfo_file: Path, schema: str, output: Path) -> None:
        self.manifest_path = manifest_path
        self.dbinfo_file = dbinfo_file
        self.schema_name = schema
        self.output = output
        self.manifest = load_manifest(manifest_path)
        self.files = {item["session"]: item for item in self.manifest["files"]}
        self.target = MySQLTarget(dbinfo_file, schema, self.manifest)
        self.schema = self.target.prepare()
        self.names = _manifest_table_names(self.manifest)
        self.target_session = self.manifest["target_blocks"]["chronological_20"][-1]
        target = next(item for item in self.manifest["targets"] if item["session"] == self.target_session)
        self.dependencies = tuple(target["zscore_dependencies"])
        self.classpath = _java_paths()

    def query(self, sql: str, parameters=(), *, write: bool = False):
        connection = self.target.connect()
        try:
            self.target.validate(connection)
            cursor = connection.cursor()
            try:
                cursor.execute(sql, parameters)
                if write:
                    connection.commit()
                    return cursor.rowcount
                return cursor.fetchall()
            finally:
                cursor.close()
        finally:
            connection.close()

    def stage_inputs(self) -> None:
        from stockguidance.performance.mysql_candidates import MySQLPythonBaseline

        reference = MySQLPythonBaseline(self.manifest, self.target, self.output.with_name("python-reference.json"))
        valid = tuple(session for session in self.dependencies if self.files[session]["valid"])
        reference._stage_raw(valid)
        reference._bars_cache.clear()
        connection = self.target.connect()
        try:
            cursor = connection.cursor()
            try:
                prices = quote_identifier(self.names.prices)
                deltas = quote_identifier(self.names.deltas)
                sessions = quote_identifier(self.names.sessions)
                for prior, current in zip(self.dependencies, self.dependencies[1:]):
                    cursor.execute(
                        f"INSERT INTO {deltas} (ticker_id, date_id, close_ratio_to_prior) "
                        f"SELECT current.ticker_id, current.date_id, current.close / prior.close "
                        f"FROM {prices} current JOIN {prices} prior ON prior.ticker_id = current.ticker_id "
                        f"WHERE current.date_id = (SELECT id FROM {sessions} WHERE transaction_date = %s) "
                        f"AND prior.date_id = (SELECT id FROM {sessions} WHERE transaction_date = %s) "
                        f"AND current.close IS NOT NULL AND prior.close IS NOT NULL AND prior.close <> 0 "
                        f"ON DUPLICATE KEY UPDATE close_ratio_to_prior = VALUES(close_ratio_to_prior)",
                        (current, prior),
                    )
                connection.commit()
            finally:
                cursor.close()
        finally:
            connection.close()

    def java(self, family: str, window: int, *, checkpoint: Path | None = None,
             interruption: str | None = None, manifest_path: Path | None = None, fail: bool = False) -> dict:
        command = ["java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "-cp", self.classpath, "stockguidance.migration.JavaZScoreMain",
                   "--manifest", str(manifest_path or self.manifest_path), "--dbinfo-file", str(self.dbinfo_file),
                   "--disposable-schema", self.schema_name, "--family", family, "--window", str(window),
                   "--session", self.target_session]
        if checkpoint is not None:
            command.extend(("--checkpoint", str(checkpoint)))
        if interruption is not None:
            command.extend(("--interrupt", interruption))
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        payload = json.loads(result.stdout)
        if (result.returncode != 0) != fail:
            raise AssertionError(f"Unexpected Java z-score outcome for {family}/{window}: {payload}")
        if interruption is not None and payload.get("failure_type") != "InterruptionException":
            raise AssertionError(f"Expected injected interruption, got {payload}")
        return payload

    def _ticker_ids(self) -> tuple[int, ...]:
        return tuple(sorted(int(item["id"]) for item in self.manifest["mappings"]["ticker_mappings"]))

    def _date_ids(self) -> dict[str, int]:
        rows = self.query(f"SELECT id, transaction_date FROM {quote_identifier(self.names.sessions)} WHERE transaction_date IN ({','.join('%s' for _ in self.dependencies)})", self.dependencies)
        return {str(row[1]): int(row[0]) for row in rows}

    def oracle(self, family: str, window: int) -> dict[int, tuple[float | None, int]]:
        ticker_ids = self._ticker_ids()
        date_ids = self._date_ids()
        raw_table = quote_identifier(self.names.prices)
        delta_table = quote_identifier(self.names.deltas)
        raw_rows = self.query(
            f"SELECT date_id, ticker_id, close FROM {raw_table} WHERE date_id IN ({','.join('%s' for _ in date_ids)}) ORDER BY ticker_id, date_id",
            tuple(date_ids.values()),
        )
        raw = {(int(ticker_id), int(date_id)): value for date_id, ticker_id, value in raw_rows}
        delta = {}
        if family == "price_delta":
            delta_rows = self.query(
                f"SELECT date_id, ticker_id, close_ratio_to_prior FROM {delta_table} WHERE date_id IN ({','.join('%s' for _ in date_ids)}) ORDER BY ticker_id, date_id",
                tuple(date_ids.values()),
            )
            delta = {(int(ticker_id), int(date_id)): value for date_id, ticker_id, value in delta_rows}
        imports = self.query(
            f"SELECT session_date, raw_id, available_at FROM {quote_identifier(self.names.imports)} WHERE session_date IN ({','.join('%s' for _ in self.dependencies)})",
            self.dependencies,
        )
        receipt = {str(session): (raw_id, available_at) for session, raw_id, available_at in imports}
        expected = {session: self.files[session].get("records_id") for session in self.dependencies}
        calendar = ExchangeCalendar("XNYS", date.fromisoformat(self.dependencies[0]), date.fromisoformat(self.target_session))
        sessions = tuple(calendar.session(date.fromisoformat(session)) for session in self.dependencies)
        observations: dict[int, tuple[ZScoreObservation, ...]] = {}
        for ticker_id in ticker_ids:
            values: list[ZScoreObservation] = []
            previous: tuple[InputState, float | None] | None = None
            for index, session in enumerate(sessions):
                session_key = session.session_date.isoformat()
                verified = expected[session_key] is not None and session_key in receipt and receipt[session_key][0] == expected[session_key] and bool(receipt[session_key][1])
                value = raw.get((ticker_id, date_ids[session_key]))
                if not verified:
                    close = (InputState.UNVERIFIED, None)
                elif value is None:
                    close = (InputState.MISSING, None)
                elif not _finite_positive(value):
                    close = (InputState.INVALID, None)
                else:
                    close = (InputState.AVAILABLE, float(value))
                if family == "price":
                    values.append(ZScoreObservation(value=close[1], state=close[0]))
                elif index == 0:
                    values.append(ZScoreObservation.insufficient_history())
                elif close[0] is not InputState.AVAILABLE or previous is None or previous[0] is not InputState.AVAILABLE:
                    priority = {InputState.INVALID: 4, InputState.UNVERIFIED: 3, InputState.MISSING: 2, InputState.INSUFFICIENT_HISTORY: 1}
                    state = max((close[0], previous[0]), key=lambda item: priority.get(item, 0))
                    values.append(ZScoreObservation(value=None, state=state))
                else:
                    ratio = delta.get((ticker_id, date_ids[session_key]))
                    expected_ratio = close[1] / previous[1]
                    if ratio is None:
                        values.append(ZScoreObservation.missing())
                    elif not _finite_positive(ratio) or abs(float(ratio) - expected_ratio) > max(1e-12, 1e-10 * abs(expected_ratio)):
                        values.append(ZScoreObservation.invalid(ratio))
                    else:
                        values.append(ZScoreObservation.ready(float(ratio) - 1.0))
                previous = close
            input_data = ZScoreInput.from_values(
                ZScoreFamily.PRICE if family == "price" else ZScoreFamily.PRICE_DELTA,
                sessions,
                {ticker_id: values},
                dependency_sessions=sessions,
                scored_sessions=(sessions[-1],),
            )
            specification = ZScoreSpecification(
                input_data.family, (window,),
                delta_definition=DELTA_DEFINITION if family == "price_delta" else "not_applicable",
            )
            row = calculate_zscores(specification, input_data).rows[0]
            oracle_value = None if row.score is None else float(row.score)
            observations[ticker_id] = (oracle_value, int(row.status))
        return observations

    def compare(self, family: str, window: int) -> dict:
        result = self.java(family, window)
        date_id = self._date_ids()[self.target_session]
        table = self.names.resolve_zscore_tables((window,))[window].price if family == "price" else self.names.resolve_zscore_tables((window,))[window].price_delta
        rows = self.query(f"SELECT ticker_id, zscore, status FROM {quote_identifier(table)} WHERE date_id = %s ORDER BY ticker_id", (date_id,))
        actual = {int(ticker_id): (None if score is None else float(score), int(status)) for ticker_id, score, status in rows}
        expected = self.oracle(family, window)
        if set(actual) != set(expected):
            raise AssertionError(f"Dense z-score ticker scope differs for {family}/{window}")
        for ticker_id, (score, status) in actual.items():
            expected_score, expected_status = expected[ticker_id]
            if status != expected_status:
                raise AssertionError(f"Status mismatch for {family}/{window}/{ticker_id}: {status} != {expected_status}")
            if expected_score is None:
                if score is not None:
                    raise AssertionError(f"Unavailable z-score has a value for {family}/{window}/{ticker_id}")
            elif score is None or abs(score - expected_score) > max(1e-6, 1e-5 * abs(expected_score)):
                raise AssertionError(f"Score mismatch for {family}/{window}/{ticker_id}")
        metadata = self.query(
            f"SELECT row_count, ready_count, status_counts, content_digest FROM {quote_identifier(self.names.zscore_sessions)} WHERE series_key = %s AND date_id = %s",
            (result["content_digest"] and self._series_key(family, window), date_id),
        )
        if len(metadata) != 1 or int(metadata[0][0]) != len(expected) or int(metadata[0][1]) != sum(status == 0 for _, status in expected.values()):
            raise AssertionError(f"Z-score metadata mismatch for {family}/{window}")
        return {"family": family, "window": window, "session": self.target_session, "row_count": len(actual), "ready_count": result["ready_count"], "content_digest": result["content_digest"]}

    def _series_key(self, family: str, window: int) -> str:
        definition = {
            "schema_version": 1,
            "source_scope": content_id(self.manifest["source"]["archive_source"]),
            "calendar_id": "XNYS",
            "family": family,
            "window": window,
            "calculation_version": "rolling-population-zscore-v1",
            "delta_definition": DELTA_DEFINITION if family == "price_delta" else "not_applicable",
            "precision": "float32",
            "missing_value_policy": "null_status_v1",
        }
        return content_id(definition)

    def snapshot(self) -> str:
        values = []
        for table in (self.names.zscore_series, self.names.zscore_sessions, "zscores_price_20", "zscores_price_60", "zscores_price_delta_20", "zscores_price_delta_60"):
            values.append([[None if value is None else str(value) for value in row] for row in self.query(f"SELECT * FROM {quote_identifier(table)} ORDER BY 1, 2")])
        return content_id(values)

    def recovery(self) -> list[str]:
        checks: list[str] = []
        family, window = "price", 20
        checkpoint = self.output.with_name(f"{self.schema_name}-java-zscore-checkpoint.json")
        before = self.snapshot()
        self.java(family, window, checkpoint=checkpoint, interruption="before_commit", fail=True)
        if self.snapshot() != before or checkpoint.exists():
            raise AssertionError("Z-score before-commit rollback failed")
        checks.append("before_commit_rollback")
        self.java(family, window, checkpoint=checkpoint, interruption="after_commit", fail=True)
        if self.snapshot() == before or checkpoint.exists():
            raise AssertionError("Z-score after-commit durability failed")
        checks.append("after_commit_durable_without_checkpoint")
        stable = self.snapshot()
        self.java(family, window, checkpoint=checkpoint)
        if self.snapshot() != stable or not checkpoint.exists():
            raise AssertionError("Z-score restart idempotency failed")
        checks.append("restart_idempotent")
        checkpoint.write_text('{"schema_version":1,"manifest_id":"stale"}', encoding="utf-8")
        self.java(family, window, checkpoint=checkpoint, fail=True)
        if self.snapshot() != stable:
            raise AssertionError("Stale z-score checkpoint changed storage")
        checks.append("stale_checkpoint_rejected")
        archive = next(Path(item["path"]) for item in self.manifest["files"] if item["session"] == self.dependencies[-1])
        original = archive.read_bytes()
        try:
            archive.write_bytes(original + b"\n")
            self.java(family, window, fail=True)
        finally:
            archive.write_bytes(original)
        if self.snapshot() != stable:
            raise AssertionError("Changed source was published")
        checks.append("changed_input_rejected")
        return checks


def _finite_positive(value: object) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def run_trial(manifest_path: Path, dbinfo_file: Path, schema: str, output: Path) -> dict:
    manifest = load_manifest(manifest_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    repository = Path(__file__).resolve().parents[3]
    code_files = [Path(__file__)]
    code_files.extend((repository / "java/src/main/java/stockguidance/migration").glob("Java*Score*.java"))
    report = {
        "schema_version": 1, "stage": "7b", "backend": "java", "storage_engine": "mysql",
        "status": "incomplete", "manifest_id": manifest["manifest_id"], "series": [], "recovery_checks": [],
        "comparison_eligible": False, "production_access": {"ddl": False, "dml": False},
        "protocol": {"writers": 1, "preparers": 1, "queued_sessions_max": 3, "java_heap": "-Xmx512m", "transaction_unit": "one series/session", "score_precision": "float32"},
        "code_hashes": {str(path.relative_to(repository)): _file_hash(path) for path in code_files},
    }

    def save() -> None:
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    try:
        trial = ZScoreTrial(manifest_path, dbinfo_file, schema, output)
        report["schema"] = trial.schema
        report["target_session"] = trial.target_session
        report["dependency_session_count"] = len(trial.dependencies)
        trial.stage_inputs()
        report["recovery_checks"] = trial.recovery()
        for family in ("price", "price_delta"):
            for window in (20, 60):
                report["series"].append(trial.compare(family, window))
                save()
        report["status"] = "complete"
        report["comparison_eligible"] = True
    except Exception as error:
        report["status"] = "failed"
        report["failure_type"] = type(error).__name__
        report["failure_message"] = str(error)
        report["failure_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
        save()
        raise RuntimeError("Disposable MySQL Java z-score trial failed; inspect its sanitized report") from None
    save()
    return report
