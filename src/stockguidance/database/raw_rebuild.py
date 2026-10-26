"""Stage 6 raw observation and interpolation rebuilding.

This module is deliberately behind the Stage 5 ``affected_rows_cleared``
boundary.  It does not alter a canonical archive, consult an archive catalog,
or create database schema.  Payload preparation is a separate operation-owned
step; the runner only consumes the hash-bound payloads persisted in a pending
scope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from hashlib import sha256
from typing import Protocol, Sequence
from uuid import uuid4

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceConfig
from stockguidance.database.operation_recovery import RebuildResult
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.simplified_contract import (
	DATA_STATE_IDS,
	DataState,
	PendingUpdate,
	PreparedPayload,
	UnitKind,
	WorkUnit,
)
from stockguidance.database.simplified_schema import require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnection
from stockguidance.market_data.archive import archive_content, archive_path, decode_archive, unique_object
from stockguidance.market_data.identity import content_id, encode_json
from stockguidance.market_data.manifests import decode_response, response_payload
from stockguidance.market_data.providers.contracts import DownloadedSession, ProviderResponse, ResponseState
from stockguidance.market_data.quality import FIELDS, METHOD, ObservedBar, QualityFlag, QualityRecord, interpolate, invalid_fields
from stockguidance.market_data.records import count_value, records_id


RAW_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "transactions")
_RAW_COLUMNS = ("open", "high", "low", "close", "volume", "vwap", "transactions")


class RawRebuildError(RuntimeError):
	"""Raised when a raw unit cannot be reconstructed safely."""


class RawPayloadError(RawRebuildError):
	"""Raised when a prepared or canonical payload is malformed or mismatched."""


class RawPayloadUnavailable(RawRebuildError):
	"""A provider response is not yet usable as a completed raw session."""


class AdjustmentMismatchError(RawPayloadError):
	"""Raised when a canonical file cannot represent the configured adjustment."""


@dataclass(frozen=True)
class ValidatedRawPayload:
	"""A complete, source-validated response ready for one raw session unit."""

	session: TradingSession
	response: ProviderResponse
	available_at: datetime
	raw_id: str
	content_sha256: str


class PreparedPayloadReader(Protocol):
	def read(self, payload: PreparedPayload, session: TradingSession) -> ValidatedRawPayload: ...


class RawPayloadProvider(Protocol):
	def fetch(self, source: SourceConfig, session: TradingSession) -> ProviderResponse: ...


def _utc(value: object, name: str) -> datetime:
	if isinstance(value, str):
		try:
			value = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
		except ValueError as error:
			raise RawPayloadError(f"{name} must be a timestamp") from error
	if not isinstance(value, datetime):
		raise RawPayloadError(f"{name} must be a timestamp")
	if value.tzinfo is None:
		value = value.replace(tzinfo=timezone.utc)
	return value.astimezone(timezone.utc)


def _payload_digest(content: bytes) -> str:
	return sha256(content).hexdigest()


def _timestamp(milliseconds: int) -> datetime:
	if type(milliseconds) is not int or milliseconds < 0:
		raise RawPayloadError("Source timestamp must be a nonnegative integer")
	seconds, remainder = divmod(milliseconds, 1000)
	return datetime.fromtimestamp(seconds, timezone.utc) + timedelta(milliseconds=remainder)


def _decode_json(content: bytes) -> object:
	try:
		return json.loads(content, parse_float=Decimal, object_pairs_hook=unique_object)
	except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
		raise RawPayloadError("Prepared payload is not valid JSON") from error


def _validated_response(
	session: TradingSession,
	response: ProviderResponse,
	available_at: datetime,
) -> ProviderResponse:
	"""Run the same complete-response checks used by canonical publication."""

	available_at = _utc(available_at, "available_at")
	if available_at < session.closes_at:
		raise RawPayloadError("A completed source response precedes the session close")
	if response.state in {ResponseState.UNVERIFIED, ResponseState.NOT_YET_PUBLISHED}:
		raise RawPayloadUnavailable(f"Source response is {response.state.value}")
	if response.state not in {ResponseState.COMPLETE, ResponseState.NO_TRADING}:
		raise RawPayloadError(f"Unsupported source response state: {response.state.value}")
	if response.state is ResponseState.COMPLETE:
		# archive_content validates exact counts, timestamps, ticker identity, and
		# Decimal reconstruction.  The returned response is retained so explicit
		# ignored placeholder keys are not lost.
		try:
			archive_content(DownloadedSession(session, response, available_at))
		except (TypeError, ValueError) as error:
			raise RawPayloadError("Complete source response failed canonical validation") from error
	else:
		if not response.evidence or response.records or response.bad_records or response.has_more:
			raise RawPayloadError("No-trading response contains source observations")
	return response


def _raw_id(response: ProviderResponse) -> str:
	return records_id(response.records)


def _validated_payload(
	session: TradingSession,
	response: ProviderResponse,
	available_at: datetime,
	content_sha256: str,
) -> ValidatedRawPayload:
	response = _validated_response(session, response, available_at)
	return ValidatedRawPayload(session, response, _utc(available_at, "available_at"), _raw_id(response), content_sha256)


def _decode_prepared_content(content: bytes, session: TradingSession, available_at: datetime) -> ProviderResponse:
	"""Decode either canonical NDJSON or an operation-owned response envelope."""

	try:
		return decode_archive(content, session)
	except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
		try:
			return decode_response(_decode_json(content))
		except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
			raise RawPayloadError("Prepared payload is neither canonical NDJSON nor a response envelope") from error


class FilePreparedPayloadReader:
	"""Read and hash-check one operation-owned prepared payload."""

	def read(self, payload: PreparedPayload, session: TradingSession) -> ValidatedRawPayload:
		if payload.session_date != session.session_date:
			raise RawPayloadError("Prepared payload session does not match its raw unit")
		try:
			content = payload.temporary_path.read_bytes()
		except OSError as error:
			raise RawPayloadError(f"Prepared payload cannot be read: {payload.temporary_path}") from error
		if _payload_digest(content) != payload.sha256:
			raise RawPayloadError(f"Prepared payload checksum does not match: {payload.temporary_path}")
		response = _decode_prepared_content(content, session, payload.available_at)
		validated = _validated_payload(session, response, payload.available_at, payload.sha256)
		if validated.raw_id != payload.raw_id:
			raise RawPayloadError("Prepared payload raw identity does not match its pending scope")
		return validated


def read_canonical_file(path: Path, session: TradingSession, available_at: datetime) -> ValidatedRawPayload:
	"""Load the exact canonical daily file without consulting file metadata."""

	try:
		content = path.read_bytes()
	except OSError as error:
		raise RawPayloadError(f"Canonical file cannot be read: {path}") from error
	try:
		response = decode_archive(content, session)
	except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
		raise RawPayloadError(f"Malformed canonical file: {path}") from error
	return _validated_payload(session, response, available_at, _payload_digest(content))


def canonical_file(data_root: Path, session: TradingSession) -> Path:
	"""Return the only canonical location accepted for a session."""

	return archive_path(data_root, session.session_date)


class RawPayloadPreparer:
	"""Prepare a hash-bound payload before Stage 5 clearing begins.

	A normal attempt reads only the exact canonical path.  A forced attempt never
	reads that path and always obtains a new provider response.  The staged copy
	is the operation-owned artifact referenced by ``PreparedPayload``; file size
	and modification time are never used as replacement evidence.
	"""

	def __init__(
		self,
		source: SourceConfig,
		staging_root: Path,
		provider: RawPayloadProvider | None = None,
		*,
		clock: Callable[[], datetime] | None = None,
	) -> None:
		if not isinstance(staging_root, Path):
			raise ValueError("staging_root must be a Path")
		self.source = source
		self.staging_root = staging_root
		self.provider = provider
		self.clock = clock or (lambda: datetime.now(timezone.utc))

	def prepare(
		self,
		session: TradingSession,
		*,
		force_refresh: bool = False,
		available_at: datetime | None = None,
	) -> PreparedPayload:
		when = _utc(available_at or self.clock(), "available_at")
		if not force_refresh and self.source.identity.adjustment is PriceAdjustment.UNADJUSTED:
			path = canonical_file(self.source.data_root, session)
			if path.is_file():
				validated = read_canonical_file(path, session, when)
				content = path.read_bytes()
				return self._stage(session, content, validated.raw_id, when)
		elif not force_refresh and self.source.identity.adjustment is PriceAdjustment.ADJUSTED:
			# Canonical market_summary is explicitly unadjusted.  An adjusted
			# source must use its source-aware provider/fixture payload.
			pass
		if self.provider is None:
			if force_refresh:
				raise RawPayloadError("Forced refresh requires a provider response prepared before clearing")
			if self.source.identity.adjustment is PriceAdjustment.ADJUSTED:
				raise AdjustmentMismatchError("Adjusted source cannot use the unadjusted canonical file")
			raise RawPayloadError(f"Canonical file is missing: {canonical_file(self.source.data_root, session)}")
		try:
			response = self.provider.fetch(self.source, session)
		except Exception as error:
			raise RawPayloadError("Provider fallback failed while preparing a raw payload") from error
		validated = _validated_payload(session, response, when, "0" * 64)
		if response.state is ResponseState.COMPLETE:
			content = archive_content(DownloadedSession(session, response, when))
		else:
			content = encode_json(response_payload(response))
		return self._stage(session, content, validated.raw_id, when)

	def _stage(self, session: TradingSession, content: bytes, raw_id: str, available_at: datetime) -> PreparedPayload:
		self.staging_root.mkdir(parents=True, exist_ok=True)
		path = self.staging_root / f"{session.session_date.isoformat()}-{uuid4().hex}.payload"
		try:
			with path.open("xb") as stream:
				stream.write(content)
		except OSError as error:
			raise RawPayloadError(f"Cannot create operation-owned prepared payload: {path}") from error
		return PreparedPayload(
			f"{session.session_date.isoformat()}-{uuid4().hex}", session.session_date, path,
			_payload_digest(content), raw_id, available_at, _utc(self.clock(), "prepared_at"),
		)


def _db_id(value: object, name: str) -> int:
	if isinstance(value, bytes):
		value = value.decode("ascii")
	if isinstance(value, str) and value.isdecimal():
		value = int(value)
	if type(value) is not int or value <= 0:
		raise RawRebuildError(f"Invalid database {name}")
	return value


def _db_date(value: object) -> date:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	if isinstance(value, str):
		try:
			return date.fromisoformat(value)
		except ValueError:
			pass
	raise RawRebuildError("Invalid database session date")


def _db_state(value: object) -> DataState:
	state = DATA_STATE_IDS
	state_id = _db_id(value, "state_id")
	for code, identifier in state.items():
		if identifier == state_id:
			return code
	raise RawRebuildError("Unknown database data state")


def _json_value(values: Sequence[object | None]) -> str:
	return json.dumps([None if value is None else str(value) for value in values], separators=(",", ":"), ensure_ascii=False)


def _json_endpoint(values: tuple[tuple[object | None, ...], tuple[object | None, ...]]) -> str:
	return json.dumps([[None if value is None else str(value) for value in endpoint] for endpoint in values], separators=(",", ":"), ensure_ascii=False)


def _safe_raw_value(value: object, field: str) -> object | None:
	if value is None or isinstance(value, bool):
		return None
	try:
		decimal = value if isinstance(value, Decimal) else Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError):
		return None
	if not decimal.is_finite():
		return None
	if field in {"volume", "vwap"} and decimal < 0:
		return None
	if field == "transactions":
		try:
			return count_value(decimal)
		except ValueError:
			return None
	# Keep only values representable by the selected DECIMAL columns.  Invalid
	# semantic values (zero/negative prices or inconsistent OHLC) may remain as
	# raw fragments because state_id prevents them from becoming feature input.
	precision, scale = (20, 8) if field == "volume" else (42, 22) if field == "vwap" else (28, 8)
	exponent = int(decimal.as_tuple().exponent)
	if exponent < -scale or decimal.adjusted() >= precision - scale:
		return None
	return decimal


def _original_values(values: Sequence[object | None]) -> tuple[object | None, ...]:
	return tuple(None if value is None else value if isinstance(value, (str, Decimal, int)) else str(value) for value in values)


def _known_missing(values: Sequence[object | None]) -> bool:
	"""Recognize provider zero placeholders after NDJSON string decoding."""
	if len(values) != 7 or values[5] is not None:
		return False
	try:
		prices = tuple(Decimal(str(value)) for value in values[:5])
		transactions = Decimal(str(values[6])) if values[6] is not None else Decimal(0)
	except (InvalidOperation, TypeError, ValueError):
		return False
	return all(value == 0 for value in prices) and transactions == 0


class RawRebuildRunner:
	"""Rebuild raw sessions and repair rows after the durable clear flag.

	Delta and z-score units are returned as deferred work for Stage 7.  A later
	coordinator must compose this runner with those numerical stages before it can
	delete the pending row.
	"""

	def __init__(
		self,
		source: SourceConfig,
		calendar: SessionCalendar,
		*,
		table_names: TableNames | None = None,
		payload_reader: PreparedPayloadReader | None = None,
		clock: Callable[[], datetime] | None = None,
	) -> None:
		if calendar.calendar_id != source.calendar_id:
			raise ValueError("Raw rebuild calendar does not match source configuration")
		self.source = source
		self.calendar = calendar
		self.table_names = table_names or load_table_names()
		self.payload_reader = payload_reader or FilePreparedPayloadReader()
		self.clock = clock or (lambda: datetime.now(timezone.utc))

	def rebuild(self, operation: PendingUpdate, connection: WriteConnection) -> RebuildResult:
		if not isinstance(operation, PendingUpdate):
			raise ValueError("operation must be a PendingUpdate")
		if not operation.affected_rows_cleared:
			raise RawRebuildError("Raw rebuilding is unreachable before affected_rows_cleared")
		cursor = connection.cursor()
		committed: list[WorkUnit] = []
		explicit_non_ready: list[WorkUnit] = []
		deferred: list[WorkUnit] = []
		try:
			require_simplified_schema(cursor, self.table_names)
			payloads = {payload.session_date: payload for payload in operation.scope.payloads}
			for unit in operation.scope.rebuild:
				kind = UnitKind(unit.unit_kind)
				if kind is UnitKind.RAW_SESSION:
					if unit.date_id is None or unit.session_date is None:
						raise RawRebuildError("Raw work unit is incomplete")
					payload = payloads.get(unit.session_date)
					if payload is None:
						raise RawRebuildError(f"No prepared payload for raw session {unit.session_date}")
					if self._raw_complete(cursor, unit, payload):
						committed.append(unit)
						continue
					validated = self.payload_reader.read(payload, self._session(unit.session_date))
					outcome = self._write_raw_unit(cursor, connection, unit, validated)
					committed.append(unit)
					if outcome is not None:
						explicit_non_ready.append(unit)
			for unit in operation.scope.rebuild:
				kind = UnitKind(unit.unit_kind)
				if kind is UnitKind.REPAIR_ROW:
					if unit.date_id is None or unit.ticker_id is None or unit.session_date is None:
						raise RawRebuildError("Repair work unit is incomplete")
					result = self._write_repair_unit(cursor, connection, unit)
					if result is None:
						committed.append(unit)
					else:
						committed.append(unit)
						if result is DataState.BAD or result is DataState.MISSING:
							explicit_non_ready.append(unit)
						elif result is DataState.INTERPOLATED:
							explicit_non_ready = [
								candidate for candidate in explicit_non_ready
								if not (candidate.unit_kind == UnitKind.RAW_SESSION.value and candidate.date_id == unit.date_id)
							]
				elif kind is not UnitKind.RAW_SESSION:
					deferred.append(unit)
		finally:
			cursor.close()
		return RebuildResult(tuple(committed), tuple(explicit_non_ready), tuple(deferred), ())

	def _session(self, session_date: date) -> TradingSession:
		try:
			return self.calendar.session(session_date)
		except Exception as error:
			raise RawRebuildError(f"Session is not in the configured exchange calendar: {session_date}") from error

	def _raw_complete(self, cursor: Cursor, unit: WorkUnit, payload: PreparedPayload) -> bool:
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT transaction_date, raw_complete, raw_revision FROM {sessions} WHERE id = %s", (unit.date_id,))
		rows = cursor.fetchall()
		if len(rows) != 1 or _db_date(rows[0][0]) != unit.session_date:
			raise RawRebuildError("Raw work unit date ID does not resolve to its session")
		return rows[0][1] in (1, True) and rows[0][2] == payload.raw_id

	def _write_raw_unit(
		self,
		cursor: Cursor,
		connection: WriteConnection,
		unit: WorkUnit,
		payload: ValidatedRawPayload,
	) -> DataState | None:
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		prices = quote_identifier(self.table_names.table(TableRole.PRICES))
		repairs = quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))
		if unit.date_id is None or unit.session_date is None or payload.session.session_date != unit.session_date:
			raise RawRebuildError("Prepared raw payload does not match its work unit")
		try:
			cursor.execute("START TRANSACTION", ())
			cursor.execute(f"SELECT transaction_date FROM {sessions} WHERE id = %s", (unit.date_id,))
			rows = cursor.fetchall()
			if len(rows) != 1 or _db_date(rows[0][0]) != unit.session_date:
				raise RawRebuildError("Raw work unit date ID does not resolve to its session")
			cursor.execute(f"DELETE FROM {prices} WHERE date_id = %s", (unit.date_id,))
			cursor.execute(f"DELETE FROM {repairs} WHERE date_id = %s", (unit.date_id,))
			values_by_ticker: dict[str, tuple[object | None, ...]] = {}
			states_by_ticker: dict[str, DataState] = {}
			observed_by_ticker: dict[str, datetime | None] = {}
			metadata: list[tuple[str, DataState, tuple[object | None, ...], tuple[str, ...], datetime | None]] = []
			for bar in payload.response.records:
				if bar.ticker in values_by_ticker:
					raise RawRebuildError("Duplicate ticker in validated source records")
				values_by_ticker[bar.ticker] = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap, bar.transactions)
				states_by_ticker[bar.ticker] = DataState.GOOD
				observed_by_ticker[bar.ticker] = self._observed_at(payload.response, bar.ticker, payload.session)
			for ticker, original in payload.response.bad_records:
				if ticker in values_by_ticker:
					raise RawRebuildError("Duplicate ticker in valid and bad source records")
				missing = ticker in payload.response.ignored_placeholders or _known_missing(original)
				state = DataState.MISSING if missing else DataState.BAD
				values_by_ticker[ticker] = tuple(_safe_raw_value(value, field) for field, value in zip(RAW_FIELDS, original)) if not missing else (None,) * 7
				states_by_ticker[ticker] = state
				observed_by_ticker[ticker] = self._observed_at(payload.response, ticker, payload.session)
				metadata.append((ticker, state, _original_values(original), tuple(FIELDS) if missing else invalid_fields(original), observed_by_ticker[ticker]))
			for ticker in payload.response.ignored_placeholders:
				if ticker in values_by_ticker:
					continue
				values_by_ticker[ticker] = (None,) * 7
				states_by_ticker[ticker] = DataState.MISSING
				observed_by_ticker[ticker] = self._observed_at(payload.response, ticker, payload.session)
				metadata.append((ticker, DataState.MISSING, (None,) * 7, tuple(FIELDS), observed_by_ticker[ticker]))
			for ticker in sorted(values_by_ticker):
				ticker_id = self._ticker_id(cursor, ticker, payload.session.calendar_id)
				values = values_by_ticker[ticker]
				if states_by_ticker[ticker] is DataState.MISSING:
					values = (None,) * 7
				columns = ", ".join(_RAW_COLUMNS)
				placeholders = ", ".join("%s" for _ in range(11))
				cursor.execute(
					f"INSERT INTO {prices} (ticker_id, date_id, {columns}, state_id, observed_at) VALUES ({placeholders})",
					(ticker_id, unit.date_id, *values, DATA_STATE_IDS[states_by_ticker[ticker]], observed_by_ticker[ticker]),
				)
			for ticker, state, original, invalid, observed_at in metadata:
				ticker_id = self._ticker_id(cursor, ticker, payload.session.calendar_id)
				revision = content_id({"method": METHOD, "ticker": ticker, "date": payload.session.session_date.isoformat(), "original": [None if value is None else str(value) for value in original], "fields": list(invalid), "state": state.value})
				cursor.execute(
					f"INSERT INTO {repairs} (date_id, ticker_id, revision, state_id, original_values, effective_values, invalid_fields, interpolation_method, prior_date_id, following_date_id, endpoint_values, observed_at, available_at, endpoints_available_at, recorded_at) VALUES (%s,%s,%s,%s,%s,NULL,%s,NULL,NULL,NULL,NULL,%s,%s,NULL,%s)",
					(unit.date_id, ticker_id, revision, DATA_STATE_IDS[state], _json_value(original), _json_value(invalid), observed_at, payload.available_at, self.clock().astimezone(timezone.utc)),
				)
			completed_at = _utc(self.clock(), "raw_completed_at")
			cursor.execute(f"UPDATE {sessions} SET raw_complete = 1, raw_available_at = %s, raw_completed_at = %s, raw_revision = %s WHERE id = %s", (payload.available_at, completed_at, payload.raw_id, unit.date_id))
			connection.commit()
		except Exception:
			connection.rollback()
			raise
		return next((state for _, state, _, _, _ in metadata if state in {DataState.BAD, DataState.MISSING}), None)

	def _observed_at(self, response: ProviderResponse, ticker: str, session: TradingSession) -> datetime | None:
		timestamp = dict(response.source_timestamps).get(ticker)
		if timestamp is None:
			return None
		observed = _timestamp(timestamp)
		if observed.astimezone(self.calendar.timezone).date() != session.session_date:
			raise RawPayloadError(f"Source timestamp does not belong to {session.session_date}: {ticker}")
		return observed

	def _ticker_id(self, cursor: Cursor, ticker: str, exchange: str) -> int:
		tickers = quote_identifier(self.table_names.table(TableRole.TICKERS))
		cursor.execute(f"SELECT id, exchange FROM {tickers} WHERE label = %s", (ticker,))
		rows = cursor.fetchall()
		if len(rows) > 1:
			raise RawRebuildError("Ticker identity is duplicated")
		if rows:
			identifier = _db_id(rows[0][0], "ticker_id")
			if rows[0][1] is not None and rows[0][1] != exchange:
				raise RawRebuildError(f"Ticker exchange identity changed for {ticker}")
			if rows[0][1] is None:
				cursor.execute(f"UPDATE {tickers} SET exchange = %s WHERE id = %s", (exchange, identifier))
			return identifier
		cursor.execute(f"INSERT INTO {tickers} (label, exchange) VALUES (%s,%s)", (ticker, exchange))
		cursor.execute(f"SELECT id FROM {tickers} WHERE label = %s", (ticker,))
		rows = cursor.fetchall()
		if len(rows) != 1:
			raise RawRebuildError("Ticker insert did not resolve one stable identity")
		return _db_id(rows[0][0], "ticker_id")

	def _write_repair_unit(self, cursor: Cursor, connection: WriteConnection, unit: WorkUnit) -> DataState | None:
		prices = quote_identifier(self.table_names.table(TableRole.PRICES))
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		if unit.date_id is None or unit.ticker_id is None or unit.session_date is None:
			raise RawRebuildError("Repair work unit is incomplete")
		try:
			cursor.execute(f"SELECT transaction_date FROM {sessions} WHERE id = %s", (unit.date_id,))
			date_rows = cursor.fetchall()
			if len(date_rows) != 1 or _db_date(date_rows[0][0]) != unit.session_date:
				raise RawRebuildError("Repair work unit date ID does not resolve to its session")
			row = self._load_raw_row(cursor, unit.ticker_id, unit.date_id)
			if row is None:
				return DataState.MISSING
			state, original, _observation = row
			if state is DataState.GOOD or state is DataState.MISSING:
				return None
			if state is DataState.INTERPOLATED and original is None:
				raise RawRebuildError("Interpolated raw row has no original repair metadata")
			observations, by_key = self._observations_for_ticker(cursor, unit.ticker_id)
			record = next((item for item in interpolate(tuple(observations), _utc(self.clock(), "repair time")) if item.observation.session_date == unit.session_date), None)
			if record is None:
				return DataState.BAD
			if self._crosses_missing(by_key, record):
				record = QualityRecord(record.observation, QualityFlag.BAD, record.fields, gap_length=record.gap_length)
			metadata = self._current_repair(cursor, unit.date_id, unit.ticker_id)
			if metadata is not None and metadata[0] == record.revision:
				return DataState.INTERPOLATED if record.effective is not None else DataState.BAD
			cursor.execute("START TRANSACTION", ())
			try:
				if record.effective is None:
					self._update_raw_state(cursor, unit.ticker_id, unit.date_id, DataState.BAD, tuple(_safe_raw_value(value, field) for field, value in zip(RAW_FIELDS, record.observation.values)))
					self._replace_repair(cursor, unit, record, DataState.BAD, None, None, None)
					connection.commit()
					return DataState.BAD
				effective = tuple(record.effective)
				self._update_raw_state(cursor, unit.ticker_id, unit.date_id, DataState.INTERPOLATED, effective)
				self._replace_repair(cursor, unit, record, DataState.INTERPOLATED, effective, record.prior, record.following)
				connection.commit()
				return DataState.INTERPOLATED
			except Exception:
				connection.rollback()
				raise
		finally:
			# The transaction above is intentionally per repair work unit.  There is
			# no long-lived transaction across the persisted scope.
			pass

	def _load_raw_row(self, cursor: Cursor, ticker_id: int, date_id: int) -> tuple[DataState, tuple[object | None, ...] | None, datetime | None] | None:
		prices = quote_identifier(self.table_names.table(TableRole.PRICES))
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT r.state_id, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions, r.observed_at, s.raw_available_at FROM {prices} r JOIN {sessions} s ON s.id = r.date_id WHERE r.ticker_id = %s AND r.date_id = %s", (ticker_id, date_id))
		rows = cursor.fetchall()
		if not rows:
			return None
		if len(rows) != 1:
			raise RawRebuildError("Raw observation key is duplicated")
		state = _db_state(rows[0][0])
		metadata = self._current_repair(cursor, date_id, ticker_id)
		original = None if metadata is None else metadata[1]
		return state, original, None if rows[0][9] is None else _utc(rows[0][9], "raw_available_at")

	def _observations_for_ticker(self, cursor: Cursor, ticker_id: int) -> tuple[list[ObservedBar], Mapping[tuple[date, int], DataState]]:
		prices = quote_identifier(self.table_names.table(TableRole.PRICES))
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT r.date_id, s.transaction_date, r.state_id, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions, r.observed_at, s.raw_available_at FROM {prices} r JOIN {sessions} s ON s.id = r.date_id WHERE r.ticker_id = %s ORDER BY s.transaction_date, r.date_id", (ticker_id,))
		rows = cursor.fetchall()
		observed: list[ObservedBar] = []
		states: dict[tuple[date, int], DataState] = {}
		for row in rows:
			date_id, session_date = _db_id(row[0], "date_id"), _db_date(row[1])
			state = _db_state(row[2])
			metadata = self._current_repair(cursor, date_id, ticker_id)
			if state is DataState.GOOD:
				values = tuple(row[3:10])
			elif metadata is not None:
				values = self._decode_original(metadata[1])
			else:
				if state is DataState.INTERPOLATED:
					raise RawRebuildError("Interpolated raw row has no original repair metadata")
				values = tuple(row[3:10])
			session = self._session(session_date)
			available = row[11] if row[11] is not None else session.closes_at
			observed.append(ObservedBar(self._ticker_label(cursor, ticker_id), session_date, session.sequence, values, _utc(available, "available_at")))
			states[(session_date, date_id)] = state
		return observed, states

	def _ticker_label(self, cursor: Cursor, ticker_id: int) -> str:
		tickers = quote_identifier(self.table_names.table(TableRole.TICKERS))
		cursor.execute(f"SELECT label FROM {tickers} WHERE id = %s", (ticker_id,))
		rows = cursor.fetchall()
		if len(rows) != 1 or not isinstance(rows[0][0], str):
			raise RawRebuildError("Raw observation ticker identity is missing")
		return rows[0][0]

	def _current_repair(self, cursor: Cursor, date_id: int, ticker_id: int) -> tuple[str, tuple[object | None, ...]] | None:
		repairs = quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))
		cursor.execute(f"SELECT revision, original_values FROM {repairs} WHERE date_id = %s AND ticker_id = %s ORDER BY recorded_at DESC, id DESC LIMIT 1", (date_id, ticker_id))
		rows = cursor.fetchall()
		if not rows:
			return None
		value = rows[0][1]
		if isinstance(value, bytes):
			value = value.decode("utf-8")
		try:
			decoded = json.loads(value) if isinstance(value, str) else value
		except json.JSONDecodeError as error:
			raise RawRebuildError("Repair metadata original values are invalid JSON") from error
		if not isinstance(decoded, list) or len(decoded) != 7:
			raise RawRebuildError("Repair metadata original values are incomplete")
		return str(rows[0][0]), tuple(decoded)

	def _decode_original(self, values: tuple[object | None, ...]) -> tuple[object | None, ...]:
		return tuple(None if value is None else value for value in values)

	def _crosses_missing(self, by_key: Mapping[tuple[date, int], DataState], record: QualityRecord) -> bool:
		if record.prior is None or record.following is None:
			return False
		prior, following = self._session(record.prior), self._session(record.following)
		return any(state is DataState.MISSING for (session_date, _), state in by_key.items() if prior.session_date < session_date < following.session_date)

	def _update_raw_state(self, cursor: Cursor, ticker_id: int, date_id: int, state: DataState, values: Sequence[object | None]) -> None:
		prices = quote_identifier(self.table_names.table(TableRole.PRICES))
		if len(values) != 7:
			raise RawRebuildError("Raw value layout is incomplete")
		cursor.execute(f"UPDATE {prices} SET open = %s, high = %s, low = %s, close = %s, volume = %s, vwap = %s, transactions = %s, state_id = %s WHERE ticker_id = %s AND date_id = %s", (*values, DATA_STATE_IDS[state], ticker_id, date_id))

	def _replace_repair(
		self,
		cursor: Cursor,
		unit: WorkUnit,
		record: QualityRecord,
		state: DataState,
		effective: Sequence[object | None] | None,
		prior: date | None,
		following: date | None,
	) -> None:
		repairs = quote_identifier(self.table_names.table(TableRole.REPAIR_METADATA))
		if unit.date_id is None or unit.ticker_id is None:
			raise RawRebuildError("Repair work unit is incomplete")
		prior_id = self._date_id_for(cursor, prior) if prior is not None else None
		following_id = self._date_id_for(cursor, following) if following is not None else None
		endpoint_values = None
		if state is DataState.INTERPOLATED:
			if len(record.endpoint_values) != 2:
				raise RawRebuildError("Interpolated result has no endpoint values")
			endpoint_values = _json_endpoint((tuple(record.endpoint_values[0]), tuple(record.endpoint_values[1])))
		original = tuple(record.observation.values)
		invalid = tuple(record.fields)
		revision = record.revision
		cursor.execute(f"DELETE FROM {repairs} WHERE date_id = %s AND ticker_id = %s", (unit.date_id, unit.ticker_id))
		cursor.execute(
			f"INSERT INTO {repairs} (date_id, ticker_id, revision, state_id, original_values, effective_values, invalid_fields, interpolation_method, prior_date_id, following_date_id, endpoint_values, observed_at, available_at, endpoints_available_at, recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
			(unit.date_id, unit.ticker_id, revision, DATA_STATE_IDS[state], _json_value(original), None if effective is None else _json_value(effective), _json_value(invalid), METHOD if state is DataState.INTERPOLATED else None, prior_id, following_id, endpoint_values, None, record.available_at, record.endpoints_available_at, _utc(self.clock(), "recorded_at")),
		)

	def _date_id_for(self, cursor: Cursor, session_date: date | None) -> int | None:
		if session_date is None:
			return None
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT id FROM {sessions} WHERE transaction_date = %s", (session_date,))
		rows = cursor.fetchall()
		if len(rows) != 1:
			raise RawRebuildError("Interpolation endpoint session is missing")
		return _db_id(rows[0][0], "endpoint date_id")

