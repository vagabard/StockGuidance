from datetime import timedelta

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.calendar.clock import SystemClock
from stockguidance.config.source import PriceAdjustment
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.recommendations.jobs import RecommendationConfiguration
from stockguidance.database.repositories.eligibility import VerifiedEligibilityRepository
from stockguidance.database.repositories.mysql_market_data import MySQLMarketDataRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.database.repositories.signals import SignalRepository
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.configuration import load_database_configuration
from stockguidance.market_data.history import UnknownHistoryBoundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.market_data.opening_refresh import OpeningRefresh
from stockguidance.market_data.providers.configured_openings import ConfiguredOpeningProvider
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.models.policy import ModelRefreshPolicy, ModelSelection
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.serialization import decimal_text, load_portfolio
from stockguidance.portfolio.state import initial_portfolio
from stockguidance.recommendations.configuration import ArtifactKind, RecommendationJob, RetrainingPlan
from stockguidance.recommendations.options import RecommendationOptions
from stockguidance.targets.specification import TargetSpec
from stockguidance.workflows.recommend import recommend
from stockguidance.workflows.recommendation_dependencies import RecommendationServices
from stockguidance.workflows.recommendation_result import RecommendationRunResult


def assemble_and_recommend(configuration: RecommendationConfiguration, arguments: RecommendationOptions) -> RecommendationRunResult:
	boundary = JsonHistoryStore(configuration.source.data_root / "history").load(configuration.source.identity)
	if boundary is None:
		raise UnknownHistoryBoundary("Recommendation requires evidenced, persisted source history; use update_market_data first")
	if (configuration.retraining is not None) != (arguments.policy is ModelRefreshPolicy.RETRAIN):
		raise ValueError("Model policy and training configuration differ")
	calendar = ExchangeCalendar(boundary.calendar_id, min(boundary.history_start, configuration.anchor_date) - timedelta(days=14), max(configuration.end, configuration.entry_date) + timedelta(days=14))
	policy = ModelRefreshPolicy(arguments.policy)
	if policy is ModelRefreshPolicy.CURRENT and arguments.artifact_id is None:
		raise ValueError("Current requires an explicit artifact identity")
	reference = None if arguments.artifact_id is None else ArtifactReference(arguments.artifact, arguments.artifact_id)
	retraining = None
	if configuration.retraining is not None:
		settings = configuration.retraining
		decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - settings.decision_lead) for session in calendar.sessions(settings.start, settings.end))
		retraining = RetrainingPlan(decisions, settings.training, arguments.artifact)
	job = RecommendationJob(configuration.source, configuration.start or boundary.history_start, configuration.end, configuration.entry_date, configuration.features, TargetSpec(), ModelSelection(policy, None if reference is None else reference.artifact_id), AllocationSpec(configuration.allocation_policy, calendar.session(configuration.anchor_date)), configuration.publication, configuration.corrections, configuration.eligibility, configuration.top_k, reference, ArtifactKind(arguments.artifact_kind), retraining, configuration.availability_policy)
	clock = SystemClock()
	portfolio = None
	if arguments.portfolio_state is not None:
		portfolio = load_portfolio(arguments.portfolio_state, job.allocation, calendar)
	elif arguments.capital is not None:
		if job.entry_date != job.allocation.anchor_session.session_date:
			raise ValueError("Initial cash requires entry at the anchor; supply actual portfolio state for later sessions")
		portfolio = initial_portfolio(job.allocation, decimal_text(arguments.capital), clock.now())
	connect = MySQLConnectionFactory()
	table_names = load_database_configuration(job.source.database_config).load_table_names()
	validation = MySQLValidationRepository(job.source, connect, table_names)
	closes = VerifiedCloseRepository(job.source, validation)
	opening_store = OpeningStore(job.source.data_root)
	market_data = MySQLMarketDataRepository(job.source, connect, table_names)
	market_data.legacy_read_only = job.source.identity.adjustment is PriceAdjustment.ADJUSTED
	services = RecommendationServices(ConfiguredPolygonProvider(arguments.max_retries), market_data, validation, SignalRepository(closes, opening_store), VerifiedEligibilityRepository(closes), OpeningRefresh(job.source, opening_store, ConfiguredOpeningProvider(arguments.max_retries), closes))
	return recommend(job, boundary, calendar, services, clock, portfolio=portfolio, execution=ExecutionPolicy(decimal_text(arguments.fee_per_fill)))
