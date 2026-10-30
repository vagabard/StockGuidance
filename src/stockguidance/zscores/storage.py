from dataclasses import dataclass
from datetime import datetime
import math
from typing import Final

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime, require_positive_int, require_text
from stockguidance.market_data.identity import content_id
from stockguidance.zscores.results import ZScoreResult, ZScoreRow, ZScoreStatus
from stockguidance.zscores.specification import ZScoreFamily, ZScoreSpecification
from stockguidance.market_data.quality import QualityPolicy


METADATA_SCHEMA_VERSION: Final = 1
MISSING_VALUE_POLICY: Final = "null_status_v1"


def _identity(value: str) -> str:

	if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
		raise ValueError("Invalid z-score identity")
	return value


@dataclass(frozen=True)
class ZScoreSeriesDefinition:
	"""The immutable definition addressed by one compact z-score table."""

	source_scope: str
	calendar_id: str
	family: ZScoreFamily
	window: int
	calculation_version: str
	precision: str
	delta_definition: str
	missing_value_policy: str = MISSING_VALUE_POLICY
	schema_version: int = METADATA_SCHEMA_VERSION

	def __post_init__(self) -> None:
		_identity(self.source_scope)
		require_text(self.calendar_id, "z-score calendar_id")
		if not isinstance(self.family, ZScoreFamily):
			raise ValueError("Invalid z-score family")
		if type(self.window) is not int or self.window < 2:
			raise ValueError("z-score window must be an integer >= 2")
		require_text(self.calculation_version, "z-score calculation version")
		if self.precision != "float32":
			raise ValueError("z-score precision must be float32")
		require_text(self.delta_definition, "z-score delta definition")
		require_text(self.missing_value_policy, "z-score missing-value policy")
		if type(self.schema_version) is not int or self.schema_version != METADATA_SCHEMA_VERSION:
			raise ValueError("Unsupported z-score metadata schema version")
		if self.family in (ZScoreFamily.PRICE, ZScoreFamily.VOLUME) and self.delta_definition != "not_applicable":
			raise ValueError("Price and volume z-scores must not declare a delta definition")
		if self.family is ZScoreFamily.PRICE_DELTA and self.delta_definition != "close_ratio_to_prior_minus_one":
			raise ValueError("Delta z-scores require the stored close-ratio definition")

	@property
	def series_key(self) -> str:
		return content_id(self.as_payload())

	def as_payload(self) -> dict[str, object]:
		return {
			"schema_version": self.schema_version,
			"source_scope": self.source_scope,
			"calendar_id": self.calendar_id,
			"family": self.family.value,
			"window": self.window,
			"calculation_version": self.calculation_version,
			"delta_definition": self.delta_definition,
			"precision": self.precision,
			"missing_value_policy": self.missing_value_policy,
		}

	@classmethod
	def from_specification(cls, source_scope: str, calendar_id: str, specification: ZScoreSpecification, window: int) -> "ZScoreSeriesDefinition":
		if window not in specification.windows:
			raise ValueError("Z-score window is not present in the specification")
		version = specification.calculation_version
		if specification.quality_policy != QualityPolicy():
			version += ":quality:" + specification.quality_policy.identity[:16]
		return cls(source_scope, calendar_id, specification.family, window, version, specification.precision, specification.delta_definition)


@dataclass(frozen=True)
class ZScoreDependencySnapshot:
	"""A digest of the exact evidence used for one published session."""

	series_key: str
	session: TradingSession
	date_id: int
	ticker_ids: tuple[int, ...]
	dependency_digest: str
	availability_digest: str
	actual_input_available: bool
	as_of: datetime
	input_sessions: tuple[TradingSession, ...] = ()

	def __post_init__(self) -> None:
		_identity(self.series_key)
		if not isinstance(self.session, TradingSession):
			raise ValueError("Invalid z-score dependency session")
		require_positive_int(self.date_id, "z-score dependency date_id")
		if not isinstance(self.ticker_ids, tuple) or not self.ticker_ids or len(set(self.ticker_ids)) != len(self.ticker_ids) or any(type(value) is not int or value < 1 for value in self.ticker_ids):
			raise ValueError("Invalid z-score dependency ticker IDs")
		_identity(self.dependency_digest)
		_identity(self.availability_digest)
		if type(self.actual_input_available) is not bool:
			raise ValueError("Invalid z-score input availability")
		require_aware_datetime(self.as_of, "z-score dependency cutoff")
		if not isinstance(self.input_sessions, tuple) or any(not isinstance(session, TradingSession) for session in self.input_sessions):
			raise ValueError("Invalid z-score dependency input sessions")


