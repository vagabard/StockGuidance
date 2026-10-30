"""Read-only simplified-database adapter for frozen-universe selection."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from stockguidance.database.dates import normalize_date_id, normalize_session_date
from stockguidance.database.repositories.mysql_sessions import ConnectionFactory, Cursor
from stockguidance.database.simplified_contract import DataState
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.market_data.identity import content_id
from stockguidance.universe.selection import (
	RawObservationState,
	SourceSessionEvidence,
	TickerIdentity,
	UniverseDataRepository,
	UniverseObservation,
	UniverseReadRequest,
	UniverseSourceData,
)
from stockguidance.validation.coverage import CoverageState


def _int(value: object, name: str) -> int:
	if type(value) is not int or value < 1:
		raise ValueError(f"Invalid {name}")
	return value


def _decimal(value: object, name: str) -> Decimal:
	try:
		result = value if isinstance(value, Decimal) else Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError):
		raise ValueError(f"Invalid {name}") from None
	if not result.is_finite():
		raise ValueError(f"Invalid {name}")
	return result


def _optional_decimal(value: object, name: str) -> Decimal | None:
	return None if value is None else _decimal(value, name)


def _optional_count(value: object) -> int | None:
	if value is None:
		return None
	if isinstance(value, bool):
		raise ValueError("Invalid transaction count")
	if isinstance(value, int):
		result = value
	elif isinstance(value, Decimal):
		if not value.is_finite() or value != value.to_integral_value():
			raise ValueError("Invalid transaction count")
		result = int(value)
	elif isinstance(value, float):
		if not value.is_integer():
			raise ValueError("Invalid transaction count")
		result = int(value)
	else:
		raise ValueError("Invalid transaction count")
	if result < 0:
		raise ValueError("Invalid transaction count")
	return result


class MySQLUniverseRepository(UniverseDataRepository):
	"""Load exact session/ticker rows and simplified raw completion evidence.

	The adapter never creates or migrates schema.  ``raw_complete`` and the raw
	revision on the simplified session table establish whole-session evidence;
	individual ``state_id`` values establish per-ticker evidence.
	"""

	def __init__(self, source: Any, connect: ConnectionFactory, table_names: TableNames | None = None) -> None:
		self.source = source
		self.connect = connect
		self.table_names = table_names or load_table_names()

	def read(self, request: UniverseReadRequest) -> UniverseSourceData:
		if not isinstance(request, UniverseReadRequest):
			raise ValueError("Universe repository requires a typed read request")
		connection = self.connect(self.source.database_config)
		try:
			cursor = connection.cursor()
			try:
				tickers = self._read_tickers(cursor, request)
				coverage = self._read_coverage(cursor, request)
				observations = self._read_observations(cursor, request, tickers)
			finally:
				cursor.close()
		finally:
			connection.close()
		revision = content_id({
			"coverage": [item.to_mapping() for item in coverage],
			"observations": [
				(item.ticker_id, item.session.session_date.isoformat(), item.state.value, None if item.close is None else str(item.close), None if item.volume is None else str(item.volume), item.transactions)
				for item in observations
			],
		})
		return UniverseSourceData(tickers, coverage, observations, revision)

	def _read_tickers(self, cursor: Cursor, request: UniverseReadRequest) -> tuple[TickerIdentity, ...]:
		table = quote_identifier(self.table_names.table(TableRole.TICKERS))
		parameters: tuple[object, ...] = ()
		where = ""
		if request.ticker_ids is not None:
			placeholders = ",".join("%s" for _ in request.ticker_ids)
			where = f" WHERE id IN ({placeholders})"
			parameters = request.ticker_ids
		cursor.execute(f"SELECT id, label FROM {table}{where} ORDER BY id", parameters)
		rows = cursor.fetchall()
		result: list[TickerIdentity] = []
		for row in rows:
			if len(row) != 2 or not isinstance(row[1], str):
				raise ValueError("Universe ticker query returned an invalid row")
			result.append(TickerIdentity(_int(row[0], "ticker ID"), row[1]))
		if request.ticker_ids is not None and {item.ticker_id for item in result} != set(request.ticker_ids):
			raise ValueError("Explicit universe ticker IDs are not all present in the database")
		return tuple(result)

	def _read_coverage(self, cursor: Cursor, request: UniverseReadRequest) -> tuple[SourceSessionEvidence, ...]:
		dates = tuple(item.session_date for item in request.sessions)
		placeholders = ",".join("%s" for _ in dates)
		table = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		cursor.execute(f"SELECT id, transaction_date, raw_complete, raw_available_at, raw_revision FROM {table} WHERE transaction_date IN ({placeholders}) ORDER BY transaction_date", dates)
		by_date = {item.session_date: item for item in request.sessions}
		result: list[SourceSessionEvidence] = []
		seen: set[object] = set()
		for row in cursor.fetchall():
			if len(row) != 5:
				raise ValueError("Universe session query returned an invalid row")
			day = normalize_session_date(row[1])
			if day not in by_date or day in seen:
				raise ValueError("Universe session query returned a duplicate or out-of-scope row")
			seen.add(day)
			complete = row[2] in (1, True)
			available_at = row[3]
			if isinstance(available_at, datetime) and available_at.tzinfo is None:
				available_at = available_at.replace(tzinfo=timezone.utc)
			if available_at is not None and not isinstance(available_at, datetime):
				raise ValueError("Universe session query returned an invalid availability timestamp")
			revision = row[4] if isinstance(row[4], str) and row[4] else None
			if request.as_of is not None and isinstance(available_at, datetime) and available_at > request.as_of:
				state, detail = CoverageState.UNVERIFIED, "Raw session completion is after the requested information cutoff"
			elif complete and revision is not None and available_at is not None:
				state, detail = CoverageState.COMPLETE, "Simplified raw session completion and revision evidence verified"
			else:
				state, detail = CoverageState.INCOMPLETE, "Simplified raw session completion or revision evidence is incomplete"
			result.append(SourceSessionEvidence(by_date[day], state, detail, revision))
		for session in request.sessions:
			if session.session_date not in seen:
				result.append(SourceSessionEvidence(session, CoverageState.UNVERIFIED, "No database session completion row was returned"))
		return tuple(sorted(result, key=lambda item: item.session.sequence))

	def _read_observations(self, cursor: Cursor, request: UniverseReadRequest, tickers: tuple[TickerIdentity, ...]) -> tuple[UniverseObservation, ...]:
		dates = tuple(item.session_date for item in request.sessions)
		date_placeholders = ",".join("%s" for _ in dates)
		ticker_ids = request.ticker_ids or tuple(item.ticker_id for item in tickers)
		if not ticker_ids:
			return ()
		ticker_placeholders = ",".join("%s" for _ in ticker_ids)
		columns = ["s.transaction_date", "t.id", "p.close"]
		if request.read_volume:
			columns.append("p.volume")
		if request.read_transactions:
			columns.append("p.transactions")
		columns.extend(("ds.code", "p.state_id"))
		prices = quote_identifier(self.table_names.table(TableRole.PRICES))
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		tickers_table = quote_identifier(self.table_names.table(TableRole.TICKERS))
		states = quote_identifier(self.table_names.table(TableRole.DATA_STATES))
		query = (
			f"SELECT {', '.join(columns)} FROM {sessions} s CROSS JOIN {tickers_table} t "
			f"LEFT JOIN {prices} p ON p.date_id = s.id AND p.ticker_id = t.id "
			f"LEFT JOIN {states} ds ON ds.id = p.state_id "
			f"WHERE s.transaction_date IN ({date_placeholders}) AND t.id IN ({ticker_placeholders}) "
			"ORDER BY s.transaction_date, t.id"
		)
		cursor.execute(query, (*dates, *ticker_ids))
		by_date = {item.session_date: item for item in request.sessions}
		result: list[UniverseObservation] = []
		for row in cursor.fetchall():
			minimum = 5 + int(request.read_volume) + int(request.read_transactions)
			if len(row) != minimum:
				raise ValueError("Universe observation query returned an invalid row")
			day = normalize_session_date(row[0])
			ticker_id = _int(row[1], "observation ticker ID")
			if day not in by_date or ticker_id not in ticker_ids:
				raise ValueError("Universe observation query returned an out-of-scope row")
			position = 3
			close_raw = row[2]
			volume_raw = row[position] if request.read_volume else None
			position += int(request.read_volume)
			transactions_raw = row[position] if request.read_transactions else None
			position += int(request.read_transactions)
			state_code, state_id = row[position], row[position + 1]
			if state_code is None or state_id is None:
				state = RawObservationState.MISSING
				close = volume = None
				transactions = None
			else:
				try:
					data_state = DataState(str(state_code))
				except ValueError:
					raise ValueError("Universe observation query returned an unknown data state") from None
				state = RawObservationState.AVAILABLE if data_state in {DataState.GOOD, DataState.INTERPOLATED} else RawObservationState.INVALID if data_state is DataState.BAD else RawObservationState.MISSING
				try:
					close = None if close_raw is None else _decimal(close_raw, "close")
					volume = None if volume_raw is None else _decimal(volume_raw, "volume")
					transactions = _optional_count(transactions_raw)
					if state is RawObservationState.AVAILABLE and (close is None or close <= 0 or (volume is not None and volume < 0)):
						state = RawObservationState.INVALID
					if state is not RawObservationState.AVAILABLE:
						close = None if close is None or close <= 0 else close
						volume = None if volume is None or volume < 0 else volume
				except ValueError:
					state, close, volume, transactions = RawObservationState.INVALID, None, None, None
			result.append(UniverseObservation(ticker_id, by_date[day], state, close, volume, transactions))
		return tuple(result)


__all__ = ["MySQLUniverseRepository"]
