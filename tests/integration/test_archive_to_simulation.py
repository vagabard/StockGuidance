from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from support.ingestion import FakeDatabase, FakeProvider, FrozenClock
from support.samples import FixtureCloseRepository
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.update_schema import initialize_update_schema
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.discovery import DiscoveryRequest, discover_coverage
from stockguidance.market_data.history import HistoryProvenance, establish_history_boundary
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.providers.contracts import CapabilityState, ProviderCapability
from stockguidance.market_data.closes import CloseIssue, CloseObservation, PriceState
from stockguidance.portfolio.marks import ClosingMark
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec
from stockguidance.recommendations.eligibility import EligibilityPolicy, LiquidityObservation
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.prices import ExecutionFrame
from stockguidance.simulation.score_source import FixedModelScores, HistoricalSignals
from stockguidance.targets.specification import TargetSpec
from stockguidance.training.configuration import DeviceChoice, TrainingConfig
from stockguidance.models.specification import ModelSpec
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding
from stockguidance.validation.repair import RepairAction, plan_repairs
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history
from stockguidance.workflows.update_market_data import UpdateSettings, update_market_data


@dataclass
class AcceptanceExecution:
	source: SourceIdentity
	prices: dict[tuple[object, int], Decimal]
	audit_cutoff: datetime

	def read(self, session, ticker_ids):
		quotes, issues = [], []
		for ticker_id in ticker_ids:
			price = self.prices.get((session, ticker_id))
			if price is None:
				issues.append(CloseIssue(ticker_id, session, PriceState.EXPECTED_ABSENCE, "Acceptance execution price is missing"))
			else:
				quotes.append(ClosingMark(ticker_id, session, price, f"acceptance-execution-{session.session_date}-{ticker_id}"))
		return ExecutionFrame(self.source, session, ticker_ids, tuple(quotes), tuple(issues))


class AcceptanceLiquidity:
	def __init__(self, repository):
		self.repository = repository

	def read(self, query):
		data = self.repository.read(query)
		return tuple(LiquidityObservation(item.ticker_id, 500, item.timing, item.revision) for item in data.observations)


@pytest.fixture
def cross_stage_fixture(tmp_path):
	calendar = ExchangeCalendar("XNYS", date(2010, 1, 1), date(2022, 12, 31))
	clock = FrozenClock(datetime(2021, 1, 15, 23, tzinfo=timezone.utc))
	identity = SourceIdentity("polygon", "grouped-daily", "us-stocks", "fixture-full-universe", PriceAdjustment.ADJUSTED)
	source = SourceConfig(identity, tmp_path / "data", tmp_path / "dbinfo.json", "fixture-credential-reference")
	boundary = establish_history_boundary(identity, calendar, date(2010, 1, 4), clock, provenance=HistoryProvenance.EXPLICIT_BOUNDARY, evidence="Stage 10 fixture boundary")
	publication = DailyPublicationPolicy(timedelta(minutes=15), "Stage 10 fixture publication")
	corrections = CorrectionPolicy(timedelta(days=30), timedelta(days=30), timedelta(days=365), "Stage 10 fixture corrections")
	database = FakeDatabase(tmp_path / "database.sqlite")
	initialize_update_schema(source, database)
	repository = MySQLMarketDataRepository(source, database)
	provider = FakeProvider()
	return calendar, clock, source, boundary, publication, corrections, database, repository, provider


def _update(calendar, clock, source, boundary, publication, corrections, repository, provider, start, end, discovery=None):
	settings = UpdateSettings(start, end, publication, corrections, discovery=discovery)
	return update_market_data(source, settings, boundary, calendar, provider, repository, clock)


def _timing(calendar, entry_date):
	entry = calendar.session(entry_date)
	return DecisionTiming(calendar.shift(entry, -1), entry, calendar.shift(entry, 1), entry.closes_at - timedelta(minutes=30))