@dataclass(frozen=True)
class ZScoreSessionMetadata:
	series_key: str
	date_id: int
	dependency_digest: str
	availability_digest: str
	actual_input_available: bool
	completed_at: datetime
	row_count: int
	ready_count: int
	status_counts: tuple[tuple[ZScoreStatus, int], ...]
	content_digest: str

	def __post_init__(self) -> None:
		_identity(self.series_key)
		require_positive_int(self.date_id, "z-score session date_id")
		_identity(self.dependency_digest)
		_identity(self.availability_digest)
		if type(self.actual_input_available) is not bool:
			raise ValueError("Invalid z-score input availability")
		require_aware_datetime(self.completed_at, "z-score computation time")
		if type(self.row_count) is not int or self.row_count < 0 or type(self.ready_count) is not int or self.ready_count < 0 or self.ready_count > self.row_count:
			raise ValueError("Invalid z-score row counts")
		if not isinstance(self.status_counts, tuple) or any(not isinstance(status, ZScoreStatus) or type(count) is not int or count < 0 for status, count in self.status_counts):
			raise ValueError("Invalid z-score status counts")
		if len({status for status, _ in self.status_counts}) != len(self.status_counts):
			raise ValueError("Duplicate z-score status counts")
		if sum(count for _, count in self.status_counts) != self.row_count or dict(self.status_counts).get(ZScoreStatus.READY, 0) != self.ready_count:
			raise ValueError("Z-score status counts do not match row counts")
		_identity(self.content_digest)


@dataclass(frozen=True)
class VerifiedZScoreRead:
	definition: ZScoreSeriesDefinition
	metadata: ZScoreSessionMetadata
	rows: tuple[ZScoreRow, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.definition, ZScoreSeriesDefinition) or not isinstance(self.metadata, ZScoreSessionMetadata) or not isinstance(self.rows, tuple):
			raise ValueError("Invalid verified z-score read")
		if self.metadata.series_key != self.definition.series_key:
			raise ValueError("Z-score metadata series identity mismatch")
		if any(row.window != self.definition.window for row in self.rows):
			raise ValueError("Z-score row window mismatch")
		if self.metadata.row_count != len(self.rows):
			raise ValueError("Z-score metadata row count mismatch")
		if self.metadata.status_counts != status_counts(self.rows):
			raise ValueError("Z-score metadata status counts mismatch")


def status_counts(rows: tuple[ZScoreRow, ...]) -> tuple[tuple[ZScoreStatus, int], ...]:
	counts = {status: 0 for status in ZScoreStatus}
	for row in rows:
		counts[row.status] += 1
	return tuple((status, counts[status]) for status in ZScoreStatus)


def rows_content_id(rows: tuple[ZScoreRow, ...]) -> str:
	return content_id([
		(row.session.session_date.isoformat(), row.ticker_id, row.score, int(row.status), int(row.quality), row.gap_length, row.provenance) if row.quality else (row.session.session_date.isoformat(), row.ticker_id, row.score, int(row.status))
		for row in sorted(rows, key=lambda item: (item.session.sequence, item.ticker_id))
	])


def dependency_digest(payload: object) -> str:
	return content_id(payload)


def base_series_key(source_scope: str, calendar_id: str, family: ZScoreFamily) -> str:
	return content_id({"source_scope": source_scope, "calendar_id": calendar_id, "family": family.value})


def bind_dependency(dependency: ZScoreDependencySnapshot, definition: ZScoreSeriesDefinition) -> ZScoreDependencySnapshot:
	if dependency.series_key == definition.series_key:
		return dependency
	if dependency.series_key != base_series_key(definition.source_scope, definition.calendar_id, definition.family):
		raise ValueError("Z-score dependency belongs to a different series scope")
	return ZScoreDependencySnapshot(
		definition.series_key, dependency.session, dependency.date_id, dependency.ticker_ids,
		dependency.dependency_digest, dependency.availability_digest, dependency.actual_input_available, dependency.as_of, dependency.input_sessions,
	)


def session_metadata(
	definition: ZScoreSeriesDefinition,
	dependency: ZScoreDependencySnapshot,
	rows: tuple[ZScoreRow, ...],
	completed_at: datetime,
) -> ZScoreSessionMetadata:
	if dependency.series_key != definition.series_key or any(row.session != dependency.session for row in rows):
		raise ValueError("Z-score publication dependency does not match rows")
	counts = status_counts(rows)
	return ZScoreSessionMetadata(
		definition.series_key,
		dependency.date_id,
		dependency.dependency_digest,
		dependency.availability_digest,
		dependency.actual_input_available,
		completed_at,
		len(rows),
		next(count for status, count in counts if status is ZScoreStatus.READY),
		counts,
		rows_content_id(rows),
	)


def validate_ready_score(score: object) -> float:
	if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
		raise ValueError("Ready z-score value must be finite")
	return float(score)


def metadata_from_result(
	definition: ZScoreSeriesDefinition,
	result: ZScoreResult,
	dependency: ZScoreDependencySnapshot,
	completed_at: datetime,
) -> ZScoreSessionMetadata:
	rows = tuple(row for row in result.rows if row.window == definition.window and row.session == dependency.session)
	return session_metadata(definition, dependency, rows, completed_at)
