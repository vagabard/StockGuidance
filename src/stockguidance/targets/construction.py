from dataclasses import dataclass
import math

from stockguidance.calendar.sessions import TradingSession, require_next_session
from stockguidance.database.repositories.closes import CloseData
from stockguidance.market_data.closes import CloseIssue, valid_close
from stockguidance.targets.specification import TargetKind, TargetSpec, TargetSpecV2, TargetSpecification
from stockguidance.market_data.quality import QualityFlag, combine_quality


def close_to_close_return(entry_close: float, exit_close: float, specification: TargetSpec) -> float:
	if not isinstance(specification, TargetSpec):
		raise ValueError("Close target requires an explicit target specification")
	result = valid_close(exit_close) / valid_close(entry_close) - 1
	if not math.isfinite(result):
		raise ValueError("Close target is not finite")
	return result


@dataclass(frozen=True)
class TargetValues:
	raw: tuple[float | None, ...]
	transformed: tuple[float | None, ...]
	issues: tuple[CloseIssue, ...] = ()
	quality: QualityFlag = QualityFlag.CLEAN
	gap_length: int = 0
	provenance: tuple[str, ...] = ()
	statistics: "TargetStatistics | None" = None

	def __post_init__(self) -> None:
		if not isinstance(self.raw, tuple) or not self.raw or not isinstance(self.transformed, tuple) or len(self.raw) != len(self.transformed) or not isinstance(self.issues, tuple):
			raise ValueError("Targets require matching immutable raw/transformed layouts")
		if any(not isinstance(issue, CloseIssue) for issue in self.issues):
			raise ValueError("Invalid target issues")
		if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < -1) for value in self.raw):
			raise ValueError("Invalid raw close return")
		if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)) for value in self.transformed):
			raise ValueError("Invalid transformed target")
		if self.statistics is not None and not isinstance(self.statistics, TargetStatistics):
			raise ValueError("Invalid target statistics")

	@property
	def ready(self) -> bool:
		return not self.issues and all(value is not None for value in self.raw) and all(value is not None for value in self.transformed)


@dataclass(frozen=True)
class TargetStatistics:
	"""Outcome-only moments used to explain a transformed label."""

	kind: TargetKind
	baseline_sessions: tuple[TradingSession, ...]
	means: tuple[float, ...]
	variances: tuple[float, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.kind, TargetKind) or not isinstance(self.baseline_sessions, tuple) or len(self.means) != len(self.variances) or not self.means:
			raise ValueError("Invalid target statistics")
		if any(not isinstance(item, TradingSession) for item in self.baseline_sessions) or any(not math.isfinite(value) or value < 0 for value in (*self.means, *self.variances)):
			raise ValueError("Target statistics must be finite population moments")

	def to_mapping(self) -> dict[str, object]:
		return {
			"kind": self.kind.value,
			"baseline_sessions": [session.session_date.isoformat() for session in self.baseline_sessions],
			"means": list(self.means), "variances": list(self.variances),
		}


def target_dependency_sessions(specification: TargetSpecification, entry: TradingSession, exit_session: TradingSession, available_sessions: tuple[TradingSession, ...] | None = None) -> tuple[TradingSession, ...]:
	"""Return exact close sessions needed for raw labels and target baselines."""

	require_next_session(entry, exit_session)
	if not isinstance(specification, TargetSpec):
		raise ValueError("Target dependency planning requires an explicit target specification")
	by_sequence = {} if available_sessions is None else {session.sequence: session for session in available_sessions}
	offsets = (*specification.baseline_offsets, 0, 1)
	result: list[TradingSession] = []
	for offset in sorted(set(offsets)):
		sequence = entry.sequence + offset
		if available_sessions is None:
			if offset == 1:
				result.append(exit_session)
			elif offset == 0:
				result.append(entry)
			else:
				# A direct target construction call must supply the baseline rows in
				# its CloseData scope; no arbitrary session can be synthesized here.
				raise ValueError("Per-ticker target dependencies require the baseline sessions in CloseData")
		else:
			if sequence not in by_sequence:
				raise ValueError("Target data omits a required baseline session")
			result.append(by_sequence[sequence])
	return tuple(result)


