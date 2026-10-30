from datetime import date, datetime, timedelta, timezone

import pytest

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.database.repositories.closes import CloseData, CloseQuery
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.closes import CloseObservation
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.targets.construction import construct_targets
from stockguidance.targets.specification import TargetKind, TargetSpecV2


def _close_data(sessions: tuple[TradingSession, ...], values: dict[tuple[int, int], float]) -> CloseData:
	source = SourceIdentity("fixture", "offline", "stocks", "target-fixture", PriceAdjustment.ADJUSTED)
	boundary = HistoryBoundary(source, "target-calendar", sessions[0].session_date, HistoryProvenance.EXPLICIT_BOUNDARY, "target fixture", sessions[0].closes_at + timedelta(hours=1))
	query = CloseQuery(SessionQuery(boundary, sessions, sessions[-1].closes_at + timedelta(minutes=30)), (1, 2))
	observations = tuple(CloseObservation(ticker_id, close, ObservationTiming(session, session.closes_at, session.closes_at + timedelta(minutes=15)), f"{session.sequence}-{ticker_id}") for (session_sequence, ticker_id), close in sorted(values.items()) for session in sessions if session.sequence == session_sequence)
	return CloseData(query, observations, ())


def _sessions(count: int) -> tuple[TradingSession, ...]:
	return tuple(TradingSession("target-calendar", date(2024, 1, 2) + timedelta(days=index), index, datetime(2024, 1, 2, 14, tzinfo=timezone.utc) + timedelta(days=index), datetime(2024, 1, 2, 21, tzinfo=timezone.utc) + timedelta(days=index)) for index in range(count))


def test_cross_sectional_target_uses_full_universe_and_zeroes_ties() -> None:
	sessions = _sessions(2)
	data = _close_data(sessions, {(0, 1): 100.0, (0, 2): 200.0, (1, 1): 110.0, (1, 2): 180.0})
	target = construct_targets(TargetSpecV2(kind=TargetKind.RETURN_ZSCORE_CROSS_SECTIONAL), (1, 2), sessions[0], sessions[1], data)
	assert target.raw == pytest.approx((.1, -.1))
	assert target.transformed == pytest.approx((1.0, -1.0))
	assert target.statistics is not None and target.statistics.variances == pytest.approx((.01,))

	tied = _close_data(sessions, {(0, 1): 100.0, (0, 2): 200.0, (1, 1): 110.0, (1, 2): 220.0})
	assert construct_targets(TargetSpecV2(kind=TargetKind.RETURN_ZSCORE_CROSS_SECTIONAL), (1, 2), sessions[0], sessions[1], tied).transformed == (0.0, 0.0)
	assert construct_targets(TargetSpecV2(kind=TargetKind.RETURN_ZSCORE_CROSS_SECTIONAL), (1,), sessions[0], sessions[1], data).transformed == (0.0,)


def test_per_ticker_target_uses_previous_mature_returns() -> None:
	sessions = _sessions(22)
	values: dict[tuple[int, int], float] = {}
	for session in sessions:
		values[(session.sequence, 1)] = 100.0 * 1.01 ** session.sequence
		values[(session.sequence, 2)] = 200.0 * 1.02 ** session.sequence
	target = construct_targets(TargetSpecV2(kind=TargetKind.RETURN_ZSCORE_PER_TICKER, rolling_window_sessions=20), (1, 2), sessions[20], sessions[21], _close_data(sessions, values))
	assert target.ready
	assert target.raw == pytest.approx((1.01 - 1, 1.02 - 1))
	assert target.statistics is not None
	assert target.transformed[0] == pytest.approx((target.raw[0] - target.statistics.means[0]) / target.statistics.variances[0] ** .5)
	assert target.transformed[1] == pytest.approx(0.0, abs=1e-10)
	assert target.statistics is not None and len(target.statistics.baseline_sessions) == 20
