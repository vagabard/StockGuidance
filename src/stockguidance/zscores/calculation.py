from collections import deque
from dataclasses import replace
from collections.abc import Iterable
from decimal import Decimal
import math
import struct
from typing import cast

from stockguidance.zscores.inputs import InputState, TickerSeries, ZScoreInput, ZScoreObservation
from stockguidance.zscores.results import ZScoreResult, ZScoreRow, ZScoreStatus
from stockguidance.zscores.specification import ZScoreFamily, ZScoreSpecification
from stockguidance.market_data.quality import QualityFlag, QualityPolicy


_STATUS_PRIORITY = {
	ZScoreStatus.INSUFFICIENT_HISTORY: 1,
	ZScoreStatus.MISSING_INPUT: 2,
	ZScoreStatus.UNVERIFIED_INPUT: 3,
	ZScoreStatus.INVALID_INPUT: 4,
}


def quantize_float32(value: float) -> float:
	if not math.isfinite(value):
		raise ValueError("z-score result must be finite before float32 quantization")
	try:
		quantized = cast(float, struct.unpack("!f", struct.pack("!f", value))[0])
	except (OverflowError, struct.error) as error:
		raise ValueError("z-score result is not representable as float32") from error
	if not math.isfinite(quantized):
		raise ValueError("z-score result is non-finite after float32 quantization")
	return quantized


class _CompensatedSum:
	def __init__(self) -> None:
		self.total = 0.0
		self.correction = 0.0

	def add(self, value: float) -> None:
		partial = self.total + value
		if abs(self.total) >= abs(value):
			self.correction += (self.total - partial) + value
		else:
			self.correction += (value - partial) + self.total
		self.total = partial

	@property
	def value(self) -> float:
		return self.total + self.correction


class _RollingMoments:
	def __init__(self, capacity: int) -> None:
		self.capacity = capacity
		self._values: deque[tuple[int, float]] = deque()
		self._anchor: float | None = None
		self._sum = _CompensatedSum()
		self._squared_sum = _CompensatedSum()
		self._direct_m2: float | None = None

	def add(self, sequence: int, value: float) -> None:
		if self._anchor is None:
			self._anchor = value
		self._append(sequence, value)

	def _append(self, sequence: int, value: float) -> None:
		assert self._anchor is not None
		delta = value - self._anchor
		self._values.append((sequence, value))
		self._sum.add(delta)
		self._squared_sum.add(delta * delta)
		self._direct_m2 = None
		self._check_finite()

	def remove_before(self, first_sequence: int) -> None:
		while self._values and self._values[0][0] < first_sequence:
			_, value = self._values.popleft()
			assert self._anchor is not None
			delta = value - self._anchor
			self._sum.add(-delta)
			self._squared_sum.add(-(delta * delta))
			self._direct_m2 = None
		if not self._values:
			self._anchor = None
			self._sum = _CompensatedSum()
			self._squared_sum = _CompensatedSum()
			self._direct_m2 = 0.0
		elif self._requires_rebase():
			self._recalculate()
		self._check_finite()

	def _requires_rebase(self) -> bool:
		if not self._values:
			return False
		count = len(self._values)
		sum_delta = self._sum.value
		squared_sum = self._squared_sum.value
		m2 = squared_sum - (sum_delta * sum_delta) / count
		if not math.isfinite(m2):
			return True
		scale = max(abs(squared_sum), abs((sum_delta * sum_delta) / count))
		return m2 < 0.0 or (scale > 0.0 and m2 <= 32.0 * math.ulp(scale))

	def _recalculate(self) -> None:
		values = tuple(value for _, value in self._values)
		mean = math.fsum(values) / len(values)
		self._anchor = mean
		self._sum = _CompensatedSum()
		self._squared_sum = _CompensatedSum()
		for value in values:
			delta = value - mean
			self._sum.add(delta)
			self._squared_sum.add(delta * delta)
		self._direct_m2 = math.fsum((value - mean) ** 2 for value in values)

	def _check_finite(self) -> None:
		if not math.isfinite(self._sum.value) or not math.isfinite(self._squared_sum.value):
			raise ValueError("z-score rolling moments became non-finite")

	@property
	def count(self) -> int:
		return len(self._values)

	@property
	def variance(self) -> float:
		if self.count == 0:
			raise ValueError("cannot calculate variance of an empty window")
		first_value = self._values[0][1]
		if all(value == first_value for _, value in self._values):
			return 0.0
		if self._direct_m2 is not None:
			m2 = self._direct_m2
		else:
			sum_delta = self._sum.value
			m2 = self._squared_sum.value - (sum_delta * sum_delta) / self.count
		if not math.isfinite(m2) or m2 < 0.0:
			raise ValueError("z-score variance is not finite and nonnegative")
		return m2 / self.count

	@property
	def mean(self) -> float:
		if self._anchor is None:
			raise ValueError("cannot calculate mean of an empty window")
		return self._anchor + self._sum.value / self.count


