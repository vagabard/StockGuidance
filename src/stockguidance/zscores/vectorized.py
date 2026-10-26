from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	import numpy as np

from stockguidance.zscores.calculation import _calculate_ticker, _dependency_issue, _observation_issue
from stockguidance.zscores.inputs import ZScoreInput
from stockguidance.zscores.results import ZScoreResult, ZScoreRow, ZScoreStatus
from stockguidance.zscores.specification import ZScoreSpecification


BATCH_SIZE = 1000


def _compensated_sum(values: np.ndarray) -> np.ndarray:
	import numpy as np

	total = np.zeros(values.shape[1], dtype=np.float64)
	correction = np.zeros_like(total)
	for current in values:
		partial = total + current
		correction += np.where(np.abs(total) >= np.abs(current), (total - partial) + current, (current - partial) + total)
		total = partial
	return total + correction


def calculate_zscores_vectorized(specification: ZScoreSpecification, input_data: ZScoreInput) -> ZScoreResult:
	import numpy as np

	if not isinstance(specification, ZScoreSpecification) or not isinstance(input_data, ZScoreInput):
		raise ValueError("Vectorized calculation requires z-score specification and input contracts")
	if specification.family is not input_data.family:
		raise ValueError("z-score specification and input families must match")
	if any(observation.quality for series in input_data.tickers for observation in series.observations):
		from stockguidance.zscores.calculation import calculate_zscores
		return calculate_zscores(specification, input_data)
	rows = []
	sequence_indexes = {session.sequence: index for index, session in enumerate(input_data.sessions)}
	dependencies = input_data.dependency_sequences
	minimum_dependency = min(dependencies) if dependencies else None
	for offset in range(0, len(input_data.tickers), BATCH_SIZE):
		batch = input_data.tickers[offset:offset + BATCH_SIZE]
		values = np.zeros((len(input_data.sessions), len(batch)), dtype=np.float64)
		issues = np.zeros(values.shape, dtype=np.uint8)
		for ticker_index, series in enumerate(batch):
			for session_index, observation in enumerate(series.observations):
				issue, numeric, _ = _observation_issue(observation, specification.family)
				issues[session_index, ticker_index] = 0 if issue is None else int(issue)
				if numeric is not None:
					values[session_index, ticker_index] = numeric
		for window in specification.windows:
			for session in input_data.scored_sessions or ():
				end = sequence_indexes[session.sequence] + 1
				start = end - window
				first_sequence = session.sequence - window + 1
				dependency_issue = _dependency_issue(specification.family, first_sequence, session.sequence, dependencies, minimum_dependency)
				selected = values[max(0, start):end]
				status = issues[max(0, start):end].max(axis=0)
				if start < 0:
					status = np.maximum(status, int(ZScoreStatus.INSUFFICIENT_HISTORY))
				if dependency_issue is not None:
					status = np.maximum(status, int(dependency_issue))
				contiguous = start >= 0 and all(input_data.sessions[index].sequence == first_sequence + index - start for index in range(start, end))
				with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
					mean = _compensated_sum(selected) / window
					centered = selected - mean
					variance = _compensated_sum(centered * centered) / window
					scores = (selected[-1] - mean) / np.sqrt(variance)
					quantized = scores.astype(np.float32)
				for ticker_index, series in enumerate(batch):
					current_status = ZScoreStatus(int(status[ticker_index]))
					score = None
					if not contiguous:
						fallback = _calculate_ticker(specification, input_data, series)
						rows.append(next(row for row in fallback if row.window == window and row.session == session))
						continue
					if current_status is ZScoreStatus.READY:
						if np.all(selected[:, ticker_index] == selected[0, ticker_index]):
							rows.append(ZScoreRow(window, session, series.ticker_id, None, ZScoreStatus.ZERO_VARIANCE, "zero_variance"))
							continue
						current_variance = float(variance[ticker_index])
						scale = float(np.max(np.abs(selected[:, ticker_index])))
						if not math.isfinite(current_variance) or math.sqrt(current_variance) <= 1e6 * math.ulp(scale) or not np.isfinite(quantized[ticker_index]):
							fallback = _calculate_ticker(specification, input_data, series)
							rows.append(next(row for row in fallback if row.window == window and row.session == session))
							continue
						score = float(quantized[ticker_index])
					rows.append(ZScoreRow(window, session, series.ticker_id, score, current_status, current_status.name.lower()))
	return ZScoreResult(specification, input_data.sessions, input_data.scored_sessions or (), input_data.dependency_sessions or (), tuple(rows))
