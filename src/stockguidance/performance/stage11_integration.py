from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from time import perf_counter
from typing import Any, Callable, Iterable

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.legacy_migration.audit import ARCHIVE_SOURCE
from stockguidance.legacy_migration.evidence import ArchiveEvidenceStore
from stockguidance.legacy_migration.precision import ArchiveFileInventory, ExactArchiveBar, exact_records_id, read_exact_archive
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.legacy_derived import DERIVED_COLUMNS, derived_id
from stockguidance.performance.candidates import _exact_derived_rows, _file_hash, _manifest_table_names, create_disposable_database
from stockguidance.performance.mysql_candidates import MySQLTarget, load_manifest
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import InputState, ZScoreInput, ZScoreObservation
from stockguidance.zscores.results import ZScoreStatus
from stockguidance.zscores.specification import DELTA_DEFINITION, ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.storage import ZScoreSeriesDefinition, rows_content_id, status_counts


STAGE11_SCHEMA_VERSION = 1
PROVENANCE_KIND = "adopted_archive_snapshot"
UNKNOWN_ORIGINAL_RECEIPT = None
WORKLOADS = ("raw", "derived", "zscores")


class Stage11Error(ValueError):
	pass


@dataclass(frozen=True)
class Stage11Context:
	decision: dict[str, Any]
	manifest: dict[str, Any]
	audit: dict[str, Any]
	decision_path: Path
	manifest_path: Path
	audit_path: Path
	evidence_path: Path
	cutoff: datetime
	manifest_id: str
	decision_sha256: str
	manifest_sha256: str
	audit_sha256: str


@dataclass(frozen=True)
class RawAdoption:
	session: str
	status: str
	changed_rows: int
	removed_rows: int
	record_count: int
	records_id: str | None
	content_digest: str | None
	invalidated_derived: tuple[str, ...]
	invalidated_zscores: tuple[str, ...]
	committed: bool


def _json(path: Path) -> dict[str, Any]:
	try:
		value = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
		raise Stage11Error(f"Cannot read Stage 11 artifact: {path}") from None
	if not isinstance(value, dict):
		raise Stage11Error(f"Stage 11 artifact must be an object: {path}")
	return value


def _sha256(path: Path) -> str:
	from hashlib import sha256
	digest = sha256()
	try:
		with path.open("rb") as stream:
			for block in iter(lambda: stream.read(1024 * 1024), b""):
				digest.update(block)
	except (OSError, UnicodeError):
		raise Stage11Error(f"Cannot hash Stage 11 artifact: {path}") from None
	return digest.hexdigest()


def _iso(value: datetime) -> str:
	return value.astimezone(timezone.utc).isoformat()


def _require_path(path: Path, name: str) -> Path:
	path = path.resolve()
	if not path.is_file():
		raise Stage11Error(f"Missing Stage 11 {name}: {path}")
	return path


def _selected(decision: dict[str, Any], workload: str) -> dict[str, Any]:
	try:
		value = decision["workloads"][workload]
	except (KeyError, TypeError):
		raise Stage11Error(f"Stage 10 decision has no {workload} selection") from None
	if not isinstance(value, dict) or value.get("selected_backend") not in {"python", "java", "sql"}:
		raise Stage11Error(f"Stage 10 decision has an invalid {workload} selection")
	return value


def validate_prerequisites(
	decision_path: Path,
	manifest_path: Path,
	audit_path: Path,
	evidence_path: Path,
	cutoff: datetime,
) -> Stage11Context:
	if cutoff.tzinfo is None or cutoff.utcoffset() is None:
		raise Stage11Error("Stage 11 cutoff must be timezone-aware")
	if cutoff > datetime.now(timezone.utc):
		raise Stage11Error("Stage 11 cutoff cannot be in the future")
	decision_path = _require_path(decision_path, "decision")
	manifest_path = _require_path(manifest_path, "manifest")
	audit_path = _require_path(audit_path, "audit report")
	evidence_path = evidence_path.resolve()
	if not evidence_path.is_dir():
		raise Stage11Error(f"Missing Stage 11 evidence directory: {evidence_path}")
	decision = _json(decision_path)
	manifest = _json(manifest_path)
	audit = _json(audit_path)
	if decision.get("schema_version") != 2 or decision.get("stage") != 10 or decision.get("status") != "complete":
		raise Stage11Error("Stage 10 decision is not complete")
	if decision.get("production_ready") is not False or decision.get("no_production_mutations") is not True:
		raise Stage11Error("Stage 10 decision does not carry the production-mutation gate")
	if manifest.get("stage") != 6 or manifest.get("schema_version") != 1:
		raise Stage11Error("Stage 6 manifest is not a supported Stage 11 input")
	unsigned_manifest = {key: value for key, value in manifest.items() if key != "manifest_id"}
	if content_id(unsigned_manifest) != manifest.get("manifest_id"):
		raise Stage11Error("Stage 6 manifest identity changed")
	if not audit.get("complete") or not audit.get("database_available"):
		raise Stage11Error("Stage 5c audit is incomplete")
	if not decision.get("inputs", {}).get("manifest", {}).get("sha256") == _sha256(manifest_path):
		raise Stage11Error("Stage 10 decision does not bind the current manifest")
	if not decision.get("inputs", {}).get("audit", {}).get("sha256") == _sha256(audit_path):
		raise Stage11Error("Stage 10 decision does not bind the current audit")
	if manifest.get("snapshot_identity", {}).get("audit_sha256") != _sha256(audit_path):
		raise Stage11Error("Manifest and audit identities differ")
	if not manifest.get("mappings", {}).get("ticker_mappings") or not manifest.get("mappings", {}).get("date_mappings"):
		raise Stage11Error("Stage 6 manifest mappings are incomplete")
	if _selected(decision, "raw_adoption_repair").get("selected_backend") != "python":
		raise Stage11Error("Stage 11a expects the frozen Python raw selection")
	if _selected(decision, "derived_computation_persistence").get("selected_backend") != "java":
		raise Stage11Error("Stage 11a expects the frozen Java derived selection")
	if _selected(decision, "zscores_persistence").get("selected_backend") != "java":
		raise Stage11Error("Stage 11a expects the frozen Java z-score selection")
	return Stage11Context(
		decision,
		manifest,
		audit,
		decision_path,
		manifest_path,
		audit_path,
		evidence_path,
		cutoff.astimezone(timezone.utc),
		str(manifest["manifest_id"]),
		_sha256(decision_path),
		_sha256(manifest_path),
		_sha256(audit_path),
	)


