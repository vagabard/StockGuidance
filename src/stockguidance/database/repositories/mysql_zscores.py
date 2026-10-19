from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import math
from types import SimpleNamespace
from typing import Iterator, cast

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.repositories.zscores import LoadedZScoreInputs, ZScoreInputQuery, ZScoreRepository
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.update_schema import assert_source, assert_transactional_tables, assert_zscore_tables
from stockguidance.database.write_connections import WriteConnectionFactory, WriteCursor
from stockguidance.market_data.identity import content_id, source_key
from stockguidance.market_data.manifests import InvalidManifest, ManifestStore, SourceManifest
from stockguidance.market_data.providers.contracts import ResponseState
from stockguidance.market_data.records import records_id
from stockguidance.zscores.inputs import InputState, ZScoreInput, ZScoreObservation
from stockguidance.zscores.results import ZScoreResult, ZScoreRow, ZScoreStatus
from stockguidance.zscores.storage import (
	ZScoreDependencySnapshot,
	ZScoreSeriesDefinition,
	ZScoreSessionMetadata,
	VerifiedZScoreRead,
	base_series_key,
	bind_dependency,
	metadata_from_result,
	rows_content_id,
	status_counts,
)


class ZScoreDependencyChanged(ValueError):
	"""Raised when database or manifest evidence changed during publication/read."""


class ZScoreMetadataConflict(ValueError):
	"""Raised when an existing series definition is not compatible."""


class ZScoreSchemaError(ValueError):
	"""Raised when optional z-score tables are absent or incompatible."""


def _placeholders(values: Sequence[object]) -> str:
	if not values:
		raise ValueError("A bounded z-score query requires values")
	return ",".join("%s" for _ in values)


def _as_float(value: object) -> float | None:
	if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
		return None
	try:
		converted = float(value)
	except (TypeError, ValueError, OverflowError):
		return None
	return converted if math.isfinite(converted) else None


def _digest_value(value: object) -> object:
	if isinstance(value, datetime):
		return value.isoformat()
	if isinstance(value, (str, int, float, bool)) or value is None:
		return value
	return str(value)


def _status_observation(state: InputState, value: object | None = None) -> ZScoreObservation:
	if state is InputState.MISSING:
		return ZScoreObservation.missing()
	if state is InputState.UNVERIFIED:
		return ZScoreObservation.unverified(value)
	if state is InputState.NOT_YET_AVAILABLE:
		return ZScoreObservation.not_yet_available(value)
	if state is InputState.INVALID:
		return ZScoreObservation.invalid(value)
	if state is InputState.INSUFFICIENT_HISTORY:
		return ZScoreObservation.insufficient_history()
	return ZScoreObservation.ready(value)


class _CloseValue:
	def __init__(self, state: InputState, value: float | None) -> None:
		self.state, self.value = state, value


class _SessionEvidence:
	def __init__(self, session_date: date) -> None:
		self.session_date = session_date
		self.date_id: int | None = None
		self.raw: dict[int, object] = {}
		self.raw_duplicates: set[int] = set()
		self.deltas: dict[int, object] = {}
		self.delta_duplicates: set[int] = set()
		self.receipt: tuple[str, datetime] | None = None
		self.receipt_invalid = False
		self.manifest: SourceManifest | None = None
		self.manifest_invalid = False