def _population_moments(values: tuple[float, ...]) -> tuple[float, float]:
	mean = sum(values) / len(values)
	variance = sum((value - mean) ** 2 for value in values) / len(values)
	return mean, variance


def construct_targets(specification: TargetSpecification, ticker_ids: tuple[int, ...], entry: TradingSession, exit_session: TradingSession, data: CloseData) -> TargetValues:
	require_next_session(entry, exit_session)
	by_key = {(item.timing.session, item.ticker_id): item for item in data.observations}
	needed_sessions = target_dependency_sessions(specification, entry, exit_session, data.query.scope.sessions)
	needed = set(needed_sessions)
	by_sequence = {session.sequence: session for session in needed_sessions}
	issues = tuple(issue for issue in data.issues if issue.session in needed and issue.ticker_id in ticker_ids)
	values: list[float | None] = []
	for ticker_id in ticker_ids:
		entry_price, exit_price = by_key.get((entry, ticker_id)), by_key.get((exit_session, ticker_id))
		if entry_price is None or exit_price is None:
			if not any(issue.ticker_id == ticker_id for issue in issues):
				raise ValueError("Target result omits a required session/ticker key")
			values.append(None)
		else:
			values.append(close_to_close_return(entry_price.close, exit_price.close, specification))
	raw = tuple(values)
	transformed: tuple[float | None, ...] = raw
	statistics: TargetStatistics | None = None
	if isinstance(specification, TargetSpecV2) and specification.kind is TargetKind.RETURN_ZSCORE_PER_TICKER:
		assert specification.rolling_window_sessions is not None
		baseline_sessions = tuple(session for session in needed_sessions if session.sequence < entry.sequence)
		if len(baseline_sessions) != specification.rolling_window_sessions:
			raise ValueError("Per-ticker target baseline does not contain the requested window")
		means: list[float] = []
		variances: list[float] = []
		transformed_values: list[float | None] = []
		for ticker_id, raw_value in zip(ticker_ids, raw):
			baseline: list[float] = []
			baseline_issue = False
			for session in baseline_sessions:
				left = by_key.get((session, ticker_id))
				right_session = by_sequence.get(session.sequence + 1)
				right = None if right_session is None else by_key.get((right_session, ticker_id))
				if left is None or right is None:
					baseline_issue = True
					continue
				baseline.append(close_to_close_return(left.close, right.close, specification))
			if baseline_issue or len(baseline) != specification.rolling_window_sessions or any(not math.isfinite(value) for value in baseline):
				transformed_values.append(None)
				means.append(0.0)
				variances.append(0.0)
				continue
			mean, variance = _population_moments(tuple(baseline))
			means.append(mean)
			variances.append(variance)
			transformed_values.append(None if raw_value is None else 0.0 if variance == 0 else (raw_value - mean) / math.sqrt(variance))
		statistics = TargetStatistics(specification.kind, baseline_sessions, tuple(means), tuple(variances))
		transformed = tuple(transformed_values)
	elif isinstance(specification, TargetSpecV2) and specification.kind is TargetKind.RETURN_ZSCORE_CROSS_SECTIONAL:
		if any(value is None for value in raw) or issues:
			transformed = tuple(None for _ in raw)
		else:
			complete = tuple(value for value in raw if value is not None)
			mean, variance = _population_moments(complete)
			transformed = tuple(0.0 if variance == 0 else (value - mean) / math.sqrt(variance) for value in complete)
			statistics = TargetStatistics(specification.kind, (entry,), (mean,), (variance,))
	quality, gap_length, provenance = combine_quality([*(item for item in data.observations if item.timing.session in needed and item.ticker_id in ticker_ids), *issues])
	return TargetValues(raw, transformed, issues, quality, gap_length, provenance, statistics)