def test_retained_2010_archive_and_2021_capability_bound_feed_later_simulation(cross_stage_fixture, tmp_path):
	calendar, clock, source, boundary, publication, corrections, database, repository, provider = cross_stage_fixture
	old_start, old_end = date(2010, 1, 4), date(2010, 1, 8)
	current_start, current_end = date(2021, 1, 4), date(2021, 1, 8)
	old_sessions = calendar.sessions(old_start, old_end)
	assert _update(calendar, clock, source, boundary, publication, corrections, repository, provider, old_start, old_end).successful
	old_manifests = {session.session_date: ManifestStore(source.data_root).path_for(source.identity, session).read_bytes() for session in old_sessions}
	old_rows = set(database.sql("SELECT transaction_date FROM transaction_dates"))

	capability = ProviderCapability(
		source.identity,
		CapabilityState.VERIFIED,
		clock.now(),
		"Stage 10 fixture account capability",
		"Provider access begins with the 2021 fixture sessions",
		current_start,
		current_end,
		timedelta(days=2),
		current_start,
	)
	discovery = discover_coverage(
		source.identity,
		calendar,
		clock,
		publication,
		request=DiscoveryRequest(source.identity, current_start, current_end),
		local_inventory=ManifestStore(source.data_root).inventory(source.identity, calendar),
		provider_capability=capability,
		history_boundary=boundary,
	)
	provider.errors.add(date(2021, 1, 6))
	provider.calls.clear()
	failed = _update(calendar, clock, source, boundary, publication, corrections, repository, provider, current_start, current_end, discovery)
	assert not failed.successful
	assert provider.calls and all(session.session_date >= current_start for source_value, session in provider.calls)

	provider.errors.clear()
	provider.calls.clear()
	repaired = _update(calendar, clock, source, boundary, publication, corrections, repository, provider, current_start, current_end, discovery)
	assert repaired.successful
	assert date(2021, 1, 6) in {session.session_date for source_value, session in provider.calls}
	assert all(session.session_date >= current_start for source_value, session in provider.calls)
	assert old_rows.issubset(set(database.sql("SELECT transaction_date FROM transaction_dates")))
	assert old_manifests == {session_date: ManifestStore(source.data_root).path_for(source.identity, calendar.session(session_date)).read_bytes() for session_date in old_manifests}

	wide_inventory = ManifestStore(source.data_root).inventory(source.identity, calendar, start=old_start, end=current_end)
	wide_discovery = discover_coverage(
		source.identity,
		calendar,
		clock,
		publication,
		request=DiscoveryRequest(source.identity, old_start, current_end),
		local_inventory=wide_inventory,
		provider_capability=capability,
		history_boundary=boundary,
	)
	old_gap = date(2010, 1, 11)
	assert old_gap in wide_discovery.retained.holes
	plan = plan_repairs(
		source.identity,
		old_start,
		current_end,
		clock.now(),
		(ValidationFinding(FindingCategory.MISSING, ValidationCheck.SESSION, "Retained archive gap is outside current provider access", old_gap),),
		fetchable_start=current_start,
		fetchable_end=current_end,
		capability_resolved=True,
	)
	assert plan.items[0].action is RepairAction.BLOCKED_OUT_OF_ACCESS

	training_start, training_end = date(2021, 1, 6), date(2021, 1, 7)
	training_sessions = calendar.sessions(date(2021, 1, 4), date(2021, 1, 13))
	observations = []
	for index, session in enumerate(training_sessions):
		for ticker_id, base in ((17, Decimal("100")), (3, Decimal("50"))):
			close = base + Decimal(index * (2 if ticker_id == 17 else 1))
			observations.append(CloseObservation(ticker_id, float(close), ObservationTiming(session, session.closes_at, session.closes_at + timedelta(minutes=15)), f"acceptance-close-{session.session_date}-{ticker_id}"))
	close_repository = FixtureCloseRepository(observations)
	features = FeatureSpec((17, 3), 1)
	target = TargetSpec()
	training_decisions = tuple(_timing(calendar, entry_date) for entry_date in (training_start, training_end))
	training = train_from_history(
		HistoryTrainingRequest(
			training_decisions,
			calendar.session(date(2021, 1, 8)).closes_at + timedelta(minutes=16),
			features,
			target,
			TrainingConfig(model=ModelSpec(hidden_widths=()), epochs=1, batch_size=1, device=DeviceChoice.CPU, seed=42),
			tmp_path / "acceptance.sgmodel",
		),
		boundary,
		calendar,
		publication,
		close_repository,
	)
	assert training.artifact.path.exists() and training.artifact.artifact_id

	liquidity = AcceptanceLiquidity(close_repository)
	signals = HistoricalSignals(boundary, calendar, publication, close_repository, liquidity)
	simulation_start, simulation_end = date(2021, 1, 11), date(2021, 1, 13)
	request = SimulationRequest(
		source.identity,
		features,
		target,
		AllocationSpec(AllocationPolicy.ALTERNATE_SESSIONS, calendar.session(simulation_start)),
		simulation_start,
		simulation_end,
		Decimal("1000.00"),
		eligibility=EligibilityPolicy(200),
		top_k=1,
	)
	prices = AcceptanceExecution(
		source.identity,
		{(session, ticker_id): Decimal(str(observation.close)) for observation in observations for session in (observation.timing.session,) for ticker_id in (observation.ticker_id,)},
		calendar.session(simulation_end).closes_at + timedelta(minutes=16),
	)
	result = simulate(request, boundary, calendar, FixedModelScores(training.artifact, signals), prices)
	assert result.successful and result.complete and not result.training_runs
	assert all(session.sequence < forecast.sample.timing.entry_session.sequence for forecast in result.forecasts for session in forecast.sample.features.sessions)
	assert all(forecast.sample.target_state.value == "not_requested" for forecast in result.forecasts)
	assert all(event.at == event.session.closes_at for event in result.events if event.kind.value in {"buy", "sale"})
