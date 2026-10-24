from datetime import date, datetime, timedelta, timezone
import math

import pytest

from stockguidance.calendar.sessions import TradingSession
from stockguidance.zscores import (
	InputState,
	ZScoreFamily,
	ZScoreInput,
	ZScoreObservation,
	ZScoreSpecification,
	ZScoreStatus,
	calculate_zscores,
	quantize_float32,
)


def sessions(count: int, *, sequences: tuple[int, ...] | None = None, dates: tuple[date, ...] | None = None) -> tuple[TradingSession, ...]:
	selected_sequences = sequences or tuple(range(count))
	selected_dates = dates or tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(count))
	return tuple(
		TradingSession(
			"fixture-calendar",
			session_date,
			sequence,
			datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc),
			datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=6),
		)
		for sequence, session_date in zip(selected_sequences, selected_dates)
	)


def price_input(values: dict[int, tuple[object | ZScoreObservation, ...]], input_sessions: tuple[TradingSession, ...], **kwargs: object) -> ZScoreInput:
	return ZScoreInput.from_values(ZScoreFamily.PRICE, input_sessions, values, **kwargs)


def delta_input(values: dict[int, tuple[object | ZScoreObservation, ...]], input_sessions: tuple[TradingSession, ...], **kwargs: object) -> ZScoreInput:
	return ZScoreInput.from_values(ZScoreFamily.PRICE_DELTA, input_sessions, values, **kwargs)


def specification(family: ZScoreFamily = ZScoreFamily.PRICE, *windows: int) -> ZScoreSpecification:
	return ZScoreSpecification(family, windows or (3,), delta_definition="close_ratio_to_prior_minus_one" if family is ZScoreFamily.PRICE_DELTA else "not_applicable")


def reference_score(values: tuple[float, ...]) -> float:
	mean = math.fsum(values) / len(values)
	variance = math.fsum((value - mean) ** 2 for value in values) / len(values)
	return quantize_float32((values[-1] - mean) / math.sqrt(variance))


def test_price_last_score_uses_inclusive_population_window() -> None:
	input_sessions = sessions(3)
	result = calculate_zscores(specification(), price_input({7: (1.0, 2.0, 3.0)}, input_sessions))

	row = result.row(3, input_sessions[-1], 7)

	assert row.status is ZScoreStatus.READY
	assert row.score == pytest.approx(math.sqrt(1.5), rel=1e-7)


def test_tickers_are_normalized_independently_and_windows_are_independent() -> None:
	input_sessions = sessions(4)
	result = calculate_zscores(
		specification(ZScoreFamily.PRICE, 2, 3),
		price_input({3: (1.0, 2.0, 3.0, 4.0), 9: (10.0, 20.0, 30.0, 40.0)}, input_sessions),
	)

	assert result.row(2, input_sessions[-1], 3).score == pytest.approx(1.0)
	assert result.row(3, input_sessions[-1], 3).score == pytest.approx(reference_score((2.0, 3.0, 4.0)))
	assert result.row(3, input_sessions[-1], 9).score == pytest.approx(reference_score((20.0, 30.0, 40.0)))


def test_boundary_history_and_n2_statuses_are_explicit() -> None:
	input_sessions = sessions(2)
	result = calculate_zscores(specification(ZScoreFamily.PRICE, 2), price_input({7: (1.0, 2.0)}, input_sessions))

	assert result.row(2, input_sessions[0], 7).status is ZScoreStatus.INSUFFICIENT_HISTORY
	assert result.row(2, input_sessions[1], 7).score == pytest.approx(1.0)


def test_constant_window_is_zero_variance_with_null_score() -> None:
	input_sessions = sessions(3)
	row = calculate_zscores(specification(), price_input({7: (4.0, 4.0, 4.0)}, input_sessions)).row(3, input_sessions[-1], 7)

	assert row.status is ZScoreStatus.ZERO_VARIANCE
	assert row.score is None


def test_constant_rolling_window_remains_zero_variance_after_prior_changes() -> None:
	input_sessions = sessions(5)
	row = calculate_zscores(specification(ZScoreFamily.PRICE, 3), price_input({7: (1.0, 2.0, 4.0, 4.0, 4.0)}, input_sessions)).row(3, input_sessions[-1], 7)

	assert row.status is ZScoreStatus.ZERO_VARIANCE
	assert row.score is None


def test_holiday_is_not_an_interior_gap_but_missing_exchange_session_is() -> None:
	holiday_sessions = sessions(2, dates=(date(2024, 7, 3), date(2024, 7, 5)))
	holiday_result = calculate_zscores(specification(ZScoreFamily.PRICE, 2), price_input({7: (1.0, 2.0)}, holiday_sessions))

	missing_sessions = sessions(3, sequences=(0, 1, 3))
	missing_result = calculate_zscores(specification(ZScoreFamily.PRICE, 3), price_input({7: (1.0, 2.0, 3.0)}, missing_sessions))

	assert holiday_result.row(2, holiday_sessions[-1], 7).status is ZScoreStatus.READY
	assert missing_result.row(3, missing_sessions[-1], 7).status is ZScoreStatus.MISSING_INPUT


