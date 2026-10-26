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
from stockguidance.database.simplified_runtime import source_from_database, target_schema_state
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.database.table_names import TableNames
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
from stockguidance.recommendations.artifacts import load_recommendation_artifact
from stockguidance.recommendations.options import RecommendationOptions
from stockguidance.targets.specification import TargetSpec
from stockguidance.workflows.recommend import recommend
from stockguidance.workflows.recommendation_dependencies import RecommendationServices
from stockguidance.workflows.recommendation_result import RecommendationRunResult
from stockguidance.workflows.simplified_readiness import SimplifiedReadinessWorkflow, boundary_for_scope, required_session_scope
from stockguidance.workflows.simplified_readiness import SimplifiedCloseRepository, SimplifiedEligibilityRepository
from stockguidance.workflows.failures import FailureCode, StageExecutionError, StageFailure, WorkflowStage


def _preflight_current_artifact(arguments: RecommendationOptions) -> None:
	if arguments.policy is not ModelRefreshPolicy.CURRENT:
		return
	if arguments.artifact_id is None:
		raise ValueError("Current requires an explicit artifact identity")
	try:
		load_recommendation_artifact(ArtifactReference(arguments.artifact, arguments.artifact_id), ArtifactKind(arguments.artifact_kind))
	except Exception:
		raise StageExecutionError(StageFailure(WorkflowStage.ARTIFACT, FailureCode.INCOMPATIBLE_ARTIFACT, "Explicit current artifact is unavailable or incompatible; legacy models require rebuilding")) from None


def _target_assemble(configuration: RecommendationConfiguration, arguments: RecommendationOptions, connect: WriteConnectionFactory, table_names: TableNames) -> RecommendationRunResult:
	data_root = configuration.source.data_root
	source, names = source_from_database(configuration.source.database_config, data_root, connect, table_names)
	if source.identity != configuration.source.identity:
		raise ValueError("Recommendation source differs from the database-owned configuration")
	if (configuration.retraining is not None) != (arguments.policy is ModelRefreshPolicy.RETRAIN):
		raise ValueError("Model policy and training configuration differ")
	clock = SystemClock()
	first = min(configuration.start or configuration.anchor_date, configuration.anchor_date)
	last = max(configuration.end, configuration.entry_date) + timedelta(days=30)
	calendar = ExchangeCalendar(source.calendar_id, first - timedelta(days=45), last)
	policy = ModelRefreshPolicy(arguments.policy)
	entry = calendar.session(configuration.entry_date)
	timing = DecisionTiming(calendar.shift(entry, -1), entry, calendar.shift(entry, 1), clock.now())
	training_decisions: tuple[DecisionTiming, ...] = ()
	retraining = None
	if configuration.retraining is not None:
		settings = configuration.retraining
		training_decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - settings.decision_lead) for session in calendar.sessions(settings.start, settings.end))
		retraining = RetrainingPlan(training_decisions, settings.training, arguments.artifact)
	scope = required_session_scope(calendar, tuple((*training_decisions, timing)), configuration.features, TargetSpec(), include_labels=bool(training_decisions))
	boundary = boundary_for_scope(source, calendar, scope, clock.now())
	if policy is ModelRefreshPolicy.CURRENT and arguments.artifact_id is None:
		raise ValueError("Current requires an explicit artifact identity")
	reference = None if arguments.artifact_id is None else ArtifactReference(arguments.artifact, arguments.artifact_id)
	job = RecommendationJob(source, scope.sessions[0].session_date, configuration.end, configuration.entry_date, configuration.features, TargetSpec(), ModelSelection(policy, None if reference is None else reference.artifact_id), AllocationSpec(configuration.allocation_policy, calendar.session(configuration.anchor_date)), configuration.publication, configuration.corrections, configuration.eligibility, configuration.top_k, reference, ArtifactKind(arguments.artifact_kind), retraining, configuration.availability_policy)
	portfolio = None
	if arguments.portfolio_state is not None:
		portfolio = load_portfolio(arguments.portfolio_state, job.allocation, calendar)
	elif arguments.capital is not None:
		if job.entry_date != job.allocation.anchor_session.session_date:
			raise ValueError("Initial cash requires an actual portfolio state for a later entry")
		portfolio = initial_portfolio(job.allocation, decimal_text(arguments.capital), clock.now())
	provider = ConfiguredPolygonProvider(arguments.max_retries)
	closes = SimplifiedCloseRepository(source, connect, names)
	readiness = SimplifiedReadinessWorkflow(source, connect, calendar, job.publication, provider, clock, table_names=names, staging_root=source.data_root / ".database-simplification" / "pending")
	opening_store = OpeningStore(source.data_root)
	services = RecommendationServices(provider, None, None, SignalRepository(closes, opening_store), SimplifiedEligibilityRepository(closes), OpeningRefresh(source, opening_store, ConfiguredOpeningProvider(arguments.max_retries), closes), readiness)
	return recommend(job, boundary, calendar, services, clock, portfolio=portfolio, execution=ExecutionPolicy(decimal_text(arguments.fee_per_fill)))


def assemble_and_recommend(configuration: RecommendationConfiguration, arguments: RecommendationOptions) -> RecommendationRunResult:
	_preflight_current_artifact(arguments)
	if arguments.capital is not None and configuration.entry_date != configuration.anchor_date:
		raise ValueError("Initial cash requires an actual portfolio state for a later entry")
	if not configuration.source.data_root.exists():
		raise UnknownHistoryBoundary("Recommendation requires evidenced, persisted source history; use update_market_data first")
	connect = MySQLConnectionFactory()
	table_names = load_database_configuration(configuration.source.database_config).load_table_names()
	try:
		if target_schema_state(connect, configuration.source.database_config, table_names) == "target":
			return _target_assemble(configuration, arguments, connect, table_names)
	except RuntimeError:
		# A legacy history workflow can remain usable when its optional target
		# database is unavailable.  A present incompatible target is not hidden.
		pass
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
	validation = MySQLValidationRepository(job.source, connect, table_names)
	closes = VerifiedCloseRepository(job.source, validation)
	opening_store = OpeningStore(job.source.data_root)
	market_data = MySQLMarketDataRepository(job.source, connect, table_names)
	market_data.legacy_read_only = job.source.identity.adjustment is PriceAdjustment.ADJUSTED
	services = RecommendationServices(ConfiguredPolygonProvider(arguments.max_retries), market_data, validation, SignalRepository(closes, opening_store), VerifiedEligibilityRepository(closes), OpeningRefresh(job.source, opening_store, ConfiguredOpeningProvider(arguments.max_retries), closes))
	return recommend(job, boundary, calendar, services, clock, portfolio=portfolio, execution=ExecutionPolicy(decimal_text(arguments.fee_per_fill)))