class MySQLZScoreRepository(ZScoreRepository):
	"""Bounded z-score storage using the existing updater tables as evidence."""

	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None, manifest_store: ManifestStore | None = None) -> None:
		self.source, self.connect = source, connect
		self.table_names = table_names or load_table_names()
		self.manifests = manifest_store or ManifestStore(source.data_root)

	@contextmanager
	def _transaction(self, *, write: bool = False) -> Iterator[WriteCursor]:
		connection = self.connect(self.source.database_config)
		try:
			cursor = connection.cursor()
			try:
				cursor.execute("START TRANSACTION", ())
				assert_transactional_tables(cursor, self.table_names)
				assert_source(cursor, self.source, self.table_names)
				yield cursor
				if write:
					connection.commit()
				else:
					connection.rollback()
			except Exception:
				connection.rollback()
				raise
			finally:
				cursor.close()
		finally:
			connection.close()

	def load_inputs(self, query: ZScoreInputQuery) -> LoadedZScoreInputs:
		if query.scope.boundary.source != self.source.identity:
			raise ValueError("Z-score input and database source identities differ")
		with self._transaction() as cursor:
			return self._load_inputs(cursor, query, lock=False)

	def list_ticker_ids(self) -> tuple[int, ...]:
		with self._transaction() as cursor:
			cursor.execute(f"SELECT id FROM {quote_identifier(self.table_names.table(TableRole.TICKERS))} ORDER BY id", ())
			rows = cursor.fetchall()
		if any(len(row) != 1 for row in rows):
			raise ValueError("Invalid z-score ticker mapping row")
		values = tuple(normalize_date_id(row[0]) for row in rows)
		if len(values) != len(set(values)) or not values:
			raise ValueError("Invalid or empty z-score ticker mapping")
		return values

	def _load_inputs(self, cursor: Cursor, query: ZScoreInputQuery, *, lock: bool) -> LoadedZScoreInputs:
		evidence, labels = self._read_evidence(cursor, query, lock=lock)
		close_values = self._close_values(query, evidence, labels)
		observations: dict[int, tuple[ZScoreObservation, ...]] = {}
		for ticker_id in query.ticker_ids:
			values: list[ZScoreObservation] = []
			for index, session in enumerate(query.scope.sessions):
				current = close_values[session]
				if query.family.value == "price":
					values.append(_status_observation(current[ticker_id].state, current[ticker_id].value))
					continue
				if index == 0:
					values.append(_status_observation(InputState.INSUFFICIENT_HISTORY))
					continue
				prior = close_values[query.scope.sessions[index - 1]][ticker_id]
				stored = evidence[session.session_date].deltas.get(ticker_id)
				if current[ticker_id].state is not InputState.AVAILABLE or prior.state is not InputState.AVAILABLE:
					state = self._combine_close_states(current[ticker_id].state, prior.state)
					values.append(_status_observation(state, stored))
					continue
				if ticker_id in evidence[session.session_date].delta_duplicates:
					values.append(_status_observation(InputState.INVALID, stored))
					continue
				stored_ratio = _as_float(stored)
				current_value, prior_value = current[ticker_id].value, prior.value
				if stored is None:
					values.append(_status_observation(InputState.MISSING))
				elif stored_ratio is None or stored_ratio <= 0.0 or current_value is None or prior_value is None or not math.isclose(stored_ratio, current_value / prior_value, rel_tol=1e-10, abs_tol=1e-12):
					values.append(_status_observation(InputState.INVALID, stored))
				else:
					values.append(_status_observation(InputState.AVAILABLE, stored_ratio - 1.0))
			observations[ticker_id] = tuple(values)
		input_data = ZScoreInput.from_values(
			query.family,
			query.scope.sessions,
			observations,
			dependency_sessions=query.scope.sessions,
			scored_sessions=query.scored_sessions,
		)
		base_key = base_series_key(source_key(self.source.identity), query.scope.boundary.calendar_id, query.family)
		dependencies = self._dependency_snapshots(query, evidence, labels, close_values, base_key)
		return LoadedZScoreInputs(query, input_data, dependencies)

	@staticmethod
	def _combine_close_states(first: InputState, second: InputState) -> InputState:
		priority = {InputState.INVALID: 4, InputState.UNVERIFIED: 3, InputState.NOT_YET_AVAILABLE: 3, InputState.MISSING: 2, InputState.INSUFFICIENT_HISTORY: 1}
		if first is InputState.AVAILABLE and second is InputState.AVAILABLE:
			return InputState.AVAILABLE
		return max((first, second), key=lambda state: priority.get(state, 0))

	def _read_evidence(self, cursor: Cursor, query: ZScoreInputQuery, *, lock: bool) -> tuple[dict[date, _SessionEvidence], dict[int, str]]:
		sessions = query.scope.sessions
		session_by_date = {session.session_date: session for session in sessions}
		dates = tuple(session.session_date for session in sessions)
		ticker_ids = query.ticker_ids
		lock_clause = " FOR UPDATE" if lock else ""
		date_parameters = dates
		date_sql = (
			f"SELECT id, transaction_date FROM {quote_identifier(self.table_names.table(TableRole.SESSIONS))} "
			f"WHERE transaction_date IN ({_placeholders(date_parameters)}) ORDER BY transaction_date, id{lock_clause}"
		)
		cursor.execute(date_sql, date_parameters)
		evidence = {session_date: _SessionEvidence(session_date) for session_date in dates}
		for row in cursor.fetchall():
			if len(row) != 2:
				raise ValueError("Invalid z-score date mapping")
			session_date = normalize_session_date(row[1])
			if session_date not in evidence or evidence[session_date].date_id is not None:
				raise ValueError("Duplicate or out-of-scope z-score date mapping")
			evidence[session_date].date_id = normalize_date_id(row[0])

		mapping_sql = f"SELECT id, label FROM {quote_identifier(self.table_names.table(TableRole.TICKERS))} WHERE id IN ({_placeholders(ticker_ids)}) ORDER BY id{lock_clause}"
		cursor.execute(mapping_sql, ticker_ids)
		labels: dict[int, str] = {}
		for row in cursor.fetchall():
			if len(row) != 2 or not isinstance(row[1], str):
				raise ValueError("Invalid z-score ticker mapping")
			ticker_id = normalize_date_id(row[0])
			if ticker_id in labels:
				raise ValueError("Duplicate z-score ticker mapping")
			labels[ticker_id] = row[1]

		parameters = dates + ticker_ids
		raw_sql = (
			f"SELECT r.date_id, r.ticker_id, r.close FROM {quote_identifier(self.table_names.table(TableRole.PRICES))} r "
			f"WHERE r.date_id IN (SELECT id FROM {quote_identifier(self.table_names.table(TableRole.SESSIONS))} WHERE transaction_date IN ({_placeholders(dates)})) "
			f"AND r.ticker_id IN ({_placeholders(ticker_ids)}) ORDER BY r.date_id, r.ticker_id{lock_clause}"
		)
		cursor.execute(raw_sql, parameters)
		by_date_id = {item.date_id: item for item in evidence.values() if item.date_id is not None}
		for row in cursor.fetchall():
			if len(row) != 3 or normalize_date_id(row[0]) not in by_date_id:
				raise ValueError("Invalid z-score raw close row")
			item = by_date_id[normalize_date_id(row[0])]
			ticker_id = normalize_date_id(row[1])
			if ticker_id in item.raw:
				item.raw_duplicates.add(ticker_id)
			else:
				item.raw[ticker_id] = row[2]

		if query.family.value == "price_delta":
			delta_sql = (
				f"SELECT r.date_id, r.ticker_id, r.close_ratio_to_prior FROM {quote_identifier(self.table_names.table(TableRole.DELTAS))} r "
				f"WHERE r.date_id IN (SELECT id FROM {quote_identifier(self.table_names.table(TableRole.SESSIONS))} WHERE transaction_date IN ({_placeholders(dates)})) "
				f"AND r.ticker_id IN ({_placeholders(ticker_ids)}) ORDER BY r.date_id, r.ticker_id{lock_clause}"
			)
			cursor.execute(delta_sql, parameters)
			for row in cursor.fetchall():
				if len(row) != 3 or normalize_date_id(row[0]) not in by_date_id:
					raise ValueError("Invalid z-score delta row")
				item = by_date_id[normalize_date_id(row[0])]
				ticker_id = normalize_date_id(row[1])
				if ticker_id in item.deltas:
					item.delta_duplicates.add(ticker_id)
				else:
					item.deltas[ticker_id] = row[2]

		receipt_sql = f"SELECT session_date, raw_id, available_at FROM {quote_identifier(self.table_names.table(TableRole.IMPORTS))} WHERE session_date IN ({_placeholders(dates)}) ORDER BY session_date{lock_clause}"
		cursor.execute(receipt_sql, dates)
		for row in cursor.fetchall():
			if len(row) != 3:
				raise ValueError("Invalid z-score import receipt")
			session_date = normalize_session_date(row[0])
			if session_date not in evidence or evidence[session_date].receipt is not None:
				raise ValueError("Duplicate or out-of-scope z-score import receipt")
			try:
				parsed = datetime.fromisoformat(str(row[2]))
				if not isinstance(row[1], str) or parsed.tzinfo is None or parsed.utcoffset() is None or parsed < session_by_date[session_date].closes_at:
					raise ValueError("Invalid z-score receipt")
				evidence[session_date].receipt = (row[1], parsed)
			except (TypeError, ValueError, OverflowError):
				evidence[session_date].receipt_invalid = True

		for item in evidence.values():
			try:
				item.manifest = self.manifests.load(self.source.identity, next(session for session in sessions if session.session_date == item.session_date))
			except (InvalidManifest, OSError, ValueError):
				item.manifest_invalid = True
		return evidence, labels

	@staticmethod
	def _close_values(query: ZScoreInputQuery, evidence: Mapping[date, _SessionEvidence], labels: Mapping[int, str]) -> dict[TradingSession, dict[int, _CloseValue]]:
		result: dict[TradingSession, dict[int, _CloseValue]] = {}
		for session in query.scope.sessions:
			item = evidence[session.session_date]
			manifest = item.manifest
			manifest_records = {} if manifest is None else {bar.ticker: bar for bar in manifest.download.response.records}
			verified = manifest is not None and not item.manifest_invalid and manifest.download.response.state in {ResponseState.COMPLETE, ResponseState.NO_TRADING} and item.receipt is not None and not item.receipt_invalid and item.receipt[0] == records_id(manifest.download.response.records)
			available = verified and item.receipt is not None and item.receipt[1] <= query.scope.as_of and manifest is not None and manifest.download.available_at <= query.scope.as_of
			values: dict[int, _CloseValue] = {}
			for ticker_id in query.ticker_ids:
				label = labels.get(ticker_id)
				if not verified:
					state = InputState.NOT_YET_AVAILABLE if item.receipt is not None and item.receipt[1] > query.scope.as_of else InputState.UNVERIFIED
					values[ticker_id] = _CloseValue(state, None)
					continue
				if not available:
					not_yet_available = verified and ((item.receipt is not None and item.receipt[1] > query.scope.as_of) or (manifest is not None and manifest.download.available_at > query.scope.as_of))
					values[ticker_id] = _CloseValue(InputState.NOT_YET_AVAILABLE if not_yet_available else InputState.UNVERIFIED, None)
					continue
				if label is None or label not in manifest_records:
					values[ticker_id] = _CloseValue(InputState.MISSING, None)
					continue
				raw_value = item.raw.get(ticker_id)
				raw_close = _as_float(raw_value)
				manifest_close = _as_float(manifest_records[label].close)
				if ticker_id in item.raw_duplicates or raw_close is None or raw_close <= 0.0 or manifest_close is None or manifest_close <= 0.0 or not math.isclose(raw_close, manifest_close, rel_tol=1e-10, abs_tol=1e-12):
					values[ticker_id] = _CloseValue(InputState.INVALID, raw_close)
				else:
					values[ticker_id] = _CloseValue(InputState.AVAILABLE, raw_close)
			result[session] = values
		return result

	def _dependency_snapshots(self, query: ZScoreInputQuery, evidence: Mapping[date, _SessionEvidence], labels: Mapping[int, str], close_values: Mapping[TradingSession, Mapping[int, _CloseValue]], base_key: str) -> tuple[ZScoreDependencySnapshot, ...]:
		payload = []
		availability = []
		for session in query.scope.sessions:
			item = evidence[session.session_date]
			close = close_values[session]
			payload.append({
				"session": session.session_date.isoformat(), "sequence": session.sequence, "date_id": item.date_id,
				"ticker_mapping": sorted(labels.items()), "manifest": None if item.manifest is None else item.manifest.content_id,
				"receipt": None if item.receipt is None else (item.receipt[0], item.receipt[1].isoformat()),
				"raw": sorted((ticker_id, _digest_value(item.raw.get(ticker_id))) for ticker_id in query.ticker_ids),
				"delta": sorted((ticker_id, _digest_value(item.deltas.get(ticker_id))) for ticker_id in query.ticker_ids) if query.family.value == "price_delta" else None,
			})
			availability.append({"session": session.session_date.isoformat(), "states": sorted((ticker_id, close[ticker_id].state.value) for ticker_id in query.ticker_ids)})
		dependency = content_id(payload)
		availability_digest = content_id(availability)
		dependencies: list[ZScoreDependencySnapshot] = []
		for session in query.scope.sessions:
			date_id = evidence[session.session_date].date_id
			if date_id is None:
				raise ZScoreDependencyChanged("Z-score session mapping is missing")
			dependencies.append(
			ZScoreDependencySnapshot(
				base_key,
				session,
				date_id,
				query.ticker_ids,
				dependency,
				availability_digest,
				all(close_values[session][ticker_id].state is InputState.AVAILABLE for ticker_id in query.ticker_ids),
				query.scope.as_of,
				query.scope.sessions,
			)
			)
		return tuple(dependencies)

	def publish(self, definition: ZScoreSeriesDefinition, result: ZScoreResult, dependencies: tuple[ZScoreDependencySnapshot, ...], computed_at: datetime) -> None:
		if not isinstance(computed_at, datetime):
			raise ValueError("Z-score publication requires a computation timestamp")
		if result.specification.family is not definition.family or definition.window not in result.specification.windows:
			raise ValueError("Z-score result does not match its series definition")
		if not dependencies:
			raise ValueError("Z-score publication requires dependency snapshots")
		self.table_names.resolve_zscore_tables((definition.window,))
		bound = tuple(bind_dependency(dependency, definition) for dependency in dependencies)
		by_session = {dependency.session: dependency for dependency in bound}
		if len(by_session) != len(bound):
			raise ValueError("Duplicate z-score publication dependencies")
		with self._transaction(write=True) as cursor:
			assert_zscore_tables(cursor, self.table_names, (definition.window,))
			self._ensure_series(cursor, definition, computed_at)
			current_by_group: dict[tuple[tuple[TradingSession, ...], tuple[int, ...], datetime], dict[TradingSession, ZScoreDependencySnapshot]] = {}
			for dependency in bound:
				group_key = (dependency.input_sessions or (dependency.session,), dependency.ticker_ids, dependency.as_of)
				if group_key not in current_by_group:
					current_by_group[group_key] = {item.session: item for item in self._load_dependencies(cursor, definition, dependency, lock=True)}
				current = current_by_group[group_key].get(dependency.session)
				if current is None:
					raise ZScoreDependencyChanged("Z-score dependency session disappeared during publication")
				if current != dependency:
					raise ZScoreDependencyChanged("Z-score inputs changed during publication")
				rows = tuple(row for row in result.rows if row.window == definition.window and row.session == dependency.session)
				if any(row.ticker_id not in dependency.ticker_ids for row in rows):
					raise ValueError("Z-score publication contains an out-of-scope ticker")
				metadata = metadata_from_result(definition, result, dependency, computed_at)
				self._replace_session(cursor, definition, dependency, rows, metadata)

	def _ensure_series(self, cursor: Cursor, definition: ZScoreSeriesDefinition, computed_at: datetime) -> None:
		table = quote_identifier(self.table_names.table(TableRole.ZSCORE_SERIES))
		cursor.execute(f"SELECT schema_version, source_scope, calendar_id, family, window, calculation_version, delta_definition, precision, missing_value_policy FROM {table} WHERE series_key = %s", (definition.series_key,))
		rows = cursor.fetchall()
		if len(rows) > 1:
			raise ZScoreMetadataConflict("Duplicate z-score series metadata")
		if rows:
			row = rows[0]
			if len(row) != 9 or tuple(row) != (definition.schema_version, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy):
				raise ZScoreMetadataConflict("Conflicting z-score series definition")
			return
		cursor.execute(
			f"INSERT INTO {table} (series_key, schema_version, source_scope, calendar_id, family, window, calculation_version, delta_definition, precision, missing_value_policy, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
			(definition.series_key, definition.schema_version, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy, computed_at.isoformat()),
		)

	def _replace_session(self, cursor: WriteCursor, definition: ZScoreSeriesDefinition, dependency: ZScoreDependencySnapshot, rows: tuple[ZScoreRow, ...], metadata: ZScoreSessionMetadata) -> None:
		table = quote_identifier(self.table_names.resolve_zscore_tables((definition.window,))[definition.window].price if definition.family.value == "price" else self.table_names.resolve_zscore_tables((definition.window,))[definition.window].price_delta)
		cursor.execute(f"DELETE FROM {table} WHERE date_id = %s", (dependency.date_id,))
		parameters = tuple((dependency.date_id, row.ticker_id, row.score, int(row.status)) for row in rows)
		for offset in range(0, len(parameters), 1000):
			cursor.executemany(f"INSERT INTO {table} (date_id, ticker_id, zscore, status) VALUES (%s,%s,%s,%s)", parameters[offset:offset + 1000])
		metadata_table = quote_identifier(self.table_names.table(TableRole.ZSCORE_SESSIONS))
		cursor.execute(f"DELETE FROM {metadata_table} WHERE series_key = %s AND date_id = %s", (metadata.series_key, metadata.date_id))
		serialized_counts = ",".join(f"{int(status)}:{count}" for status, count in metadata.status_counts)
		cursor.execute(
			f"INSERT INTO {metadata_table} (series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
			(metadata.series_key, metadata.date_id, metadata.dependency_digest, metadata.availability_digest, int(metadata.actual_input_available), metadata.completed_at.isoformat(), metadata.row_count, metadata.ready_count, serialized_counts, metadata.content_digest),
		)

	def _load_inputs_for_dependency(self, cursor: Cursor, definition: ZScoreSeriesDefinition, dependency: ZScoreDependencySnapshot, *, lock: bool) -> ZScoreDependencySnapshot:
		current = self._load_dependencies(cursor, definition, dependency, lock=lock)
		for candidate in current:
			if candidate.session == dependency.session:
				return candidate
		raise ZScoreDependencyChanged("Z-score dependency session disappeared during revalidation")

	def _load_dependencies(self, cursor: Cursor, definition: ZScoreSeriesDefinition, dependency: ZScoreDependencySnapshot, *, lock: bool) -> tuple[ZScoreDependencySnapshot, ...]:
		input_sessions = dependency.input_sessions or (dependency.session,)
		scope = SimpleNamespace(
		sessions=input_sessions,
		as_of=dependency.as_of,
		boundary=SimpleNamespace(calendar_id=definition.calendar_id),
	)
		query = cast(ZScoreInputQuery, SimpleNamespace(scope=scope, family=definition.family, ticker_ids=dependency.ticker_ids, scored_sessions=input_sessions))
		evidence, labels = self._read_evidence(cursor, query, lock=lock)
		close_values = self._close_values(query, evidence, labels)
		current = self._dependency_snapshots(query, evidence, labels, close_values, base_series_key(definition.source_scope, definition.calendar_id, definition.family))
		return tuple(bind_dependency(candidate, definition) for candidate in current)

	def read_verified(self, definition: ZScoreSeriesDefinition, dependency: ZScoreDependencySnapshot) -> VerifiedZScoreRead:
		self.table_names.resolve_zscore_tables((definition.window,))
		bound = bind_dependency(dependency, definition)
		with self._transaction() as cursor:
			assert_zscore_tables(cursor, self.table_names, (definition.window,))
			metadata = self._read_metadata(cursor, definition, bound)
			rows = self._read_rows(cursor, definition, bound)
			current = self._load_inputs_for_dependency(cursor, definition, bound, lock=False)
			if current != bound:
				raise ZScoreDependencyChanged("Stored z-score result is stale")
			if metadata.dependency_digest != bound.dependency_digest or metadata.availability_digest != bound.availability_digest or metadata.content_digest != rows_content_id(rows):
				raise ZScoreDependencyChanged("Stored z-score metadata is stale")
			return VerifiedZScoreRead(definition, metadata, rows)

	def _read_metadata(self, cursor: Cursor, definition: ZScoreSeriesDefinition, dependency: ZScoreDependencySnapshot) -> ZScoreSessionMetadata:
		table = quote_identifier(self.table_names.table(TableRole.ZSCORE_SESSIONS))
		cursor.execute(f"SELECT series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest FROM {table} WHERE series_key = %s AND date_id = %s", (definition.series_key, dependency.date_id))
		rows = cursor.fetchall()
		if len(rows) != 1 or len(rows[0]) != 10:
			raise ZScoreSchemaError("Z-score completion metadata is missing or duplicated")
		row = rows[0]
		try:
			counts = tuple((ZScoreStatus(int(item.split(":", 1)[0])), int(item.split(":", 1)[1])) for item in str(row[8]).split(",") if item)
			if row[4] not in (0, 1, False, True):
				raise ValueError("Invalid z-score availability flag")
			return ZScoreSessionMetadata(str(row[0]), normalize_date_id(row[1]), str(row[2]), str(row[3]), bool(row[4]), datetime.fromisoformat(str(row[5])), int(str(row[6])), int(str(row[7])), counts, str(row[9]))
		except (TypeError, ValueError, OverflowError) as error:
			raise ZScoreSchemaError("Malformed z-score completion metadata") from error

	def _read_rows(self, cursor: Cursor, definition: ZScoreSeriesDefinition, dependency: ZScoreDependencySnapshot) -> tuple[ZScoreRow, ...]:
		resolved = self.table_names.resolve_zscore_tables((definition.window,))[definition.window]
		physical = resolved.price if definition.family.value == "price" else resolved.price_delta
		cursor.execute(f"SELECT date_id, ticker_id, zscore, status FROM {quote_identifier(physical)} WHERE date_id = %s ORDER BY ticker_id", (dependency.date_id,))
		rows: list[ZScoreRow] = []
		for row in cursor.fetchall():
			if len(row) != 4:
				raise ZScoreSchemaError("Invalid z-score row layout")
			try:
				status = ZScoreStatus(int(str(row[3])))
				if status is ZScoreStatus.READY:
					score = _as_float(row[2])
					if score is None:
						raise ValueError("Non-finite ready z-score")
				else:
					if row[2] is not None:
						raise ValueError("Unavailable z-score rows must store NULL scores")
					score = None
				ticker_id = normalize_date_id(row[1])
				if ticker_id not in dependency.ticker_ids or normalize_date_id(row[0]) != dependency.date_id:
					raise ValueError("Out-of-scope z-score row key")
				rows.append(ZScoreRow(definition.window, dependency.session, ticker_id, score, status, "stored"))
			except (TypeError, ValueError, OverflowError) as error:
				raise ZScoreSchemaError("Invalid z-score status or score") from error
		if len({row.ticker_id for row in rows}) != len(rows):
			raise ZScoreSchemaError("Duplicate z-score row key")
		return tuple(rows)
