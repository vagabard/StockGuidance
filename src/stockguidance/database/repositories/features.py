"""Read-only simplified-database adapter for configured feature channels."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
import math
from typing import Any

from stockguidance.config.source import SourceConfig
from stockguidance.database.operation_recovery import DatabaseReadinessGuard, ReadScope, ReadinessState, consistent_read_transaction
from stockguidance.database.simplified_contract import DATA_STATE_BY_ID, DataState
from stockguidance.database.simplified_schema import require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.features.channels import FeatureChannelData, FeatureChannelObservation, FeatureChannelQuery, FeatureChannelRepository, FeatureReadinessEvidence, FeatureReadinessState, FeatureValueState
from stockguidance.features.specification import FeatureChannel, PriceDeltaZScore, PriceZScore, VolumeZScore, channel_key
from stockguidance.market_data.identity import content_id, source_key
from stockguidance.zscores.results import ZScoreStatus
from stockguidance.zscores.specification import CALCULATION_VERSION, DELTA_DEFINITION, FLOAT32_PRECISION, ZScoreFamily
from stockguidance.zscores.storage import ZScoreSeriesDefinition
from stockguidance.database.preparation import VOLUME_CALCULATION_VERSION


class FeatureChannelReadError(RuntimeError):
	"""Raised when prepared feature evidence is absent, stale, or pending."""


def _placeholders(values: Sequence[object]) -> str:
	if not values:
		raise ValueError("Feature channel queries require bounded values")
	return ",".join("%s" for _ in values)


def _date_id(value: object) -> int:
	if isinstance(value, bool):
		raise ValueError("Invalid feature date ID")
	if isinstance(value, int):
		result = value
	elif isinstance(value, bytes):
		try:
			result = int(value.decode("ascii"))
		except (TypeError, ValueError, OverflowError):
			raise ValueError("Invalid feature date ID") from None
	elif isinstance(value, str):
		try:
			result = int(value)
		except (TypeError, ValueError, OverflowError):
			raise ValueError("Invalid feature date ID") from None
	else:
		raise ValueError("Invalid feature date ID") from None
	if result <= 0:
		raise ValueError("Invalid feature date ID")
	return result


def _session_date(value: object) -> date:
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	if isinstance(value, bytes):
		return date.fromisoformat(value.decode("ascii")[:10])
	if isinstance(value, str):
		return date.fromisoformat(value[:10])
	raise ValueError("Invalid feature session date")


def _float(value: object) -> float | None:
	if isinstance(value, bool) or not isinstance(value, (int, float, str)):
		return None
	try:
		result = float(value)
	except (TypeError, ValueError, OverflowError):
		return None
	return result if math.isfinite(result) else None


def _zscore_state(status: ZScoreStatus) -> FeatureValueState:
	return {
		ZScoreStatus.READY: FeatureValueState.READY,
		ZScoreStatus.INSUFFICIENT_HISTORY: FeatureValueState.INSUFFICIENT_HISTORY,
		ZScoreStatus.MISSING_INPUT: FeatureValueState.MISSING,
		ZScoreStatus.UNVERIFIED_INPUT: FeatureValueState.UNVERIFIED,
		ZScoreStatus.INVALID_INPUT: FeatureValueState.INVALID,
		ZScoreStatus.ZERO_VARIANCE: FeatureValueState.ZERO_VARIANCE,
	}[status]


def _channel_definition(source: SourceConfig, query: FeatureChannelQuery, channel: PriceZScore | PriceDeltaZScore | VolumeZScore) -> tuple[ZScoreSeriesDefinition, str]:
	if isinstance(channel, PriceZScore):
		family, calculation, delta, table_attr = ZScoreFamily.PRICE, CALCULATION_VERSION, "not_applicable", "price"
	elif isinstance(channel, PriceDeltaZScore):
		family, calculation, delta, table_attr = ZScoreFamily.PRICE_DELTA, CALCULATION_VERSION, DELTA_DEFINITION, "price_delta"
	else:
		family, calculation, delta, table_attr = ZScoreFamily.VOLUME, VOLUME_CALCULATION_VERSION, "not_applicable", "volume"
	definition = ZScoreSeriesDefinition(source_key(source.identity), query.scored_sessions[0].calendar_id, family, channel.window, calculation, FLOAT32_PRECISION, delta)
	return definition, table_attr


class MySQLFeatureChannelRepository(FeatureChannelRepository):
	"""Consume only completed prepared rows; never create or repair schema."""

	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None = None) -> None:
		self.source = source
		self.connect = connect
		self.table_names = table_names or load_table_names()

	def read(self, query: FeatureChannelQuery) -> FeatureChannelData:
		if query.source != self.source.identity:
			raise ValueError("Feature channel query and database source identities differ")
		if not query.specification.external_channels:
			return FeatureChannelData(query, (), content_id({"channels": [], "sessions": [item.session_date.isoformat() for item in query.scored_sessions]}))
		with consistent_read_transaction(self.source, self.connect) as transaction:
			cursor = transaction.cursor()
			require_simplified_schema(cursor, self.table_names)
			date_table = quote_identifier(self.table_names.table(TableRole.SESSIONS))
			date_values = tuple(item.session_date for item in query.dependency_sessions)
			cursor.execute(f"SELECT id, transaction_date FROM {date_table} WHERE transaction_date IN ({_placeholders(date_values)}) ORDER BY transaction_date", date_values)
			date_ids = {_session_date(row[1]): _date_id(row[0]) for row in cursor.fetchall() if len(row) == 2}
			by_date_id = date_ids
			if len(by_date_id) != len(query.dependency_sessions):
				missing = next(item.session_date for item in query.dependency_sessions if item.session_date not in by_date_id)
				raise FeatureChannelReadError(f"Prepared feature session is absent: {missing.isoformat()}")
			readiness = DatabaseReadinessGuard(self.table_names).check(ReadScope(date_ids=tuple(sorted(by_date_id.values()))), transaction)
			if readiness.status is ReadinessState.UNFINISHED_OPERATION:
				raise FeatureChannelReadError(readiness.reason or "A pending database operation overlaps feature inputs")
			observations: list[FeatureChannelObservation] = []
			evidence: list[FeatureReadinessEvidence] = []
			for channel in query.specification.external_channels:
				if channel is FeatureChannel.RAW_VOLUME:
					rows, channel_evidence = self._read_raw_volume(cursor, query, by_date_id)
				else:
					assert isinstance(channel, (PriceZScore, PriceDeltaZScore, VolumeZScore))
					rows, channel_evidence = self._read_zscore(cursor, query, channel, by_date_id)
				observations.extend(rows)
				evidence.extend(channel_evidence)
			return FeatureChannelData(query, tuple(observations), content_id({"observations": [(channel_key(item.channel), item.session.session_date.isoformat(), item.ticker_id, item.value, item.state.value, item.revision) for item in observations]}), tuple(evidence))

	def _read_raw_volume(self, cursor: Any, query: FeatureChannelQuery, date_ids: dict[date, int]) -> tuple[list[FeatureChannelObservation], list[FeatureReadinessEvidence]]:
		sessions = quote_identifier(self.table_names.table(TableRole.SESSIONS))
		prices = quote_identifier(self.table_names.table(TableRole.PRICES))
		dates = tuple(item.session_date for item in query.scored_sessions)
		cursor.execute(
			f"SELECT s.transaction_date, s.raw_complete, s.raw_available_at, s.raw_revision, p.ticker_id, p.volume, p.state_id FROM {sessions} s LEFT JOIN {prices} p ON p.date_id = s.id AND p.ticker_id IN ({_placeholders(query.specification.ticker_ids)}) WHERE s.transaction_date IN ({_placeholders(dates)}) ORDER BY s.transaction_date, p.ticker_id",
			query.specification.ticker_ids + dates,
		)
		by_key: dict[tuple[object, int], tuple[object, ...]] = {}
		for row in cursor.fetchall():
			if len(row) != 7 or _session_date(row[0]) not in dates or row[4] is None:
				continue
			by_key[(_session_date(row[0]), _date_id(row[4]))] = row
		observations: list[FeatureChannelObservation] = []
		for session in query.scored_sessions:
			for ticker_id in query.specification.ticker_ids:
				row = by_key.get((session.session_date, ticker_id))
				if row is None or row[1] not in (1, True) or row[2] is None or row[3] is None:
					observations.append(FeatureChannelObservation(FeatureChannel.RAW_VOLUME, session, ticker_id, None, FeatureValueState.UNVERIFIED, "raw-session-evidence"))
					continue
				available_at = row[2]
				if isinstance(available_at, datetime) and available_at.tzinfo is None:
					available_at = available_at.replace(tzinfo=query.as_of.tzinfo)
				if isinstance(available_at, datetime) and available_at > query.as_of:
					observations.append(FeatureChannelObservation(FeatureChannel.RAW_VOLUME, session, ticker_id, None, FeatureValueState.NOT_YET_AVAILABLE, str(row[3])))
					continue
				state = DATA_STATE_BY_ID.get(_date_id(row[6])) if row[6] is not None else None
				value = _float(row[5])
				if state is DataState.GOOD and value is not None and value >= 0:
					observations.append(FeatureChannelObservation(FeatureChannel.RAW_VOLUME, session, ticker_id, value, revision=str(row[3])))
				elif state is DataState.MISSING or row[5] is None:
					observations.append(FeatureChannelObservation(FeatureChannel.RAW_VOLUME, session, ticker_id, None, FeatureValueState.MISSING, str(row[3])))
				else:
					observations.append(FeatureChannelObservation(FeatureChannel.RAW_VOLUME, session, ticker_id, None, FeatureValueState.INVALID, str(row[3])))
		evidence = [FeatureReadinessEvidence(FeatureChannel.RAW_VOLUME, session, FeatureReadinessState.READY, "raw-session-evidence", "Raw volume session is complete") for session in query.scored_sessions]
		return observations, evidence

	def _read_zscore(self, cursor: Any, query: FeatureChannelQuery, channel: PriceZScore | PriceDeltaZScore | VolumeZScore, date_ids: dict[date, int]) -> tuple[list[FeatureChannelObservation], list[FeatureReadinessEvidence]]:
		definition, attribute = _channel_definition(self.source, query, channel)
		series = quote_identifier(self.table_names.table(TableRole.ZSCORE_SERIES))
		cursor.execute(f"SELECT series_key, schema_version, source_scope, calendar_id, family, window, calculation_version, delta_definition, precision, missing_value_policy FROM {series} WHERE series_key = %s", (definition.series_key,))
		series_rows = cursor.fetchall()
		if len(series_rows) != 1 or tuple(series_rows[0]) != (definition.series_key, definition.schema_version, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy):
			raise FeatureChannelReadError(f"Prepared {channel_key(channel)} series definition is missing or incompatible")
		physical = self.table_names.resolve_zscore_tables((channel.window,))[channel.window]
		table = quote_identifier(getattr(physical, attribute))
		metadata = quote_identifier(self.table_names.table(TableRole.ZSCORE_SESSIONS))
		observations: list[FeatureChannelObservation] = []
		evidence: list[FeatureReadinessEvidence] = []
		for session in query.scored_sessions:
			date_id = date_ids[session.session_date]
			cursor.execute(f"SELECT dependency_digest, availability_digest, actual_input_available, row_count, ready_count, content_digest FROM {metadata} WHERE series_key = %s AND date_id = %s", (definition.series_key, date_id))
			metadata_rows = cursor.fetchall()
			if len(metadata_rows) != 1:
				for ticker_id in query.specification.ticker_ids:
					observations.append(FeatureChannelObservation(channel, session, ticker_id, None, FeatureValueState.PENDING, "prepared-completion-metadata"))
				evidence.append(FeatureReadinessEvidence(channel, session, FeatureReadinessState.PENDING, "prepared-completion-metadata", "Prepared feature completion metadata is missing"))
				continue
			metadata_row = metadata_rows[0]
			actual_available = metadata_row[2] in (1, True)
			try:
				row_count, ready_count = int(str(metadata_row[3])), int(str(metadata_row[4]))
			except (TypeError, ValueError, OverflowError):
				raise FeatureChannelReadError("Prepared z-score completion metadata has invalid row counts") from None
			if row_count != len(query.specification.ticker_ids) or ready_count < 0 or ready_count > row_count:
				raise FeatureChannelReadError("Prepared z-score completion metadata does not cover the requested ticker universe")
			cursor.execute(f"SELECT date_id, ticker_id, zscore, status FROM {table} WHERE date_id = %s AND ticker_id IN ({_placeholders(query.specification.ticker_ids)}) ORDER BY ticker_id", (date_id, *query.specification.ticker_ids))
			stored = {(_date_id(row[0]), _date_id(row[1])): row for row in cursor.fetchall() if len(row) == 4}
			if len(stored) != row_count:
				raise FeatureChannelReadError("Prepared z-score rows are incomplete or duplicated")
			revision = content_id({"dependency": str(metadata_row[0]), "availability": str(metadata_row[1]), "content": str(metadata_row[5])})
			for ticker_id in query.specification.ticker_ids:
				row = stored.get((date_id, ticker_id))
				if row is None:
					observations.append(FeatureChannelObservation(channel, session, ticker_id, None, FeatureValueState.PENDING, revision))
					continue
				try:
					status = ZScoreStatus(int(str(row[3])))
				except (TypeError, ValueError):
					status = ZScoreStatus.INVALID_INPUT
				state = _zscore_state(status)
				value = _float(row[2]) if state is FeatureValueState.READY else None
				if state is FeatureValueState.READY and value is None:
					state, value = FeatureValueState.INVALID, None
				observations.append(FeatureChannelObservation(channel, session, ticker_id, value, state, revision))
			evidence.append(FeatureReadinessEvidence(channel, session, FeatureReadinessState.READY if actual_available else FeatureReadinessState.UNVERIFIED, revision, "Prepared z-score completion metadata verified" if actual_available else "Prepared z-score inputs were not all available"))
		return observations, evidence


__all__ = ["FeatureChannelReadError", "MySQLFeatureChannelRepository"]