def test_invalid_nonfinite_unverified_and_missing_inputs_follow_precedence() -> None:
	input_sessions = sessions(3)
	invalid = ZScoreObservation.invalid(float("nan"))
	unverified = ZScoreObservation.not_yet_available(2.0)
	missing = ZScoreObservation.missing()
	result = calculate_zscores(specification(), price_input({7: (invalid, unverified, missing)}, input_sessions))

	assert result.row(3, input_sessions[-1], 7).status is ZScoreStatus.INVALID_INPUT
	assert result.row(3, input_sessions[-1], 7).score is None

	result = calculate_zscores(specification(), price_input({7: (1.0, unverified, 3.0)}, input_sessions))
	assert result.row(3, input_sessions[-1], 7).status is ZScoreStatus.UNVERIFIED_INPUT

	result = calculate_zscores(specification(), price_input({7: (1.0, missing, 3.0)}, input_sessions))
	assert result.row(3, input_sessions[-1], 7).status is ZScoreStatus.MISSING_INPUT


def test_partial_boundary_does_not_hide_higher_priority_invalid_input() -> None:
	input_sessions = sessions(2)
	result = calculate_zscores(specification(ZScoreFamily.PRICE, 3), price_input({7: (ZScoreObservation.invalid(1.0), 2.0)}, input_sessions))

	assert result.row(3, input_sessions[1], 7).status is ZScoreStatus.INVALID_INPUT


def test_ready_values_are_quantized_to_float32() -> None:
	input_sessions = sessions(3)
	row = calculate_zscores(specification(), price_input({7: (1.0, 2.0, 4.0)}, input_sessions)).row(3, input_sessions[-1], 7)

	assert row.score == quantize_float32((4.0 - (1.0 + 2.0 + 4.0) / 3.0) / math.sqrt(((1.0 - 7.0 / 3.0) ** 2 + (2.0 - 7.0 / 3.0) ** 2 + (4.0 - 7.0 / 3.0) ** 2) / 3.0))


def test_large_nearly_constant_values_match_independent_two_pass_reference() -> None:
	values = (1_000_000_000_000.0, 1_000_000_000_001.0, 1_000_000_000_003.0, 1_000_000_000_006.0)
	input_sessions = sessions(len(values))
	result = calculate_zscores(specification(ZScoreFamily.PRICE, 3), price_input({7: values}, input_sessions))

	assert result.row(3, input_sessions[-1], 7).score == reference_score(values[-3:])


def test_later_input_changes_do_not_change_earlier_scores() -> None:
	input_sessions = sessions(4)
	first = calculate_zscores(specification(), price_input({7: (1.0, 2.0, 3.0, 4.0)}, input_sessions))
	second = calculate_zscores(specification(), price_input({7: (1.0, 2.0, 3.0, 400.0)}, input_sessions))

	assert first.row(3, input_sessions[2], 7) == second.row(3, input_sessions[2], 7)


def test_delta_uses_explicit_values_and_declares_n_plus_one_close_dependencies() -> None:
	input_sessions = sessions(3)
	result = calculate_zscores(
		specification(ZScoreFamily.PRICE_DELTA, 2),
		delta_input({7: (0.0, 0.1, 0.2)}, input_sessions, dependency_sessions=input_sessions),
	)

	assert specification(ZScoreFamily.PRICE_DELTA, 2).required_close_count(2) == 3
	assert result.row(2, input_sessions[-1], 7).status is ZScoreStatus.READY
	assert result.dependency_sessions == input_sessions


def test_delta_without_prior_close_dependency_remains_boundary_unavailable() -> None:
	input_sessions = sessions(3)
	result = calculate_zscores(
		specification(ZScoreFamily.PRICE_DELTA, 2),
		delta_input({7: (0.0, 0.1, 0.2)}, input_sessions, dependency_sessions=input_sessions[1:]),
	)

	assert result.row(2, input_sessions[-1], 7).status is ZScoreStatus.INSUFFICIENT_HISTORY


def test_invalid_specifications_and_family_mismatches_fail() -> None:
	with pytest.raises(ValueError):
		ZScoreSpecification(ZScoreFamily.PRICE, (1,))
	with pytest.raises(ValueError):
		ZScoreSpecification(ZScoreFamily.PRICE, (2, 2))
	with pytest.raises(ValueError):
		ZScoreSpecification(ZScoreFamily.PRICE, (2,), precision="float64")

	input_sessions = sessions(2)
	with pytest.raises(ValueError):
		calculate_zscores(specification(ZScoreFamily.PRICE_DELTA, 2), price_input({7: (1.0, 2.0)}, input_sessions))


def test_no_window_can_publish_nonfinite_or_zero_filled_values() -> None:
	input_sessions = sessions(3)
	result = calculate_zscores(specification(), price_input({7: (1.0, float("inf"), 3.0)}, input_sessions))

	assert result.row(3, input_sessions[-1], 7).status is ZScoreStatus.INVALID_INPUT
	assert all(row.score is None or math.isfinite(row.score) for row in result.rows)
	assert InputState.MISSING.value == "missing"
