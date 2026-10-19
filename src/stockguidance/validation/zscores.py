from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import math
import struct
from typing import Any

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.source import SourceConfig
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.database.repositories.zscores import LoadedZScoreInputs, ZScoreInputQuery, ZScoreRepository
from stockguidance.market_data.coverage import CoveragePlan
from stockguidance.market_data.identity import source_key
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding
from stockguidance.zscores.inputs import InputState, ZScoreInput, ZScoreObservation
from stockguidance.zscores.results import ZScoreRow, ZScoreStatus
from stockguidance.zscores.specification import ZScoreFamily, ZScoreSpecification, DELTA_DEFINITION
from stockguidance.zscores.storage import ZScoreDependencySnapshot, ZScoreSeriesDefinition, VerifiedZScoreRead, rows_content_id


@dataclass(frozen=True)
class ZScoreAuditRecord:
	family: ZScoreFamily
	window: int
	session_date: date
	ticker_id: int
	expected_score: float | None
	expected_status: ZScoreStatus
	stored_score: float | None
	stored_status: ZScoreStatus | None
	detail: str

	def payload(self) -> dict[str, object]:
		return {
			"family": self.family.value,
			"window": self.window,
			"session_date": self.session_date.isoformat(),
			"ticker_id": self.ticker_id,
			"expected_score": self.expected_score,
			"expected_status": int(self.expected_status),
			"stored_score": self.stored_score,
			"stored_status": None if self.stored_status is None else int(self.stored_status),
			"detail": self.detail,
		}


def _float_value(value: object | None, family: ZScoreFamily) -> float | None:
	if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
		return None
	try:
		converted = float(value)
	except (TypeError, ValueError, OverflowError):
		return None
	if not math.isfinite(converted):
		return None
	if family is ZScoreFamily.PRICE and converted <= 0.0:
		return None
	if family is ZScoreFamily.PRICE_DELTA and converted <= -1.0:
		return None
	return converted


def _observation_issue(observation: ZScoreObservation, family: ZScoreFamily) -> tuple[ZScoreStatus | None, float | None, str]:
	if observation.state is InputState.INVALID:
		return ZScoreStatus.INVALID_INPUT, None, "invalid_input"
	if observation.state in (InputState.UNVERIFIED, InputState.NOT_YET_AVAILABLE) or not observation.actual_available:
		return ZScoreStatus.UNVERIFIED_INPUT, None, "input_not_verified_or_not_available"
	if observation.state is InputState.INSUFFICIENT_HISTORY:
		return ZScoreStatus.INSUFFICIENT_HISTORY, None, "insufficient_history"
	if observation.state is InputState.MISSING or observation.value is None:
		return ZScoreStatus.MISSING_INPUT, None, "missing_input"
	value = _float_value(observation.value, family)
	if value is None:
		return ZScoreStatus.INVALID_INPUT, None, "invalid_input"
	return None, value, "ready"


def _quantize_float32(value: float) -> float:
	try:
		quantized = struct.unpack("!f", struct.pack("!f", value))[0]
	except (OverflowError, struct.error) as error:
		raise ValueError("Expected z-score is not representable as float32") from error
	if not math.isfinite(quantized):
		raise ValueError("Expected z-score is non-finite after float32 quantization")
	return float(quantized)