def _numeric_value(value: object | None, family: ZScoreFamily) -> float | None:
	if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
		return None
	try:
		converted = float(value)
	except (OverflowError, ValueError):
		return None
	if not math.isfinite(converted):
		return None
	if family is ZScoreFamily.PRICE and converted <= 0.0:
		return None
	if family is ZScoreFamily.PRICE_DELTA and converted <= -1.0:
		return None
	return converted


def _observation_issue(observation: ZScoreObservation, family: ZScoreFamily, policy: QualityPolicy = QualityPolicy()) -> tuple[ZScoreStatus | None, float | None, str]:
	if not policy.accepts(observation.quality):
		return ZScoreStatus.INSUFFICIENT_HISTORY, None, "quality_history_reset"
	if observation.quality & QualityFlag.FUTURE_LEAKAGE and not policy.retrospective:
		return ZScoreStatus.UNVERIFIED_INPUT, None, "future_evidence_unavailable"
	if observation.state is InputState.INVALID:
		return ZScoreStatus.INVALID_INPUT, None, "invalid_input"
	if observation.state in (InputState.UNVERIFIED, InputState.NOT_YET_AVAILABLE) or not observation.actual_available:
		return ZScoreStatus.UNVERIFIED_INPUT, None, "input_not_verified_or_not_available"
	if observation.state is InputState.INSUFFICIENT_HISTORY:
		return ZScoreStatus.INSUFFICIENT_HISTORY, None, "insufficient_history"
	if observation.state is InputState.MISSING or observation.value is None:
		return ZScoreStatus.MISSING_INPUT, None, "missing_input"
	value = _numeric_value(observation.value, family)
	if value is None:
		return ZScoreStatus.INVALID_INPUT, None, "invalid_input"
	return None, value, "ready"


def _higher_priority(first: ZScoreStatus | None, second: ZScoreStatus | None) -> ZScoreStatus | None:
	if first is None:
		return second
	if second is None:
		return first
	return first if _STATUS_PRIORITY[first] >= _STATUS_PRIORITY[second] else second


def _dependency_issue(
	family: ZScoreFamily,
	window_start: int,
	window_end: int,
	dependency_sequences: frozenset[int],
	minimum_dependency: int | None,
) -> ZScoreStatus | None:
	first_required = window_start - (1 if family is ZScoreFamily.PRICE_DELTA else 0)
	required = range(first_required, window_end + 1)
	for sequence in required:
		if sequence in dependency_sequences:
			continue
		if minimum_dependency is None or sequence < minimum_dependency:
			return ZScoreStatus.INSUFFICIENT_HISTORY
		return ZScoreStatus.MISSING_INPUT
	return None


