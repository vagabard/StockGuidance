from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.models.policy import ModelRefreshPolicy, ModelSelection
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec
from stockguidance.recommendations.request import RecommendationRequest
from stockguidance.targets.specification import TargetSpec


@pytest.fixture
def sessions() -> tuple[TradingSession, ...]:
	return tuple(
		TradingSession(
			calendar_id="fixture-calendar",
			session_date=date(2026, 10, day),
			sequence=sequence,
			opens_at=datetime(2026, 10, day, 13, 30, tzinfo=timezone.utc),
			closes_at=datetime(2026, 10, day, 20, tzinfo=timezone.utc),
		)
		for sequence, day in enumerate((9, 12, 13, 14))
	)


@pytest.fixture
def timing(sessions: tuple[TradingSession, ...]) -> DecisionTiming:
	return DecisionTiming(
		previous_session=sessions[1], entry_session=sessions[2], exit_session=sessions[3],
		decision_cutoff=sessions[2].closes_at - timedelta(minutes=30),
	)


@pytest.fixture
def source() -> SourceIdentity:
	return SourceIdentity(
		provider="fixture", endpoint="grouped-daily", market="us-stocks",
		universe="all-source-stocks", adjustment=PriceAdjustment.ADJUSTED,
	)


@pytest.fixture
def recommendation_request(source: SourceIdentity, timing: DecisionTiming, tmp_path: Path) -> RecommendationRequest:
	return RecommendationRequest(
		source=SourceConfig(source, tmp_path / "data", tmp_path / "database.id", "provider-key-reference"),
		coverage=MarketDataRequest(source, date(2026, 10, 9), date(2026, 10, 13), timing.decision_cutoff),
		timing=timing,
		features=FeatureSpec(ticker_ids=(7, 3), lookback_sessions=2),
		target=TargetSpec(),
		model=ModelSelection(ModelRefreshPolicy.CURRENT, "fixture-artifact"),
		allocation=AllocationSpec(AllocationPolicy.ALTERNATE_SESSIONS, timing.previous_session),
	)