def _expected_rows(loaded: LoadedZScoreInputs, specification: ZScoreSpecification) -> tuple[ZScoreRow, ...]:
	input_data = loaded.input
	if not isinstance(input_data, ZScoreInput):
		raise ValueError("Invalid loaded z-score input")
	rows: list[ZScoreRow] = []
	sequence_to_session = {session.sequence: session for session in input_data.sessions}
	sequence_to_index = {session.sequence: index for index, session in enumerate(input_data.sessions)}
	scored_sequences = {session.sequence for session in input_data.scored_sessions or ()}
	for series in input_data.tickers:
		observations = {session.sequence: observation for session, observation in zip(input_data.sessions, series.observations, strict=True)}
		for window in specification.windows:
			for sequence in sorted(scored_sequences):
				session = sequence_to_session[sequence]
				index = sequence_to_index[sequence]
				first_index = index - window + 1
				statuses: list[ZScoreStatus] = []
				values: list[float] = []
				issue_reasons: list[str] = []
				if first_index < 0:
					statuses.append(ZScoreStatus.INSUFFICIENT_HISTORY)
					issue_reasons.append("insufficient_history")
				for candidate in input_data.sessions[max(0, first_index):index + 1]:
					status, value, reason = _observation_issue(observations[candidate.sequence], specification.family)
					if status is not None:
						statuses.append(status)
						issue_reasons.append(reason)
					elif value is not None:
						values.append(value)
				if statuses:
					status = max(statuses, key=lambda item: {ZScoreStatus.INSUFFICIENT_HISTORY: 1, ZScoreStatus.MISSING_INPUT: 2, ZScoreStatus.UNVERIFIED_INPUT: 3, ZScoreStatus.INVALID_INPUT: 4}[item])
					rows.append(ZScoreRow(window, session, series.ticker_id, None, status, ",".join(sorted(set(issue_reasons)))))
					continue
				if len(values) != window:
					rows.append(ZScoreRow(window, session, series.ticker_id, None, ZScoreStatus.MISSING_INPUT, "missing_input"))
					continue
				mean = math.fsum(values) / window
				variance = math.fsum((value - mean) ** 2 for value in values) / window
				if variance == 0.0:
					rows.append(ZScoreRow(window, session, series.ticker_id, None, ZScoreStatus.ZERO_VARIANCE, "zero_variance"))
					continue
				current = values[-1]
				score = _quantize_float32((current - mean) / math.sqrt(variance))
				rows.append(ZScoreRow(window, session, series.ticker_id, score, ZScoreStatus.READY, "ready"))
	return tuple(rows)


def _effective_sessions(plan: CoveragePlan, publication: DailyPublicationPolicy, cutoff: datetime) -> tuple[Any, ...]:
	values = []
	for item in plan.sessions:
		expected_at = publication.expected_at(item.session)
		if item.session.session_date >= plan.boundary.history_start and item.session.closes_at <= cutoff and expected_at is not None and expected_at <= cutoff:
			values.append(item.session)
	return tuple(values)


def _definition(source: SourceConfig, calendar: SessionCalendar, family: ZScoreFamily, specification: ZScoreSpecification, window: int) -> ZScoreSeriesDefinition:
	return ZScoreSeriesDefinition(source_key(source.identity), calendar.calendar_id, family, window, specification.calculation_version, specification.precision, specification.delta_definition)


def _finding_category(status: ZScoreStatus) -> FindingCategory:
	if status is ZScoreStatus.INVALID_INPUT:
		return FindingCategory.INVALID
	if status is ZScoreStatus.UNVERIFIED_INPUT:
		return FindingCategory.UNVERIFIED
	return FindingCategory.MISSING


def _detail(family: ZScoreFamily, window: int, expected: ZScoreRow, stored: ZScoreRow | None, reason: str) -> str:
	stored_status = None if stored is None else int(stored.status)
	stored_score = None if stored is None else stored.score
	return f"{family.value} window {window} expected status={int(expected.status)} score={expected.score!r}; stored status={stored_status} score={stored_score!r}; {reason}"


def _read_many(repository: ZScoreRepository, requests: tuple[tuple[ZScoreSeriesDefinition, ZScoreDependencySnapshot], ...]) -> tuple[dict[tuple[str, int], VerifiedZScoreRead], dict[tuple[str, int], Exception]]:
	if not requests:
		return {}, {}
	batch_reader = getattr(repository, "read_verified_many", None)
	audit_reader = getattr(repository, "read_for_audit_many", None)
	if callable(audit_reader):
		try:
			batch_reads = tuple(audit_reader(requests))
			if len(batch_reads) != len(requests):
				raise ValueError("Z-score repository returned an incomplete audit batch")
			return {(read.definition.series_key, read.metadata.date_id): read for read in batch_reads}, {}
		except Exception:
			pass
	if callable(batch_reader):
		try:
			batch_reads = tuple(batch_reader(requests))
			if len(batch_reads) != len(requests):
				raise ValueError("Z-score repository returned an incomplete batch")
			return {(read.definition.series_key, read.metadata.date_id): read for read in batch_reads}, {}
		except Exception:
			pass
	reads: dict[tuple[str, int], VerifiedZScoreRead] = {}
	errors: dict[tuple[str, int], Exception] = {}
	for definition, dependency in requests:
		key = (definition.series_key, dependency.date_id)
		try:
			reads[key] = repository.read_verified(definition, dependency)
		except Exception as error:
			errors[key] = error
	return reads, errors