def _sqlite_identifier(value: str) -> str:
	if not value.replace("_", "").isalnum() or not value or value[0].isdigit():
		raise Stage11Error("Invalid registry identifier")
	return '"' + value.replace('"', '""') + '"'


def _decimal_text(value: Decimal | int | float | None) -> str | None:
	if value is None:
		return None
	return format(Decimal(str(value)).normalize(), "f")


def _bar_values(bar: ExactArchiveBar) -> tuple[str | None, ...]:
	return tuple(_decimal_text(value) for value in (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap)) + (bar.transactions,)


def _records_digest(bars: Iterable[ExactArchiveBar]) -> str:
	return exact_records_id(tuple(sorted(bars, key=lambda item: item.ticker)))


class Stage11Store:
	def __init__(self, context: Stage11Context, database: Path, *, windows: tuple[int, ...] = (20, 60), now: Callable[[], datetime] | None = None) -> None:
		if not database.is_absolute():
			database = database.resolve()
		self.context = context
		self.database = database
		self.names = _manifest_table_names(context.manifest)
		self.windows = windows
		self.now = now or (lambda: datetime.now(timezone.utc))

	def initialize(self) -> dict[str, Any]:
		metadata = create_disposable_database(
			self.database,
			self.names,
			self.windows,
			data_root=Path(self.context.manifest["inputs"]["data_root"]),
		)
		with sqlite3.connect(self.database) as connection:
			self._create_stage11_tables(connection)
			connection.executemany(f"INSERT INTO {_sqlite_identifier(self.names.tickers)}(id, label) VALUES (?, ?)", [(item["id"], item["label"]) for item in self.context.manifest["mappings"]["ticker_mappings"]])
			connection.executemany(f"INSERT INTO {_sqlite_identifier(self.names.sessions)}(id, transaction_date) VALUES (?, ?)", [(item["id"], item["session"]) for item in self.context.manifest["mappings"]["date_mappings"]])
		return metadata | {"stage11_schema_version": STAGE11_SCHEMA_VERSION}

	def _connect(self) -> sqlite3.Connection:
		connection = sqlite3.connect(self.database)
		connection.execute("PRAGMA foreign_keys = ON")
		return connection

	def _create_stage11_tables(self, connection: sqlite3.Connection) -> None:
		connection.execute("CREATE TABLE stage11_adoption_receipts (session_date TEXT PRIMARY KEY, manifest_id TEXT NOT NULL, source_key TEXT NOT NULL, source_provider TEXT NOT NULL, source_path TEXT NOT NULL, file_sha256 TEXT NOT NULL, records_id TEXT NOT NULL, record_count INTEGER NOT NULL, provenance_kind TEXT NOT NULL, adopted_at TEXT NOT NULL, original_receipt_at TEXT, cutoff TEXT NOT NULL, evidence_id TEXT NOT NULL, content_digest TEXT NOT NULL)")
		connection.execute("CREATE TABLE stage11_completion (workload TEXT NOT NULL, session_date TEXT NOT NULL, series_key TEXT NOT NULL, content_digest TEXT NOT NULL, committed_at TEXT NOT NULL, PRIMARY KEY(workload, session_date, series_key))")
		connection.execute("INSERT INTO stage6_disposable_metadata(key, value) VALUES (?, ?)", ("stage11_manifest_id", self.context.manifest_id))

	def _file(self, session: str) -> tuple[dict[str, Any], Path]:
		item = next((value for value in self.context.manifest["files"] if value.get("session") == session), None)
		if not isinstance(item, dict) or not item.get("valid") or not item.get("path"):
			raise Stage11Error(f"Session is not an adoptable validated archive file: {session}")
		path = Path(item["path"]).resolve()
		if not path.is_file() or _file_hash(path) != item.get("file_sha256"):
			raise Stage11Error(f"Changed Stage 11 archive input: {session}")
		return item, path

	def _archive(self, session: str) -> tuple[dict[str, Any], Path, tuple[ExactArchiveBar, ...], Any]:
		item, path = self._file(session)
		try:
			bars, file_hash = read_exact_archive(path)
		except Exception as error:
			raise Stage11Error(f"Invalid Stage 11 archive input: {session}") from error
		if file_hash != item.get("file_sha256") or _records_digest(bars) != item.get("records_id") or len(bars) != item.get("record_count"):
			raise Stage11Error(f"Changed Stage 11 record identity: {session}")
		inventory = ArchiveFileInventory(str(path), session, file_hash, True, len(bars), _records_digest(bars), tuple(bar.ticker for bar in bars))
		try:
			evidence = ArchiveEvidenceStore(self.context.evidence_path).verify(ARCHIVE_SOURCE, session, inventory)
		except ValueError as error:
			raise Stage11Error(f"Missing or conflicting Stage 11 evidence: {session}") from error
		if evidence.validated_at > self.context.cutoff or evidence.original_path != str(path):
			raise Stage11Error(f"Archive evidence is outside the Stage 11 cutoff: {session}")
		return item, path, bars, evidence

	def _date_id(self, connection: sqlite3.Connection, session: str) -> int:
		table = _sqlite_identifier(self.names.sessions)
		rows = connection.execute(f"SELECT id FROM {table} WHERE transaction_date = ?", (session,)).fetchall()
		if len(rows) != 1:
			raise Stage11Error(f"Session mapping is not unique: {session}")
		return int(rows[0][0])

	def _ticker_ids(self, connection: sqlite3.Connection) -> dict[str, int]:
		table = _sqlite_identifier(self.names.tickers)
		rows = connection.execute(f"SELECT id, label FROM {table} ORDER BY id").fetchall()
		result: dict[str, int] = {}
		for identifier, label in rows:
			if label in result:
				raise Stage11Error("Duplicate exact-case ticker mapping")
			result[str(label)] = int(identifier)
		return result

	def _ensure_ticker(self, connection: sqlite3.Connection, ids: dict[str, int], label: str) -> int:
		identifier = ids.get(label)
		if identifier is not None:
			return identifier
		identifier = max(ids.values(), default=0) + 1
		connection.execute(f"INSERT INTO {_sqlite_identifier(self.names.tickers)}(id, label) VALUES (?, ?)", (identifier, label))
		ids[label] = identifier
		return identifier

	def _stored_bars(self, connection: sqlite3.Connection, date_id: int) -> dict[str, tuple[int, ...]]:
		prices = _sqlite_identifier(self.names.prices)
		tickers = _sqlite_identifier(self.names.tickers)
		rows = connection.execute(f"SELECT p.ticker_id, t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM {prices} p JOIN {tickers} t ON t.id = p.ticker_id WHERE p.date_id = ?", (date_id,)).fetchall()
		result: dict[str, tuple[int, ...]] = {}
		for row in rows:
			if str(row[1]) in result:
				raise Stage11Error("Duplicate stored exact-case ticker row")
			result[str(row[1])] = (int(row[0]), *row[2:])
		return result

	def _invalidate_dependencies(self, connection: sqlite3.Connection, changed_session: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
		derived: set[str] = set()
		zscore: set[str] = set()
		for target in self.context.manifest.get("targets", []):
			session = str(target.get("session"))
			if session == changed_session or changed_session in target.get("derived_dependencies", []):
				derived.add(session)
			if session == changed_session or changed_session in target.get("zscore_dependencies", []):
				zscore.add(session)
			if session in derived:
				date_id = self._date_id(connection, session)
				connection.execute(f"DELETE FROM {_sqlite_identifier(self.names.deltas)} WHERE date_id = ?", (date_id,))
				connection.execute(f"UPDATE {_sqlite_identifier(self.names.imports)} SET derived_dependency_id = NULL, derived_content_id = NULL WHERE session_date = ?", (session,))
				connection.execute("DELETE FROM stage11_completion WHERE workload = ? AND session_date = ?", ("derived", session))
				connection.execute("DELETE FROM stage6_completion WHERE workload = ? AND session_date = ?", ("derived", session))
		resolved = self.names.resolve_zscore_tables(self.windows)
		for session in zscore:
			date_id = self._date_id(connection, session)
			for tables in resolved.values():
				for table in (tables.price, tables.price_delta):
					connection.execute(f"DELETE FROM {_sqlite_identifier(table)} WHERE date_id = ?", (date_id,))
			connection.execute(f"DELETE FROM {_sqlite_identifier(self.names.zscore_sessions)} WHERE date_id = ?", (date_id,))
			connection.execute("DELETE FROM stage11_completion WHERE workload = ? AND session_date = ?", ("zscores", session))
			connection.execute("DELETE FROM stage6_completion WHERE workload = ? AND session_date = ?", ("zscores", session))
		return tuple(sorted(derived)), tuple(sorted(zscore))

	def adopt_raw(self, session: str, *, interruption: str | None = None) -> RawAdoption:
		item, path, bars, evidence = self._archive(session)
		date_id = None
		started = perf_counter()
		connection = self._connect()
		try:
			connection.execute("BEGIN IMMEDIATE")
			date_id = self._date_id(connection, session)
			ids = self._ticker_ids(connection)
			stored = self._stored_bars(connection, date_id)
			expected = {bar.ticker: bar for bar in bars}
			changed = 0
			for label, bar in expected.items():
				identifier = self._ensure_ticker(connection, ids, label)
				new_values = _bar_values(bar)
				old = stored.get(label)
				if old is not None and tuple(old[1:]) == new_values:
					continue
				connection.execute(f"INSERT OR REPLACE INTO {_sqlite_identifier(self.names.prices)}(ticker_id, date_id, open, high, low, close, volume, vwap, transactions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (identifier, date_id, *new_values))
				changed += 1
			removed = 0
			for label, old in stored.items():
				if label not in expected:
					connection.execute(f"DELETE FROM {_sqlite_identifier(self.names.prices)} WHERE ticker_id = ? AND date_id = ?", (old[0], date_id))
					removed += 1
			invalidated_derived: tuple[str, ...] = ()
			invalidated_zscores: tuple[str, ...] = ()
			if changed or removed:
				invalidated_derived, invalidated_zscores = self._invalidate_dependencies(connection, session)
			adopted_at = _iso(self.now())
			records_digest = _records_digest(bars)
			content = content_id({"session": session, "records_id": records_digest, "file_sha256": item["file_sha256"]})
			connection.execute(f"INSERT OR REPLACE INTO {_sqlite_identifier(self.names.imports)}(session_date, raw_id, available_at, derived_dependency_id, derived_content_id) SELECT ?, ?, ?, CASE WHEN ? THEN NULL ELSE derived_dependency_id END, CASE WHEN ? THEN NULL ELSE derived_content_id END FROM (SELECT ? AS session_date) LEFT JOIN {_sqlite_identifier(self.names.imports)} existing USING(session_date)", (session, records_digest, adopted_at, bool(changed or removed), bool(changed or removed), session))
			connection.execute("INSERT OR REPLACE INTO stage11_adoption_receipts(session_date, manifest_id, source_key, source_provider, source_path, file_sha256, records_id, record_count, provenance_kind, adopted_at, original_receipt_at, cutoff, evidence_id, content_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (session, self.context.manifest_id, self.context.manifest["snapshot_identity"]["source_key"], evidence.source.provider, str(path), item["file_sha256"], records_digest, len(bars), PROVENANCE_KIND, adopted_at, UNKNOWN_ORIGINAL_RECEIPT, _iso(self.context.cutoff), evidence.evidence_id, content))
			connection.execute("INSERT OR REPLACE INTO stage11_completion(workload, session_date, series_key, content_digest, committed_at) VALUES (?, ?, ?, ?, ?)", ("raw", session, "raw-adoption-v1", content, adopted_at))
			if interruption == "before_commit":
				raise RuntimeError("Injected before_commit interruption")
			connection.commit()
			if interruption == "after_commit":
				raise RuntimeError("Injected after_commit interruption")
			self._write_checkpoint("raw", session, content)
			return RawAdoption(session, "complete", changed, removed, len(bars), records_digest, content, invalidated_derived, invalidated_zscores, True)
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def _write_checkpoint(self, workload: str, session: str, digest: str, directory: Path | None = None, suffix: str | None = None) -> Path:
		directory = directory or self.database.parent
		directory.mkdir(parents=True, exist_ok=True)
		label = f"-{suffix}" if suffix else ""
		path = directory / f"stage11-{workload}-{session}{label}.checkpoint.json"
		temporary = path.with_suffix(".tmp")
		temporary.write_text(json.dumps({"schema_version": STAGE11_SCHEMA_VERSION, "manifest_id": self.context.manifest_id, "workload": workload, "session": session, "content_digest": digest, "after_commit": True, "committed_at": _iso(self.now())}, sort_keys=True) + "\n", encoding="utf-8")
		temporary.replace(path)
		return path

	def _require_stored_archive(self, connection: sqlite3.Connection, session: str, bars: tuple[ExactArchiveBar, ...]) -> int:
		date_id = self._date_id(connection, session)
		stored = self._stored_bars(connection, date_id)
		expected = {bar.ticker: _bar_values(bar) for bar in bars}
		actual = {label: tuple(row[1:]) for label, row in stored.items()}
		if actual != expected:
			raise Stage11Error(f"Stored raw dependency differs from validated archive: {session}")
		return date_id

	def write_derived(self, session: str, *, interruption: str | None = None) -> dict[str, Any]:
		target = next((item for item in self.context.manifest.get("targets", []) if item.get("session") == session), None)
		if not isinstance(target, dict) or len(target.get("derived_dependencies", [])) != 2:
			return {"session": session, "status": "missing_dependency", "committed": False}
		prior, following = tuple(target["derived_dependencies"])
		_, _, current, _ = self._archive(session)
		_, _, previous, _ = self._archive(prior)
		_, _, next_bars, _ = self._archive(following)
		rows = _exact_derived_rows(previous, current, next_bars)
		dependency_id = content_id({"prior": prior, "following": following, "raw": [_records_digest(previous), _records_digest(next_bars)]})
		content = derived_id(rows)
		connection = self._connect()
		try:
			connection.execute("BEGIN IMMEDIATE")
			date_id = self._require_stored_archive(connection, prior, previous)
			self._require_stored_archive(connection, session, current)
			self._require_stored_archive(connection, following, next_bars)
			date_id = self._date_id(connection, session)
			connection.execute(f"DELETE FROM {_sqlite_identifier(self.names.deltas)} WHERE date_id = ?", (date_id,))
			ids = self._ticker_ids(connection)
			payload = []
			for row in rows:
				payload.append((self._ensure_ticker(connection, ids, row.ticker), date_id, *row.values))
			if payload:
				columns = ",".join(DERIVED_COLUMNS)
				placeholders = ",".join("?" for _ in range(len(DERIVED_COLUMNS) + 2))
				connection.executemany(f"INSERT INTO {_sqlite_identifier(self.names.deltas)}(ticker_id, date_id, {columns}) VALUES ({placeholders})", payload)
			committed_at = _iso(self.now())
			connection.execute(f"UPDATE {_sqlite_identifier(self.names.imports)} SET derived_dependency_id = ?, derived_content_id = ? WHERE session_date = ?", (dependency_id, content, session))
			connection.execute("INSERT OR REPLACE INTO stage11_completion(workload, session_date, series_key, content_digest, committed_at) VALUES (?, ?, ?, ?, ?)", ("derived", session, "legacy-open-to-close-v1", content, committed_at))
			if interruption == "before_commit":
				raise RuntimeError("Injected before_commit interruption")
			connection.commit()
			if interruption == "after_commit":
				raise RuntimeError("Injected after_commit interruption")
			self._write_checkpoint("derived", session, content)
			return {"session": session, "status": "complete", "rows": len(rows), "content_digest": content, "committed": True}
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def _calendar(self, sessions: Iterable[str]) -> tuple[Any, ...]:
		dates = tuple(sorted(date.fromisoformat(value) for value in sessions))
		if not dates:
			raise Stage11Error("Z-score integration requires dependency sessions")
		calendar = ExchangeCalendar("XNYS", min(dates), max(dates))
		return tuple(calendar.session(value) for value in dates)

	def write_zscores(self, session: str, *, interruption: str | None = None) -> dict[str, Any]:
		target = next((item for item in self.context.manifest.get("targets", []) if item.get("session") == session), None)
		if not isinstance(target, dict) or not target.get("zscore_dependencies"):
			return {"session": session, "status": "missing_dependency", "committed": False}
		dependency_dates = tuple(target["zscore_dependencies"])
		trading_sessions = self._calendar(dependency_dates)
		current = next(value for value in trading_sessions if value.session_date.isoformat() == session)
		connection = self._connect()
		try:
			ids = self._ticker_ids(connection)
			for dependency in dependency_dates:
				item, _, bars, _ = self._archive(dependency)
				self._require_stored_archive(connection, dependency, bars)
			if not ids:
				raise Stage11Error("Z-score integration has no ticker mappings")
			for family in (ZScoreFamily.PRICE, ZScoreFamily.PRICE_DELTA):
				specification = ZScoreSpecification(family, self.windows, delta_definition=DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
				values: dict[int, list[ZScoreObservation]] = {}
				for label, identifier in sorted(ids.items(), key=lambda item: item[1]):
					closes = self._close_values(connection, identifier, trading_sessions)
					observations: list[ZScoreObservation] = []
					previous: Decimal | None = None
					for trading_session in trading_sessions:
						close = closes.get(trading_session.session_date.isoformat())
						if close is None:
							observations.append(ZScoreObservation(state=InputState.MISSING))
							previous = None
						elif family is ZScoreFamily.PRICE:
							observations.append(ZScoreObservation.ready(close))
							previous = close
						elif previous is None:
							observations.append(ZScoreObservation(state=InputState.MISSING))
							previous = close
						else:
							observations.append(ZScoreObservation.ready((close / previous) - Decimal(1)))
							previous = close
					values[identifier] = observations
				input_data = ZScoreInput.from_values(family, trading_sessions, values, dependency_sessions=trading_sessions, scored_sessions=(current,))
				result = calculate_zscores(specification, input_data)
				for window in self.windows:
					rows = tuple(row for row in result.rows if row.window == window and row.session == current)
					connection.execute("BEGIN IMMEDIATE")
					for dependency in dependency_dates:
						_, _, dependency_bars, _ = self._archive(dependency)
						self._require_stored_archive(connection, dependency, dependency_bars)
					self._publish_zscores(connection, session, family, window, rows, trading_sessions)
					if interruption == "before_commit":
						raise RuntimeError("Injected before_commit interruption")
					connection.commit()
					if interruption == "after_commit":
						raise RuntimeError("Injected after_commit interruption")
					self._write_checkpoint("zscores", session, rows_content_id(rows), suffix=f"{family.value}-{window}")
			return {"session": session, "status": "complete", "series": len(self.windows) * 2, "committed": True}
		except Exception:
			connection.rollback()
			raise
		finally:
			connection.close()

	def _close_values(self, connection: sqlite3.Connection, ticker_id: int, sessions: tuple[Any, ...]) -> dict[str, Decimal]:
		if not sessions:
			return {}
		date_ids = {session.session_date.isoformat(): self._date_id(connection, session.session_date.isoformat()) for session in sessions}
		placeholders = ",".join("?" for _ in date_ids)
		rows = connection.execute(f"SELECT date_id, close FROM {_sqlite_identifier(self.names.prices)} WHERE ticker_id = ? AND date_id IN ({placeholders})", (ticker_id, *date_ids.values())).fetchall()
		by_id = {identifier: value for identifier, value in rows}
		return {session: Decimal(str(by_id[identifier])) for session, identifier in date_ids.items() if by_id.get(identifier) is not None}

	def _publish_zscores(self, connection: sqlite3.Connection, session: str, family: ZScoreFamily, window: int, rows: tuple[Any, ...], trading_sessions: tuple[Any, ...]) -> None:
		definition = ZScoreSeriesDefinition(content_id(self.context.manifest["source"]["archive_source"]), "XNYS", family, window, "rolling-population-zscore-v1", "float32", DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
		date_id = self._date_id(connection, session)
		tables = self.names.resolve_zscore_tables((window,))[window]
		table = tables.price_delta if family is ZScoreFamily.PRICE_DELTA else tables.price
		connection.execute(f"DELETE FROM {_sqlite_identifier(table)} WHERE date_id = ?", (date_id,))
		for row in rows:
			connection.execute(f"INSERT INTO {_sqlite_identifier(table)}(date_id, ticker_id, zscore, status) VALUES (?, ?, ?, ?)", (date_id, row.ticker_id, row.score, int(row.status)))
		content = rows_content_id(rows)
		counts = dict(status_counts(rows))
		dependency = content_id([value.session_date.isoformat() for value in trading_sessions])
		connection.execute(f"INSERT OR REPLACE INTO {_sqlite_identifier(self.names.zscore_series)}(series_key, schema_version, source_scope, calendar_id, family, window, calculation_version, delta_definition, precision, missing_value_policy, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (definition.series_key, definition.schema_version, definition.source_scope, definition.calendar_id, family.value, window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy, _iso(self.now())))
		connection.execute(f"INSERT OR REPLACE INTO {_sqlite_identifier(self.names.zscore_sessions)}(series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (definition.series_key, date_id, dependency, content_id({"session": session, "family": family.value, "window": window}), True, _iso(self.now()), len(rows), counts.get(ZScoreStatus.READY, 0), json.dumps({str(int(status)): count for status, count in counts.items()}, sort_keys=True), content))
		connection.execute("INSERT OR REPLACE INTO stage11_completion(workload, session_date, series_key, content_digest, committed_at) VALUES (?, ?, ?, ?, ?)", ("zscores", session, definition.series_key, content, _iso(self.now())))

def _commands(context: Stage11Context, schema: str | None, checkpoint: Path, sessions: tuple[str, ...]) -> dict[str, Any]:
	paths = {"decision": str(context.decision_path), "evidence": str(context.evidence_path), "cutoff": _iso(context.cutoff)}
	base = [sys.executable, "-m", "stockguidance.cli.legacy_migration_benchmark", "--run-stage11a", "--stage11-decision", str(context.decision_path), "--manifest", str(context.manifest_path), "--audit-report", str(context.audit_path), "--stage11-evidence-path", str(context.evidence_path), "--stage11-cutoff", _iso(context.cutoff), "--stage11-sessions", ",".join(sessions)]
	if schema is None:
		return {"audit": [sys.executable, "-m", "stockguidance.cli.legacy_migration", "--mode", "audit", "--decision-file", str(context.decision_path), "--evidence-path", str(context.evidence_path)], "apply": "not authorized before Stage 12", "pilot": "not authorized before Stage 12", "selected": {"raw": "python:stockguidance.performance.stage11_integration", "derived": "java:stockguidance.migration.JavaDerivedBenchmarkMain", "zscores": "java:stockguidance.migration.JavaZScoreBenchmarkMain"}, "zscore": ["java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "stockguidance.migration.JavaZScoreBenchmarkMain"], "invocation": base + ["--stage11-storage", "sqlite", "--stage11-target", str((checkpoint / "stage11a-disposable.sqlite").resolve())], "paths": paths, "sessions": list(sessions), "checkpoint_directory": str(checkpoint)}
	return {"audit": [sys.executable, "-m", "stockguidance.cli.legacy_migration", "--mode", "audit", "--decision-file", str(context.decision_path), "--evidence-path", str(context.evidence_path)], "apply": "not authorized before Stage 12", "pilot": "not authorized before Stage 12", "selected": {"raw": ["python", "stockguidance.performance.stage11_integration", "--storage", "mysql", "--disposable-schema", schema], "derived": ["java", "-Xmx512m", "stockguidance.migration.JavaDerivedBenchmarkMain", "--disposable-schema", schema], "zscores": ["java", "-Xmx512m", "stockguidance.migration.JavaZScoreBenchmarkMain", "--disposable-schema", schema]}, "zscore": ["java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "stockguidance.migration.JavaZScoreBenchmarkMain", "--disposable-schema", schema], "invocation": base + ["--stage11-storage", "mysql", "--stage11-disposable-schema", schema, "--stage11-checkpoint-directory", str(checkpoint)], "paths": paths, "sessions": list(sessions), "checkpoint_directory": str(checkpoint)}


def run_offline(
	context: Stage11Context,
	target: Path,
	sessions: tuple[str, ...],
	workloads: tuple[str, ...],
	output: Path,
	*,
	cutoff: datetime | None = None,
) -> dict[str, Any]:
	if not sessions or len(sessions) > 3:
		raise Stage11Error("Stage 11a offline operation permits one to three sessions")
	if any(workload not in WORKLOADS for workload in workloads) or not workloads:
		raise Stage11Error("Invalid Stage 11 workload selection")
	if cutoff is not None and cutoff != context.cutoff:
		raise Stage11Error("Stage 11 cutoff does not match prerequisite validation")
	runner = Stage11Store(context, target)
	schema = runner.initialize()
	results: dict[str, Any] = {"raw": [], "derived": [], "zscores": []}
	ordered = tuple(sorted(sessions))
	archive_sessions = set(ordered)
	for session in ordered:
		if "derived" in workloads:
			target = next((item for item in context.manifest.get("targets", []) if item.get("session") == session), None)
			archive_sessions.update(target.get("derived_dependencies", ()) if isinstance(target, dict) else ())
		if "zscores" in workloads:
			target = next((item for item in context.manifest.get("targets", []) if item.get("session") == session), None)
			archive_sessions.update(target.get("zscore_dependencies", ()) if isinstance(target, dict) else ())
	for raw_session in sorted(archive_sessions):
		results["raw"].append(runner.adopt_raw(raw_session).__dict__)
	for session in ordered:
		if "derived" in workloads:
			results["derived"].append(runner.write_derived(session))
	for session in ordered:
		if "zscores" in workloads:
			results["zscores"].append(runner.write_zscores(session))
	output = output.resolve()
	output.parent.mkdir(parents=True, exist_ok=True)
	report = {
		"schema_version": STAGE11_SCHEMA_VERSION,
		"stage": "11a",
		"status": "complete",
		"production_access": {"ddl": False, "dml": False, "database": "offline_disposable_sqlite"},
		"prerequisites": {"decision_sha256": context.decision_sha256, "manifest_sha256": context.manifest_sha256, "audit_sha256": context.audit_sha256, "manifest_id": context.manifest_id, "audit_complete": True, "cutoff": _iso(context.cutoff)},
		"selected_backends": {workload: _selected(context.decision, key)["selected_backend"] for workload, key in (("raw", "raw_adoption_repair"), ("derived", "derived_computation_persistence"), ("zscores", "zscores_persistence"))},
		"execution": {"storage": "sqlite", "target": str(target.resolve()), "schema": schema, "sessions": list(ordered), "workloads": list(workloads), "transaction_units": context.decision["rollout"]["transactions"]},
		"operations": results,
		"commands": _commands(context, None, target.parent, ordered),
		"handoff": {"next": "Stage 11b", "production_ready": False, "checkpoint_after_commit": True, "original_receipt_time": "unknown_and_null", "chronological_rebuild": True},
	}
	output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
	return report


def _stage11_mysql_tables() -> tuple[str, ...]:
	return (
		"CREATE TABLE stage11_adoption_receipts (session_date DATE PRIMARY KEY, manifest_id VARCHAR(64) NOT NULL, source_key VARCHAR(64) NOT NULL, source_provider VARCHAR(32) NOT NULL, source_path VARCHAR(1024) NOT NULL, file_sha256 CHAR(64) NOT NULL, records_id CHAR(64) NOT NULL, record_count BIGINT NOT NULL, provenance_kind VARCHAR(64) NOT NULL, adopted_at VARCHAR(64) NOT NULL, original_receipt_at VARCHAR(64) NULL, cutoff VARCHAR(64) NOT NULL, evidence_id CHAR(64) NOT NULL, content_digest CHAR(64) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
		"CREATE TABLE stage11_completion (workload VARCHAR(32) NOT NULL, session_date DATE NOT NULL, series_key VARCHAR(128) NOT NULL, content_digest CHAR(64) NOT NULL, committed_at VARCHAR(64) NOT NULL, PRIMARY KEY(workload, session_date, series_key)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin",
	)


def _java_classpath() -> str:
	java_root = Path(__file__).resolve().parents[3] / "java"
	return ";".join(str(java_root / value) for value in ("target/classes", "target/dependency/*", "lib/mysql-connector-j-9.6.0.jar"))


def _mysql_adopt_raw(target: MySQLTarget, context: Stage11Context, manifest: dict[str, Any], session: str, item: dict[str, Any], path: Path, bars: tuple[ExactArchiveBar, ...], evidence: Any) -> dict[str, Any]:
	names = _manifest_table_names(manifest)
	connection = target.connect()
	try:
		target.validate(connection)
		connection.start_transaction()
		cursor = connection.cursor()
		try:
			cursor.execute(f"SELECT id FROM `{names.sessions}` WHERE transaction_date = %s FOR UPDATE", (session,))
			date_rows = cursor.fetchall()
			if len(date_rows) != 1:
				raise Stage11Error(f"Session mapping is not unique: {session}")
			date_id = int(date_rows[0][0])
			cursor.execute(f"SELECT id, label FROM `{names.tickers}` ORDER BY id FOR UPDATE")
			ids = {str(label): int(identifier) for identifier, label in cursor.fetchall()}
			cursor.execute(f"SELECT p.ticker_id, t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM `{names.prices}` p JOIN `{names.tickers}` t ON t.id = p.ticker_id WHERE p.date_id = %s FOR UPDATE", (date_id,))
			stored = {str(row[1]): (int(row[0]), *row[2:]) for row in cursor.fetchall()}
			changed = 0
			for bar in bars:
				identifier = ids.get(bar.ticker)
				if identifier is None:
					identifier = max(ids.values(), default=0) + 1
					cursor.execute(f"INSERT INTO `{names.tickers}`(id, label) VALUES (%s, %s)", (identifier, bar.ticker))
					ids[bar.ticker] = identifier
				new_values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)
				old = stored.get(bar.ticker)
				old_values = None if old is None else old[1:]
				if old_values is not None and all((left is None and right is None) or (left is not None and right is not None and Decimal(str(left)) == Decimal(str(right))) for left, right in zip(old_values, new_values, strict=True)):
					continue
				cursor.execute(f"INSERT INTO `{names.prices}`(ticker_id, date_id, open, high, low, close, volume, vwap, transactions) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close), volume=VALUES(volume), vwap=VALUES(vwap), transactions=VALUES(transactions)", (identifier, date_id, *new_values))
				changed += 1
			removed = 0
			for label, old in stored.items():
				if label not in {bar.ticker for bar in bars}:
					cursor.execute(f"DELETE FROM `{names.prices}` WHERE ticker_id = %s AND date_id = %s", (old[0], date_id))
					removed += 1
			derived_sessions = {str(target.get("session")) for target in manifest.get("targets", []) if str(target.get("session")) == session or session in target.get("derived_dependencies", [])}
			zscore_sessions = {str(target.get("session")) for target in manifest.get("targets", []) if str(target.get("session")) == session or session in target.get("zscore_dependencies", [])}
			if changed or removed:
				for affected in sorted(derived_sessions):
					cursor.execute(f"SELECT id FROM `{names.sessions}` WHERE transaction_date = %s FOR UPDATE", (affected,))
					affected_rows = cursor.fetchall()
					if len(affected_rows) != 1:
						raise Stage11Error(f"Derived dependency mapping is not unique: {affected}")
					cursor.execute(f"DELETE FROM `{names.deltas}` WHERE date_id = %s", (int(affected_rows[0][0]),))
					cursor.execute(f"UPDATE `{names.imports}` SET derived_dependency_id = NULL, derived_content_id = NULL WHERE session_date = %s", (affected,))
				for affected in sorted(zscore_sessions):
					cursor.execute(f"SELECT id FROM `{names.sessions}` WHERE transaction_date = %s FOR UPDATE", (affected,))
					affected_rows = cursor.fetchall()
					if len(affected_rows) != 1:
						raise Stage11Error(f"Z-score dependency mapping is not unique: {affected}")
					for tables in names.resolve_zscore_tables((20, 60)).values():
						for table in (tables.price, tables.price_delta):
							cursor.execute(f"DELETE FROM `{table}` WHERE date_id = %s", (int(affected_rows[0][0]),))
					cursor.execute(f"DELETE FROM `{names.zscore_sessions}` WHERE date_id = %s", (int(affected_rows[0][0]),))
			adopted_at = _iso(datetime.now(timezone.utc))
			records_digest = exact_records_id(bars)
			content = content_id({"session": session, "records_id": records_digest, "file_sha256": item["file_sha256"]})
			if changed or removed:
				cursor.execute(f"INSERT INTO `{names.imports}`(session_date, raw_id, available_at, derived_dependency_id, derived_content_id) VALUES (%s, %s, %s, NULL, NULL) ON DUPLICATE KEY UPDATE raw_id=VALUES(raw_id), available_at=VALUES(available_at), derived_dependency_id=NULL, derived_content_id=NULL", (session, records_digest, adopted_at))
			else:
				cursor.execute(f"INSERT INTO `{names.imports}`(session_date, raw_id, available_at, derived_dependency_id, derived_content_id) VALUES (%s, %s, %s, NULL, NULL) ON DUPLICATE KEY UPDATE raw_id=VALUES(raw_id), available_at=VALUES(available_at)", (session, records_digest, adopted_at))
			cursor.execute("INSERT INTO stage11_adoption_receipts(session_date, manifest_id, source_key, source_provider, source_path, file_sha256, records_id, record_count, provenance_kind, adopted_at, original_receipt_at, cutoff, evidence_id, content_digest) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s) ON DUPLICATE KEY UPDATE records_id=VALUES(records_id), record_count=VALUES(record_count), adopted_at=VALUES(adopted_at), cutoff=VALUES(cutoff), evidence_id=VALUES(evidence_id), content_digest=VALUES(content_digest)", (session, context.manifest_id, context.manifest["snapshot_identity"]["source_key"], evidence.source.provider, str(path), item["file_sha256"], records_digest, len(bars), PROVENANCE_KIND, adopted_at, _iso(context.cutoff), evidence.evidence_id, content))
			cursor.execute("INSERT INTO stage11_completion(workload, session_date, series_key, content_digest, committed_at) VALUES (%s, %s, %s, %s, %s) ON DUPLICATE KEY UPDATE content_digest=VALUES(content_digest), committed_at=VALUES(committed_at)", ("raw", session, "raw-adoption-v1", content, adopted_at))
			connection.commit()
			return {"session": session, "changed_rows": changed, "removed_rows": removed, "record_count": len(bars), "content_digest": content, "committed": True}
		finally:
			cursor.close()
	except Exception:
		connection.rollback()
		raise
	finally:
		connection.close()


def _mysql_stage_deltas(target: MySQLTarget, manifest: dict[str, Any], sessions: tuple[str, ...]) -> None:
	names = _manifest_table_names(manifest)
	connection = target.connect()
	try:
		cursor = connection.cursor()
		try:
			for previous, current in zip(sessions, sessions[1:]):
				cursor.execute(f"INSERT INTO `{names.deltas}` (ticker_id, date_id, close_ratio_to_prior) SELECT current.ticker_id, current.date_id, current.close / previous.close FROM `{names.prices}` current JOIN `{names.prices}` previous ON previous.ticker_id = current.ticker_id WHERE current.date_id = (SELECT id FROM `{names.sessions}` WHERE transaction_date = %s) AND previous.date_id = (SELECT id FROM `{names.sessions}` WHERE transaction_date = %s) AND current.close IS NOT NULL AND previous.close IS NOT NULL AND previous.close <> 0 ON DUPLICATE KEY UPDATE close_ratio_to_prior = VALUES(close_ratio_to_prior)", (current, previous))
			connection.commit()
		finally:
			cursor.close()
	finally:
		connection.close()


def _run_java(command: list[str], timeout: float = 900.0) -> dict[str, Any]:
	try:
		result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
	except (OSError, subprocess.TimeoutExpired) as error:
		raise Stage11Error("Selected Java Stage 11 entry point could not run") from error
	try:
		payload = json.loads(result.stdout)
	except (TypeError, ValueError, json.JSONDecodeError) as error:
		raise Stage11Error("Selected Java Stage 11 entry point returned invalid JSON") from error
	if result.returncode != 0 or payload.get("failed"):
		raise Stage11Error("Selected Java Stage 11 entry point failed")
	return payload


def run_mysql(
	context: Stage11Context,
	dbinfo_file: Path,
	schema: str,
	sessions: tuple[str, ...],
	workloads: tuple[str, ...],
	output: Path,
	checkpoint_directory: Path,
) -> dict[str, Any]:
	if not sessions or len(sessions) > 3:
		raise Stage11Error("Stage 11a MySQL operation permits one to three target sessions")
	if any(workload not in WORKLOADS for workload in workloads) or not workloads:
		raise Stage11Error("Invalid Stage 11 workload selection")
	manifest = load_manifest(context.manifest_path)
	target = MySQLTarget(dbinfo_file, schema, manifest, extra_statements=_stage11_mysql_tables())
	probe = target.connect(select_schema=False)
	try:
		cursor = probe.cursor()
		try:
			cursor.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (schema,))
			if cursor.fetchone() is not None:
				raise Stage11Error("Stage 11a requires a fresh disposable MySQL schema")
		finally:
			cursor.close()
	finally:
		probe.close()
	schema_report = target.prepare()
	checkpoint_directory = checkpoint_directory.resolve()
	checkpoint_directory.mkdir(parents=True, exist_ok=True)
	archive_sessions = set(sessions)
	for session in sessions:
		target_entry = next((item for item in manifest.get("targets", []) if item.get("session") == session), None)
		if "derived" in workloads and isinstance(target_entry, dict):
			archive_sessions.update(target_entry.get("derived_dependencies", ()))
		if "zscores" in workloads and isinstance(target_entry, dict):
			archive_sessions.update(target_entry.get("zscore_dependencies", ()))
	raw_results = []
	for session in sorted(archive_sessions):
		file_item = next((value for value in manifest["files"] if value.get("session") == session and value.get("valid")), None)
		if not isinstance(file_item, dict):
			raise Stage11Error(f"Session is not an adoptable validated archive file: {session}")
		path = Path(file_item["path"]).resolve()
		bars, file_hash = read_exact_archive(path)
		evidence = ArchiveEvidenceStore(context.evidence_path).verify(ARCHIVE_SOURCE, session, ArchiveFileInventory(str(path), session, file_hash, True, len(bars), exact_records_id(bars), tuple(bar.ticker for bar in bars)))
		if evidence.validated_at > context.cutoff:
			raise Stage11Error(f"Archive evidence is outside the Stage 11 cutoff: {session}")
		if "raw" in workloads or session not in sessions or "derived" in workloads or "zscores" in workloads:
			raw_results.append(_mysql_adopt_raw(target, context, manifest, session, file_item, path, bars, evidence))
	if "derived" in workloads:
		command = ["java", "-Xmx512m", "-cp", _java_classpath(), "stockguidance.migration.JavaDerivedBenchmarkMain", "--manifest", str(context.manifest_path), "--dbinfo-file", str(dbinfo_file), "--disposable-schema", schema, "--preparers", "1", "--sessions", ",".join(sorted(sessions)), "--checkpoint-directory", str(checkpoint_directory)]
		derived_result = _run_java(command)
	else:
		derived_result = {"status": "not_requested"}
	if "zscores" in workloads:
		dependency_sessions = tuple(sorted(archive_sessions))
		_mysql_stage_deltas(target, manifest, dependency_sessions)
		command = ["java", "--add-modules=jdk.incubator.vector", "-Xmx512m", "-cp", _java_classpath(), "stockguidance.migration.JavaZScoreBenchmarkMain", "--manifest", str(context.manifest_path), "--dbinfo-file", str(dbinfo_file), "--disposable-schema", schema, "--sessions", ",".join(sorted(sessions)), "--checkpoint-directory", str(checkpoint_directory)]
		zscore_result = _run_java(command)
	else:
		zscore_result = {"status": "not_requested"}
	output = output.resolve()
	output.parent.mkdir(parents=True, exist_ok=True)
	report = {"schema_version": STAGE11_SCHEMA_VERSION, "stage": "11a", "status": "complete", "production_access": {"ddl": False, "dml": False, "database": "mysql_disposable"}, "prerequisites": {"decision_sha256": context.decision_sha256, "manifest_sha256": context.manifest_sha256, "audit_sha256": context.audit_sha256, "manifest_id": context.manifest_id, "cutoff": _iso(context.cutoff)}, "selected_backends": {"raw": "python", "derived": "java", "zscores": "java"}, "execution": {"storage": "mysql", "schema": schema, "schema_report": schema_report, "sessions": list(sessions), "workloads": list(workloads), "transaction_units": context.decision["rollout"]["transactions"]}, "operations": {"raw": raw_results, "derived": derived_result, "zscores": zscore_result}, "commands": _commands(context, schema, checkpoint_directory, sessions), "handoff": {"next": "Stage 11b", "production_ready": False, "checkpoint_after_commit": True, "original_receipt_time": "unknown_and_null", "chronological_rebuild": True}}
	output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
	return report