def _calculate_ticker(
	specification: ZScoreSpecification,
	input_data: ZScoreInput,
	series: TickerSeries,
	) -> tuple[ZScoreRow, ...]:
	if not input_data.sessions:
		return ()
	sequence_to_observation = {
		session.sequence: observation
		for session, observation in zip(input_data.sessions, series.observations)
	}
	sequence_to_session = input_data.session_by_sequence
	minimum_sequence = input_data.sessions[0].sequence
	maximum_sequence = input_data.sessions[-1].sequence
	dependency_sequences = input_data.dependency_sequences
	minimum_dependency = min(dependency_sequences) if dependency_sequences else None
	scored_sequences = {session.sequence for session in input_data.scored_sessions or ()}
	rows: list[ZScoreRow] = []
	missing_observation = ZScoreObservation.missing()
	for window in specification.windows:
		moments = _RollingMoments(window)
		issues: deque[tuple[int, ZScoreStatus | None]] = deque()
		issue_counts = {status: 0 for status in _STATUS_PRIORITY}
		for sequence in range(minimum_sequence, maximum_sequence + 1):
			observation = sequence_to_observation.get(sequence, missing_observation)
			issue, numeric, _ = _observation_issue(observation, specification.family, specification.quality_policy)
			issues.append((sequence, issue))
			if issue is not None:
				issue_counts[issue] += 1
			elif numeric is not None:
				moments.add(sequence, numeric)
			moments.remove_before(sequence - window + 1)
			while issues and issues[0][0] < sequence - window + 1:
				_, expired_issue = issues.popleft()
				if expired_issue is not None:
					issue_counts[expired_issue] -= 1
			if sequence not in scored_sequences:
				continue
			session = sequence_to_session[sequence]
			window_start = sequence - window + 1
			status: ZScoreStatus | None = None
			for candidate in (ZScoreStatus.INVALID_INPUT, ZScoreStatus.UNVERIFIED_INPUT, ZScoreStatus.MISSING_INPUT, ZScoreStatus.INSUFFICIENT_HISTORY):
				if issue_counts[candidate]:
					status = candidate
					break
			if sequence - minimum_sequence + 1 < window:
				status = _higher_priority(status, ZScoreStatus.INSUFFICIENT_HISTORY)
			status = _higher_priority(status, _dependency_issue(specification.family, window_start, sequence, dependency_sequences, minimum_dependency))
			if status is not None:
				rows.append(ZScoreRow(window, session, series.ticker_id, None, status, status.name.lower()))
				continue
			if moments.count != window:
				raise ValueError("ready z-score window did not contain exactly its declared inputs")
			variance = moments.variance
			if variance == 0.0:
				rows.append(ZScoreRow(window, session, series.ticker_id, None, ZScoreStatus.ZERO_VARIANCE, "zero_variance"))
				continue
			standard_deviation = math.sqrt(variance)
			if numeric is None:
				raise ValueError("ready z-score window has no current numeric input")
			score = (numeric - moments.mean) / standard_deviation
			quantized = quantize_float32(score)
			rows.append(ZScoreRow(window, session, series.ticker_id, quantized, ZScoreStatus.READY, "ready"))
	flagged = [(session.sequence, observation) for session, observation in zip(input_data.sessions, series.observations) if observation.quality]
	if flagged:
		for index, row in enumerate(rows):
			flags = QualityFlag.CLEAN
			gap = 0
			provenance: set[str] = set()
			for sequence, observation in flagged:
				if row.session.sequence - row.window < sequence <= row.session.sequence:
					flags |= observation.quality
					gap = max(gap, observation.gap_length)
					provenance.update(observation.provenance)
			rows[index] = replace(row, quality=flags, gap_length=gap, provenance=tuple(sorted(provenance)))
	return tuple(rows)


def calculate_zscores(specification: ZScoreSpecification, input_data: ZScoreInput) -> ZScoreResult:
	if not isinstance(specification, ZScoreSpecification):
		raise ValueError("z-score calculation requires a ZScoreSpecification")
	if not isinstance(input_data, ZScoreInput):
		raise ValueError("z-score calculation requires a ZScoreInput")
	if specification.family is not input_data.family:
		raise ValueError("z-score specification and input families must match")
	rows: list[ZScoreRow] = []
	for series in input_data.tickers:
		rows.extend(_calculate_ticker(specification, input_data, series))
	return ZScoreResult(
		specification,
		input_data.sessions,
		input_data.scored_sessions or (),
		input_data.dependency_sessions or (),
		tuple(rows),
	)


compute_zscores = calculate_zscores