def _error_finding(error: Exception, family: ZScoreFamily, window: int, session_date: date, ticker_ids: tuple[int, ...]) -> ValidationFinding:
	message = str(error).lower()
	if error.__class__.__name__ == "ZScoreDependencyChanged":
		category = FindingCategory.UNVERIFIED
		detail = "Stored z-score dependencies are stale or unavailable at the validation cutoff; rerun the explicit z-score updater"
	elif "completion metadata is missing" in message or "series definition is missing" in message:
		category = FindingCategory.MISSING
		detail = "Z-score series or completion metadata is missing or duplicated"
	elif "invalid z-score" in message or "conflicting z-score" in message or "metadata does not match" in message:
		category = FindingCategory.INVALID
		detail = "Z-score metadata or numerical rows are invalid"
	else:
		category = FindingCategory.FAILED
		detail = "Z-score schema or storage inspection failed"
	return ValidationFinding(category, ValidationCheck.ZSCORE, f"{family.value} window {window}: {detail}; rerun the explicit z-score updater after review", session_date, tuple(f"ticker_id={ticker_id}" for ticker_id in ticker_ids))


def audit_zscores(
	source: SourceConfig,
	plan: CoveragePlan,
	calendar: SessionCalendar,
	publication: DailyPublicationPolicy,
	cutoff: datetime,
	windows: tuple[int, ...],
	repository: ZScoreRepository,
) -> tuple[tuple[ValidationFinding, ...], tuple[ZScoreAuditRecord, ...]]:
	effective = _effective_sessions(plan, publication, cutoff)
	try:
		repository.inspect_schema(windows)
	except AttributeError:
		pass
	except Exception:
		return (ValidationFinding(FindingCategory.FAILED, ValidationCheck.ZSCORE, "Selected z-score tables or metadata schema could not be inspected; review schema before rerunning the explicit z-score updater"),), ()
	if not effective:
		return (), ()
	try:
		ticker_ids = repository.list_ticker_ids()
		if not ticker_ids:
			raise ValueError("No ticker mappings are available")
	except Exception:
		return (ValidationFinding(FindingCategory.FAILED, ValidationCheck.ZSCORE, "Ticker mappings could not be read for z-score validation"),), ()
	first_required = calendar.shift(effective[0], -max(windows))
	input_start = max(first_required.session_date, plan.boundary.history_start)
	input_sessions = tuple(session for session in calendar.sessions(input_start, effective[-1].session_date) if session.closes_at <= cutoff)
	if not input_sessions or input_sessions[-1] != effective[-1]:
		return (ValidationFinding(FindingCategory.FAILED, ValidationCheck.ZSCORE, "Z-score warm-up scope could not be bounded through the requested effective end"),), ()
	query_scope = SessionQuery(plan.boundary, input_sessions, cutoff)
	loaded_by_family: dict[ZScoreFamily, LoadedZScoreInputs] = {}
	findings: list[ValidationFinding] = []
	for family in (ZScoreFamily.PRICE, ZScoreFamily.PRICE_DELTA):
		try:
			loaded_by_family[family] = repository.load_inputs(ZScoreInputQuery(query_scope, family, ticker_ids, effective))
		except Exception:
			findings.append(ValidationFinding(FindingCategory.FAILED, ValidationCheck.ZSCORE, f"{family.value} z-score inputs could not be independently inspected; review bounded local evidence"))
	if findings:
		return tuple(findings), ()
	requests: list[tuple[ZScoreSeriesDefinition, ZScoreDependencySnapshot]] = []
	expected_by_key: dict[tuple[ZScoreFamily, int, Any, int], ZScoreRow] = {}
	for family, loaded in loaded_by_family.items():
		specification = ZScoreSpecification(family, windows, delta_definition=DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
		for row in _expected_rows(loaded, specification):
			if row.session not in effective:
				continue
			expected_by_key[(family, row.window, row.session, row.ticker_id)] = row
		for window in windows:
			definition = _definition(source, calendar, family, specification, window)
			for dependency in loaded.dependencies:
				if dependency.session in effective:
					requests.append((definition, dependency))
	records: list[ZScoreAuditRecord] = []
	reads, errors = _read_many(repository, tuple(requests))
	for family, loaded in loaded_by_family.items():
		specification = ZScoreSpecification(family, windows, delta_definition=DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
		for window in windows:
			definition = _definition(source, calendar, family, specification, window)
			for dependency in loaded.dependencies:
				if dependency.session not in effective:
					continue
				key = (definition.series_key, dependency.date_id)
				expected = {ticker_id: expected_by_key[(family, window, dependency.session, ticker_id)] for ticker_id in ticker_ids}
				if key in errors:
					findings.append(_error_finding(errors[key], family, window, dependency.session.session_date, ticker_ids))
					continue
				read = reads.get(key)
				if read is None:
					findings.append(ValidationFinding(FindingCategory.FAILED, ValidationCheck.ZSCORE, f"{family.value} window {window}: repository returned no verified result; rerun the explicit z-score updater", dependency.session.session_date, tuple(f"ticker_id={ticker_id}" for ticker_id in ticker_ids)))
					continue
				if read.metadata.actual_input_available != dependency.actual_input_available or read.metadata.completed_at > cutoff:
					findings.append(ValidationFinding(FindingCategory.UNVERIFIED, ValidationCheck.ZSCORE, f"{family.value} window {window}: completion metadata does not match actual input availability or is after the validation cutoff", dependency.session.session_date, tuple(f"ticker_id={ticker_id}" for ticker_id in ticker_ids)))
				if read.metadata.content_digest != rows_content_id(read.rows):
					findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.ZSCORE, f"{family.value} window {window}: completion content digest does not match stored rows", dependency.session.session_date, tuple(f"ticker_id={ticker_id}" for ticker_id in ticker_ids)))
				stored = {row.ticker_id: row for row in read.rows}
				for ticker_id, expected_row in expected.items():
					stored_row = stored.get(ticker_id)
					if expected_row.status in {ZScoreStatus.MISSING_INPUT, ZScoreStatus.UNVERIFIED_INPUT, ZScoreStatus.INVALID_INPUT}:
						findings.append(ValidationFinding(_finding_category(expected_row.status), ValidationCheck.ZSCORE, _detail(family, window, expected_row, stored_row, "verified input is unavailable"), expected_row.session.session_date, (f"ticker_id={ticker_id}",)))
					if stored_row is None:
						records.append(ZScoreAuditRecord(family, window, expected_row.session.session_date, ticker_id, expected_row.score, expected_row.status, None, None, "stored row is missing"))
						findings.append(ValidationFinding(FindingCategory.MISSING, ValidationCheck.ZSCORE, _detail(family, window, expected_row, None, "stored row is missing; rerun the explicit z-score updater"), expected_row.session.session_date, (f"ticker_id={ticker_id}",)))
						continue
					status_mismatch = stored_row.status is not expected_row.status
					score_mismatch = expected_row.status is ZScoreStatus.READY and (stored_row.score is None or not math.isclose(stored_row.score, expected_row.score or 0.0, rel_tol=1e-5, abs_tol=1e-6))
					if status_mismatch or score_mismatch:
						reason = "stored status differs" if status_mismatch else "stored score differs beyond float32-aware tolerance"
						findings.append(ValidationFinding(FindingCategory.INVALID, ValidationCheck.ZSCORE, _detail(family, window, expected_row, stored_row, reason), expected_row.session.session_date, (f"ticker_id={ticker_id}",)))
						records.append(ZScoreAuditRecord(family, window, expected_row.session.session_date, ticker_id, expected_row.score, expected_row.status, stored_row.score, stored_row.status, reason))
	return tuple(findings), tuple(records)
