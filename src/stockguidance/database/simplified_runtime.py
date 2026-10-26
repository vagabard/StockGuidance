"""Stage 8 runtime cutover for the versioned replacement database.

The target runtime has one write path: inspect a consistent target-schema
snapshot, prepare any source payloads named by the dependency planner, and
hand the immutable scope to the pending-operation coordinator.  This module
does not create schemas and never consults the retired operational metadata.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Callable, ContextManager, Protocol

from stockguidance.calendar.clock import Clock, decision_cutoff
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import DataConfiguration, SourceConfig
from stockguidance.database.dependency_planner import (
	DatabaseSnapshot,
	DependencyPlanner,
	InterpolationInterval,
	SnapshotSession,
	UpdateRequest,
	ZScoreSeriesSnapshot,
)
from stockguidance.database.numerical_rebuild import SimplifiedNumericalRebuildRunner
from stockguidance.database.operation_recovery import (
	DatabasePendingClearer,
	DatabaseReadinessGuard,
	PendingOperationCoordinator,
	ReadScope,
	ReadinessState,
	consistent_read_transaction,
)
from stockguidance.database.raw_rebuild import FilePreparedPayloadReader, RawPayloadPreparer, RawPayloadProvider, RawRebuildRunner
from stockguidance.database.repositories.simplified import (
	PendingUpdateRepository,
)
from stockguidance.database.simplified_contract import (
	DataState,
	OperationKind,
	PendingScope,
	UnitKind,
	WorkUnit,
)
from stockguidance.database.simplified_schema import SchemaVersionError, require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.market_data.intervals import ObservationInterval
from stockguidance.market_data.providers.contracts import ProviderResponse
from stockguidance.zscores.specification import ZScoreFamily
from stockguidance.zscores.storage import ZScoreSeriesDefinition


class SimplifiedRuntimeError(RuntimeError):
	"""A safe, actionable target-runtime failure."""


class ProviderWithCount(Protocol):
	def fetch(self, source: SourceConfig, session: TradingSession) -> object: ...


def _placeholders(values: Iterable[object]) -> str:
	values = tuple(values)
	return ",".join("%s" for _ in values)


def _db_date(value: object) -> date:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	if isinstance(value, str):
		return date.fromisoformat(value)
	if isinstance(value, bytes):
		return date.fromisoformat(value.decode("ascii"))
	raise SimplifiedRuntimeError("Target database returned an invalid transaction date")


def _db_text(value: object, name: str) -> str:
	if isinstance(value, bytes):
		return value.decode("utf-8")
	if isinstance(value, str):
		return value
	raise SimplifiedRuntimeError(f"Target database returned an invalid {name}")


def _db_bool(value: object, name: str) -> bool:
	if value in (0, False):
		return False
	if value in (1, True):
		return True
	raise SimplifiedRuntimeError(f"Target database returned an invalid {name} flag")


def _db_int(value: object, name: str) -> int:
	if isinstance(value, bool):
		raise SimplifiedRuntimeError(f"Target database returned an invalid {name}")
	if isinstance(value, int):
		result = value
	elif isinstance(value, bytes) and value.decode("ascii").isdigit():
		result = int(value.decode("ascii"))
	elif isinstance(value, str) and value.isdigit():
		result = int(value)
	else:
		raise SimplifiedRuntimeError(f"Target database returned an invalid {name}")
	if result <= 0:
		raise SimplifiedRuntimeError(f"Target database returned an invalid {name}")
	return result


def _utc(value: datetime) -> datetime:
	if value.tzinfo is None:
		return value.replace(tzinfo=timezone.utc)
	return value.astimezone(timezone.utc)


def _is_missing_schema(error: Exception) -> bool:
	message = str(error).casefold()
	return "no such table" in message or "doesn't exist" in message or "does not exist" in message


def target_schema_state(connect: WriteConnectionFactory, database_config: Path, table_names: TableNames | None = None) -> str:
	"""Return ``target`` or ``legacy`` without initializing either schema.

	A present but incompatible target metadata is an error, not permission to
	fall back to a writer using the old assumptions.
	"""
	names = table_names or load_table_names()
	connection = connect(database_config)
	cursor = connection.cursor()
	try:
		try:
			cursor.execute(f"SELECT id FROM {quote_identifier(names.table(TableRole.SCHEMA))} LIMIT 1", ())
			rows = cursor.fetchall()
		except Exception as error:
			if _is_missing_schema(error):
				return "legacy"
			raise
		if not rows:
			# A few compatibility adapters expose an empty result for a missing
			# table. Confirm table existence before classifying the database.
			try:
				cursor.execute(
					"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
					(names.table(TableRole.SCHEMA),),
				)
				existing = cursor.fetchall()
			except Exception:
				existing = ()
			existing_names = {str(row[0]) for row in existing if len(row) >= 1}
			if names.table(TableRole.SCHEMA) not in existing_names:
				return "legacy"
		if len(rows) != 1:
			raise SchemaVersionError("Target schema metadata must contain exactly one row")
		require_simplified_schema(cursor, names)
		return "target"
	finally:
		connection.rollback()
		cursor.close()
		connection.close()


def require_target_schema(connect: WriteConnectionFactory, database_config: Path, table_names: TableNames | None = None) -> TableNames:
	names = table_names or load_table_names()
	if target_schema_state(connect, database_config, names) != "target":
		raise SchemaVersionError("This maintenance path requires the versioned simplified schema; run the explicit target-schema setup first")
	return names


def _source_from_record(
	database_config: Path,
	data_root: Path,
	names: TableNames,
	connect: WriteConnectionFactory,
) -> SourceConfig:
	connection = connect(database_config)
	cursor = connection.cursor()
	try:
		require_simplified_schema(cursor, names)
		source_table = quote_identifier(names.table(TableRole.SOURCE))
		cursor.execute(
			f"SELECT source_family, endpoint, market, universe, interval_code, calendar_id, raw_price_split_adjusted, raw_price_dividend_adjusted, raw_price_vendor_defined_adjustment FROM {source_table} WHERE id = 1",
			(),
		)
		rows = cursor.fetchall()
		if len(rows) != 1 or len(rows[0]) != 9:
			raise SimplifiedRuntimeError("Target database must contain exactly one source configuration row")
		row = rows[0]
		family = _db_text(row[0], "source family")
		endpoint = _db_text(row[1], "source endpoint")
		market = _db_text(row[2], "source market")
		universe = _db_text(row[3], "source universe")
		interval = ObservationInterval(_db_text(row[4], "source interval"))
		calendar_id = _db_text(row[5], "source calendar")
		configuration = DataConfiguration(
			family, endpoint, market, universe, _db_bool(row[6], "raw_price_split_adjusted"),
			_db_bool(row[7], "raw_price_dividend_adjusted"), None if row[8] is None else _db_text(row[8], "vendor adjustment"), interval=interval, calendar_id=calendar_id,
		)
		return SourceConfig(configuration.to_source_identity(), data_root, database_config, "configured target database", _configuration=configuration)
	finally:
		connection.rollback()
		cursor.close()
		connection.close()


def source_from_database(database_path: Path, data_root: Path, connect: WriteConnectionFactory, table_names: TableNames | None = None) -> tuple[SourceConfig, TableNames]:
	names = require_target_schema(connect, database_path, table_names)
	return _source_from_record(database_path, data_root, names, connect), names


def ensure_sessions(source: SourceConfig, connect: WriteConnectionFactory, sessions: tuple[TradingSession, ...], table_names: TableNames) -> None:
	if not sessions:
		return
	connection = connect(source.database_config)
	cursor = connection.cursor()
	try:
		cursor.execute("START TRANSACTION", ())
		require_simplified_schema(cursor, table_names)
		table = quote_identifier(table_names.table(TableRole.SESSIONS))
		for session in sessions:
			cursor.execute(f"SELECT id FROM {table} WHERE transaction_date = %s", (session.session_date,))
			if not cursor.fetchall():
				cursor.execute(f"INSERT INTO {table} (transaction_date) VALUES (%s)", (session.session_date,))
		connection.commit()
	except Exception:
		connection.rollback()
		raise
	finally:
		cursor.close()
		connection.close()


def _session_context(calendar: SessionCalendar, requested: tuple[TradingSession, ...], max_window: int) -> tuple[TradingSession, ...]:
	first, last = requested[0], requested[-1]
	try:
		candidate = calendar.shift(first, -(max_window + 2))
		if candidate.sequence >= first.sequence:
			raise IndexError("calendar shift did not move backward")
		first = candidate
	except Exception:
		pass
	try:
		candidate = calendar.shift(last, max_window + 2)
		if candidate.sequence <= last.sequence:
			raise IndexError("calendar shift did not move forward")
		last = candidate
	except Exception:
		pass
	return calendar.sessions(first.session_date, last.session_date)


def _decode_json(value: object, name: str) -> object:
	if isinstance(value, bytes):
		value = value.decode("utf-8")
	if isinstance(value, str):
		try:
			return json.loads(value)
		except json.JSONDecodeError as error:
			raise SimplifiedRuntimeError(f"Target database returned invalid {name} JSON") from error
	return value


def read_database_snapshot(
	source: SourceConfig,
	connect: WriteConnectionFactory,
	calendar: SessionCalendar,
	context_sessions: tuple[TradingSession, ...],
	*,
	table_names: TableNames | None = None,
	windows: tuple[int, ...] | None = None,
) -> DatabaseSnapshot:
	names = table_names or load_table_names()
	if not context_sessions:
		raise SimplifiedRuntimeError("The target snapshot requires at least one exchange session")
	with consistent_read_transaction(source, connect) as transaction:
		cursor = transaction.cursor()
		require_simplified_schema(cursor, names)
		date_values = tuple(session.session_date for session in context_sessions)
		placeholder = _placeholders(date_values)
		sessions_table = quote_identifier(names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT id, transaction_date, raw_complete, delta_complete FROM {sessions_table} WHERE transaction_date IN ({placeholder}) ORDER BY transaction_date, id", date_values)
		session_rows = cursor.fetchall()
		by_date: dict[date, tuple[int, bool, bool]] = {}
		for row in session_rows:
			if len(row) != 4:
				raise SimplifiedRuntimeError("Target session completion query returned an invalid row")
			day = _db_date(row[1])
			if day in by_date:
				raise SimplifiedRuntimeError("Target database contains duplicate transaction dates")
			by_date[day] = (_db_int(row[0], "date_id"), _db_bool(row[2], "raw_complete"), _db_bool(row[3], "delta_complete"))
		if set(by_date) != set(date_values):
			raise SimplifiedRuntimeError("Target session inventory is incomplete; maintenance must register every requested session")

		date_ids = tuple(value[0] for value in by_date.values())
		id_placeholder = _placeholders(date_ids)
		prices = quote_identifier(names.table(TableRole.PRICES))
		deltas = quote_identifier(names.table(TableRole.DELTAS))
		repairs = quote_identifier(names.table(TableRole.REPAIR_METADATA))
		cursor.execute(f"SELECT date_id, ticker_id FROM {prices} WHERE date_id IN ({id_placeholder}) ORDER BY date_id, ticker_id", date_ids)
		raw_ids: dict[int, list[int]] = {date_id: [] for date_id in date_ids}
		for row in cursor.fetchall():
			raw_ids[_db_int(row[0], "raw date_id")].append(_db_int(row[1], "raw ticker_id"))
		cursor.execute(f"SELECT date_id, ticker_id FROM {deltas} WHERE date_id IN ({id_placeholder}) ORDER BY date_id, ticker_id", date_ids)
		delta_ids: dict[int, list[int]] = {date_id: [] for date_id in date_ids}
		for row in cursor.fetchall():
			delta_ids[_db_int(row[0], "delta date_id")].append(_db_int(row[1], "delta ticker_id"))
		cursor.execute(f"SELECT date_id, ticker_id, prior_date_id, following_date_id FROM {repairs} WHERE date_id IN ({id_placeholder}) ORDER BY date_id, ticker_id", date_ids)
		repair_ids: dict[int, list[int]] = {date_id: [] for date_id in date_ids}
		interval_rows: list[tuple[int, int, int, int]] = []
		for row in cursor.fetchall():
			date_id, ticker_id = _db_int(row[0], "repair date_id"), _db_int(row[1], "repair ticker_id")
			repair_ids[date_id].append(ticker_id)
			if row[2] is not None and row[3] is not None:
				interval_rows.append((date_id, ticker_id, _db_int(row[2], "prior date_id"), _db_int(row[3], "following date_id")))
		cursor.execute(f"SELECT id FROM {quote_identifier(names.table(TableRole.TICKERS))} ORDER BY id", ())
		ticker_ids = tuple(_db_int(row[0], "ticker_id") for row in cursor.fetchall())

		series_table = quote_identifier(names.table(TableRole.ZSCORE_SERIES))
		series_query = f"SELECT series_key, schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy FROM {series_table}"
		parameters: tuple[object, ...] = ()
		if windows:
			series_query += f" WHERE `window` IN ({_placeholders(windows)})"
			parameters = tuple(windows)
		cursor.execute(series_query, parameters)
		series_rows = cursor.fetchall()
		zscore_sessions_table = quote_identifier(names.table(TableRole.ZSCORE_SESSIONS))
		completed: dict[str, set[int]] = {}
		cursor.execute(f"SELECT series_key, date_id FROM {zscore_sessions_table}", ())
		for row in cursor.fetchall():
			completed.setdefault(_db_text(row[0], "z-score series key"), set()).add(_db_int(row[1], "z-score date_id"))
		series_snapshots: list[ZScoreSeriesSnapshot] = []
		for row in series_rows:
			definition = ZScoreSeriesDefinition(
				_db_text(row[2], "z-score source scope"), _db_text(row[3], "z-score calendar"), ZScoreFamily(_db_text(row[4], "z-score family")), _db_int(row[5], "z-score window"), _db_text(row[6], "z-score calculation version"), _db_text(row[8], "z-score precision"), _db_text(row[7], "z-score delta definition"), _db_text(row[9], "z-score missing-value policy"), _db_int(row[1], "z-score schema version"),
			)
			if _db_text(row[0], "z-score series key") != definition.series_key:
				raise SimplifiedRuntimeError("Target z-score series key does not match its persisted definition")
			series_snapshots.append(ZScoreSeriesSnapshot(definition, ticker_ids, frozenset(completed.get(definition.series_key, set()))))

		by_id = {value[0]: day for day, value in by_date.items()}
		interpolation: list[InterpolationInterval] = []
		for date_id, ticker_id, prior_id, following_id in interval_rows:
			if prior_id not in by_id or following_id not in by_id:
				continue
			interpolation.append(InterpolationInterval(ticker_id, prior_id, by_id[prior_id], following_id, by_id[following_id]))
		snapshot_sessions = tuple(
			SnapshotSession(
				session, by_date[session.session_date][0], by_date[session.session_date][1], by_date[session.session_date][2],
				tuple(sorted(raw_ids[by_date[session.session_date][0]])), tuple(sorted(delta_ids[by_date[session.session_date][0]])),
				tuple(sorted(repair_ids[by_date[session.session_date][0]])),
			)
			for session in context_sessions
		)
		return DatabaseSnapshot(
			snapshot_sessions,
			configuration_key=data_configuration_key(source.data_configuration),
			calendar_id=calendar.calendar_id,
			interpolation_intervals=tuple(interpolation),
			zscore_series=tuple(series_snapshots),
		)


class _PreparedScopePlanner:
	def __init__(
		self,
		base: DependencyPlanner,
		preparer: RawPayloadPreparer,
		calendar: SessionCalendar,
		source: SourceConfig,
		connect: WriteConnectionFactory,
		table_names: TableNames,
		forced_sessions: frozenset[date],
		available_at: datetime,
	) -> None:
		self.base, self.preparer, self.calendar = base, preparer, calendar
		self.source, self.connect, self.table_names = source, connect, table_names
		self.forced_sessions, self.available_at = forced_sessions, available_at

	def plan(self, request: UpdateRequest, snapshot: DatabaseSnapshot) -> PendingScope:
		initial_scope = self.base.plan(request, snapshot)
		by_date = {session.session_date: session for session in request.sessions}
		payloads = list(initial_scope.payloads)
		replacement_ids: dict[date, tuple[int, ...]] = {}
		reader = FilePreparedPayloadReader()
		for unit in initial_scope.rebuild:
			if unit.unit_kind != UnitKind.RAW_SESSION.value or unit.session_date is None:
				continue
			session = by_date.get(unit.session_date)
			if session is None:
				raise SimplifiedRuntimeError(f"No exchange session is available for raw work unit {unit.session_date}")
			payload = self.preparer.prepare(session, force_refresh=unit.session_date in self.forced_sessions, available_at=self.available_at)
			payloads.append(payload)
			validated = reader.read(payload, session)
			labels = tuple(sorted({bar.ticker for bar in validated.response.records} | {ticker for ticker, _ in validated.response.bad_records} | set(validated.response.ignored_placeholders)))
			if labels:
				replacement_ids[unit.session_date] = self._ensure_ticker_ids(labels, session.calendar_id)
		if replacement_ids:
			updated_sessions = tuple(
				replace(item, replacement_ticker_ids=replacement_ids.get(item.session.session_date, item.replacement_ticker_ids))
				for item in snapshot.sessions
			)
			snapshot = replace(snapshot, sessions=updated_sessions)
		scope = self.base.plan(request, snapshot)
		return replace(scope, payloads=tuple(payloads))

	def _ensure_ticker_ids(self, labels: tuple[str, ...], exchange: str) -> tuple[int, ...]:
		connection = self.connect(self.source.database_config)
		cursor = connection.cursor()
		try:
			cursor.execute("START TRANSACTION", ())
			require_simplified_schema(cursor, self.table_names)
			table = quote_identifier(self.table_names.table(TableRole.TICKERS))
			identifiers: list[int] = []
			for label in labels:
				cursor.execute(f"SELECT id, exchange FROM {table} WHERE label = %s", (label,))
				rows = cursor.fetchall()
				if len(rows) > 1:
					raise SimplifiedRuntimeError(f"Ticker identity is duplicated for {label}")
				if rows:
					identifier = _db_int(rows[0][0], "ticker_id")
					if rows[0][1] is not None and _db_text(rows[0][1], "ticker exchange") != exchange:
						raise SimplifiedRuntimeError(f"Ticker exchange identity changed for {label}")
					identifiers.append(identifier)
					continue
				cursor.execute(f"INSERT INTO {table} (label, exchange) VALUES (%s, %s)", (label, exchange))
				cursor.execute(f"SELECT id FROM {table} WHERE label = %s", (label,))
				rows = cursor.fetchall()
				if len(rows) != 1:
					raise SimplifiedRuntimeError(f"Ticker identity could not be persisted for {label}")
				identifiers.append(_db_int(rows[0][0], "ticker_id"))
			connection.commit()
			return tuple(identifiers)
		except Exception:
			connection.rollback()
			raise
		finally:
			cursor.close()
			connection.close()


@dataclass(frozen=True)
class SimplifiedUpdateResult:
	source: SourceConfig
	requested_sessions: tuple[TradingSession, ...]
	operation_id: int | None
	resumed_operation: bool
	downloaded_sessions: int
	completed: bool
	findings: tuple[str, ...] = ()
	data_revision: str = ""

	@property
	def successful(self) -> bool:
		return self.completed and not self.findings

	@property
	def exit_code(self) -> int:
		return 0 if self.successful else 1

	def payload(self) -> dict[str, object]:
		return {
			"successful": self.successful,
			"complete": self.completed,
			"schema": "stockguidance-simplified-runtime-1",
			"source_configuration": data_configuration_key(self.source.data_configuration),
			"start": self.requested_sessions[0].session_date.isoformat() if self.requested_sessions else None,
			"end": self.requested_sessions[-1].session_date.isoformat() if self.requested_sessions else None,
			"operation_id": self.operation_id,
			"resumed_operation": self.resumed_operation,
			"downloaded_sessions": self.downloaded_sessions,
			"data_revision": self.data_revision,
			"findings": list(self.findings),
		}


class _CountingProvider:
	def __init__(self, provider: RawPayloadProvider | None) -> None:
		self.provider = provider
		self.sessions: list[date] = []

	def fetch(self, source: SourceConfig, session: TradingSession) -> ProviderResponse:
		if self.provider is None:
			raise SimplifiedRuntimeError(f"No provider is configured for missing raw session {session.session_date}")
		self.sessions.append(session.session_date)
		return self.provider.fetch(source, session)


def run_simplified_update(
	source: SourceConfig,
	connect: WriteConnectionFactory,
	calendar: SessionCalendar,
	sessions: tuple[TradingSession, ...],
	*,
	provider: RawPayloadProvider | None,
	operation_kind: OperationKind = OperationKind.INITIAL_IMPORT,
	forced_sessions: frozenset[date] = frozenset(),
	windows: tuple[int, ...] | None = None,
	staging_root: Path | None = None,
	clock: Clock,
	table_names: TableNames | None = None,
	lock_factory: Callable[[], ContextManager[object]] | None = None,
) -> SimplifiedUpdateResult:
	if not sessions:
		raise SimplifiedRuntimeError("The requested range contains no published exchange sessions")
	names = require_target_schema(connect, source.database_config, table_names)
	if source.calendar_id != calendar.calendar_id:
		raise SimplifiedRuntimeError("The exchange calendar does not match the database-owned source configuration")
	if not forced_sessions.issubset({session.session_date for session in sessions}):
		raise SimplifiedRuntimeError("Forced replacement dates must be within the requested session scope")
	max_window = max(windows or (2,))
	context = _session_context(calendar, sessions, max_window)
	ensure_sessions(source, connect, context, names)
	provider_counter = _CountingProvider(provider)
	preparer = RawPayloadPreparer(source, staging_root or (source.data_root / ".pending"), provider_counter, clock=lambda: _utc(decision_cutoff(clock)))
	base_planner = DependencyPlanner()
	available_at = _utc(decision_cutoff(clock))
	planner = _PreparedScopePlanner(base_planner, preparer, calendar, source, connect, names, forced_sessions, available_at)
	raw_runner = RawRebuildRunner(source, calendar, table_names=names, clock=lambda: _utc(decision_cutoff(clock)))
	rebuild_runner = SimplifiedNumericalRebuildRunner(source, calendar, raw_runner, table_names=names, clock=lambda: _utc(decision_cutoff(clock)))
	pending = PendingUpdateRepository(source, connect, names)
	active_before = pending.load_active()
	resumed = active_before is not None
	old_payloads = () if active_before is None else active_before.scope.payloads
	request = UpdateRequest(source, sessions, available_at, operation_kind, forced_sessions)
	context_provider = lambda: read_database_snapshot(source, connect, calendar, context, table_names=names, windows=windows)
	coordinator = PendingOperationCoordinator(
		source, connect, planner, DatabasePendingClearer(names), pending=pending, rebuild_runner=rebuild_runner, table_names=names,
		clock=lambda: _utc(decision_cutoff(clock)), lock_factory=lock_factory,
	)
	try:
		operation = coordinator.accept(request, context_provider)
	except Exception as error:
		if isinstance(error, (SchemaVersionError, SimplifiedRuntimeError)):
			raise
		raise SimplifiedRuntimeError("The target database operation could not be completed; the pending scope remains recoverable") from error
	for payload in (*old_payloads, *operation.scope.payloads):
		try:
			payload.temporary_path.unlink(missing_ok=True)
		except OSError:
			pass
	return SimplifiedUpdateResult(source, sessions, operation.operation_id, resumed, len(provider_counter.sessions), True)


@dataclass(frozen=True)
class SimplifiedFinding:
	date: date | None
	ticker_id: int | None
	phase: str
	reason: str
	blocking: bool

	def payload(self) -> dict[str, object]:
		return {
			"date": None if self.date is None else self.date.isoformat(),
			"ticker_id": self.ticker_id,
			"phase": self.phase,
			"reason": self.reason,
			"blocking": self.blocking,
		}


@dataclass(frozen=True)
class SimplifiedValidationResult:
	requested_sessions: tuple[TradingSession, ...]
	findings: tuple[SimplifiedFinding, ...]
	state: str = "verified"

	@property
	def complete(self) -> bool:
		return self.state == "verified" and not any(item.blocking for item in self.findings)

	@property
	def failed(self) -> bool:
		return self.state == "failed"

	@property
	def exit_code(self) -> int:
		return 0 if self.complete else 1 if self.failed else 2

	def payload(self) -> dict[str, object]:
		return {
			"schema": "stockguidance-simplified-validation-1",
			"complete": self.complete,
			"failed": self.failed,
			"exit_code": self.exit_code,
			"state": self.state,
			"sessions": [item.session_date.isoformat() for item in self.requested_sessions],
			"findings": [item.payload() for item in self.findings],
		}


def validate_simplified_database(
	source: SourceConfig,
	connect: WriteConnectionFactory,
	calendar: SessionCalendar,
	sessions: tuple[TradingSession, ...],
	*,
	table_names: TableNames | None = None,
) -> SimplifiedValidationResult:
	if not sessions:
		return SimplifiedValidationResult((), (SimplifiedFinding(None, None, "coverage", "Requested range contains no exchange sessions", True),), "failed")
	names = require_target_schema(connect, source.database_config, table_names)
	findings: list[SimplifiedFinding] = []
	with consistent_read_transaction(source, connect) as transaction:
		cursor = transaction.cursor()
		require_simplified_schema(cursor, names)
		ids: list[int] = []
		dates = tuple(item.session_date for item in sessions)
		table = quote_identifier(names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT id, transaction_date, raw_complete, delta_complete FROM {table} WHERE transaction_date IN ({_placeholders(dates)}) ORDER BY transaction_date", dates)
		rows = cursor.fetchall()
		by_date: dict[date, tuple[int, bool, bool]] = {}
		for row in rows:
			day = _db_date(row[1])
			by_date[day] = (_db_int(row[0], "date_id"), _db_bool(row[2], "raw_complete"), _db_bool(row[3], "delta_complete"))
		if len(by_date) != len(dates):
			findings.append(SimplifiedFinding(None, None, "coverage", "Requested session is absent from the target date dictionary", True))
		for day, value in by_date.items():
			if not value[1]:
				findings.append(SimplifiedFinding(day, None, "raw", "raw_completion_missing", True))
		ids.extend(value[0] for value in by_date.values())
		if ids:
			guard = DatabaseReadinessGuard(names).check(ReadScope(date_ids=tuple(ids)), transaction)
			if guard.status is ReadinessState.UNFINISHED_OPERATION:
				findings.append(SimplifiedFinding(None, guard.ticker_id, guard.phase or "operation", guard.reason or "An unfinished operation overlaps the requested read", True))
				return SimplifiedValidationResult(sessions, tuple(findings), "unfinished_operation")
			if ids:
				placeholders = _placeholders(ids)
				prices = quote_identifier(names.table(TableRole.PRICES))
				cursor.execute(f"SELECT date_id, ticker_id, state_id FROM {prices} WHERE date_id IN ({placeholders}) ORDER BY date_id, ticker_id", tuple(ids))
				for row in cursor.fetchall():
					raw_day: date | None = next((key for key, value in by_date.items() if value[0] == _db_int(row[0], "raw date_id")), None)
					state = {1: DataState.GOOD, 2: DataState.BAD, 3: DataState.MISSING, 4: DataState.INTERPOLATED}.get(_db_int(row[2], "state_id"))
					if state is DataState.BAD:
						findings.append(SimplifiedFinding(raw_day, _db_int(row[1], "raw ticker_id"), "raw", "bad_observation", True))
					elif state is DataState.MISSING:
						findings.append(SimplifiedFinding(raw_day, _db_int(row[1], "raw ticker_id"), "raw", "expected_missing_observation", False))
				deltas = quote_identifier(names.table(TableRole.DELTAS))
				cursor.execute(f"SELECT date_id, ticker_id, calculation_status FROM {deltas} WHERE date_id IN ({placeholders}) ORDER BY date_id, ticker_id", tuple(ids))
				for row in cursor.fetchall():
					status = _db_text(row[2], "delta calculation status")
					if status in {"missing_input", "invalid_input"}:
						delta_day: date | None = next((key for key, value in by_date.items() if value[0] == _db_int(row[0], "delta date_id")), None)
						findings.append(SimplifiedFinding(delta_day, _db_int(row[1], "delta ticker_id"), "delta", status, True))
				completion_missing = [day for day, value in by_date.items() if not value[2]]
				for day in completion_missing:
					findings.append(SimplifiedFinding(day, None, "delta", "delta_completion_missing", True))
				zscore = quote_identifier(names.table(TableRole.ZSCORE_SESSIONS))
				cursor.execute(f"SELECT date_id, status_counts FROM {zscore} WHERE date_id IN ({placeholders})", tuple(ids))
				for row in cursor.fetchall():
					counts = _decode_json(row[1], "z-score status_counts")
					if not isinstance(counts, dict):
						findings.append(SimplifiedFinding(None, None, "zscore", "status_counts_not_an_object", True))
						continue
					for status, count in counts.items():
						if str(status) != "0" and int(count) > 0:
							zscore_day: date | None = next((key for key, value in by_date.items() if value[0] == _db_int(row[0], "z-score date_id")), None)
							findings.append(SimplifiedFinding(zscore_day, None, "zscore", f"expected_non_ready_status_{status}", False))
		return SimplifiedValidationResult(sessions, tuple(findings), "verified")


def published_sessions(calendar: SessionCalendar, start: date, end: date, cutoff: datetime, publication_delay: timedelta) -> tuple[TradingSession, ...]:
	if start > end:
		raise SimplifiedRuntimeError("Maintenance start date must not follow end date")
	publication = _utc(cutoff)
	result = tuple(
		session for session in calendar.sessions(start, end)
		if session.closes_at <= publication and session.closes_at + publication_delay <= publication
	)
	if not result:
		raise SimplifiedRuntimeError("The requested range contains no published exchange sessions")
	return result


__all__ = [
	"SimplifiedFinding", "SimplifiedRuntimeError", "SimplifiedUpdateResult", "SimplifiedValidationResult",
	"ensure_sessions", "published_sessions", "read_database_snapshot", "require_target_schema",
	"run_simplified_update", "source_from_database", "target_schema_state", "validate_simplified_database",
]
