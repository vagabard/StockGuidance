from datetime import date, datetime, timedelta, timezone
import random

import pytest

from stockguidance.calendar.sessions import TradingSession
from stockguidance.zscores.calculation import calculate_zscores
from stockguidance.zscores.inputs import ZScoreInput, ZScoreObservation
from stockguidance.zscores.specification import DELTA_DEFINITION, ZScoreFamily, ZScoreSpecification
from stockguidance.zscores.vectorized import calculate_zscores_vectorized


def _sessions(count=61):
	return tuple(TradingSession("fixture", date(2024, 1, 1) + timedelta(days=index), index,
		datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=index),
		datetime(2024, 1, 1, 6, tzinfo=timezone.utc) + timedelta(days=index)) for index in range(count))


def _assert_parity(specification, inputs):
	expected = calculate_zscores(specification, inputs)
	actual = calculate_zscores_vectorized(specification, inputs)
	key = lambda row: (row.ticker_id, row.window, row.session.sequence)
	assert len(actual.rows) == len(expected.rows)
	for result, reference in zip(sorted(actual.rows, key=key), sorted(expected.rows, key=key)):
		assert key(result) == key(reference)
		assert result.status == reference.status
		if reference.score is None:
			assert result.score is None
		else:
			assert result.score == pytest.approx(reference.score, abs=1e-6, rel=1e-5)


@pytest.mark.parametrize("family", tuple(ZScoreFamily))
def test_vectorized_dense_random_batches_and_tails(family):
	generator = random.Random(9187)
	sessions = _sessions()
	values = {ticker: tuple(generator.uniform(10, 200) if family is ZScoreFamily.PRICE else generator.uniform(-0.8, 2) for _ in sessions) for ticker in range(1, 1004)}
	inputs = ZScoreInput.from_values(family, sessions, values, scored_sessions=sessions[-2:])
	specification = ZScoreSpecification(family, (20, 60), delta_definition=DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
	_assert_parity(specification, inputs)


@pytest.mark.parametrize("family", tuple(ZScoreFamily))
def test_vectorized_statuses_extreme_offsets_and_constant_windows(family):
	sessions = _sessions()
	values = {
		1: (2.0,) * 61,
		2: tuple(1e12 + index * 0.001 for index in range(61)),
		3: tuple(1.0 + index * 1e-15 for index in range(61)),
		4: tuple(10.0 + index for index in range(61)),
	}
	for ticker, issue in enumerate((ZScoreObservation.missing(), ZScoreObservation.unverified(3), ZScoreObservation.invalid(3), ZScoreObservation.not_yet_available(3), ZScoreObservation.insufficient_history(), float("nan"), float("inf"), True, -2), start=5):
		values[ticker] = (2.0,) * 59 + (issue, 3.0)
	inputs = ZScoreInput.from_values(family, sessions, values, scored_sessions=(sessions[-1],))
	specification = ZScoreSpecification(family, (20, 60), delta_definition=DELTA_DEFINITION if family is ZScoreFamily.PRICE_DELTA else "not_applicable")
	_assert_parity(specification, inputs)


def test_vectorized_short_history_and_calendar_gaps():
	sessions = _sessions(8)
	sessions = sessions[:3] + sessions[4:]
	inputs = ZScoreInput.from_values(ZScoreFamily.PRICE, sessions, {9: tuple(2 + index for index in range(len(sessions)))})
	_assert_parity(ZScoreSpecification(ZScoreFamily.PRICE, (2, 6, 20)), inputs)
