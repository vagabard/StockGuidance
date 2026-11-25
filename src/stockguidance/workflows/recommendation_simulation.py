"""Use resolved recommendation universes in the established simulation engine."""

from dataclasses import asdict
from datetime import timedelta
import json

from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.recommendations.options import RecommendationOptions
from stockguidance.recommendations.resolution import resolve_start
from stockguidance.database.simplified_runtime import validate_simplified_database
from stockguidance.simulation.configuration import SimulationRequest, EvaluationPolicy
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.prices import RepositoryExecutionPrices
from stockguidance.simulation.recommendation_source import RecommendationReplayScores
from stockguidance.targets.specification import TargetSpec
from stockguidance.workflows.flexible_recommendation import load_setup,resolve_setup,prepare_raw_history
from stockguidance.workflows.simplified_readiness import SimplifiedCloseRepository
from stockguidance.config.source import SourceConfig
from stockguidance.database.table_names import TableNames
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.simulation.jobs import SimulationConfiguration
from stockguidance.simulation.options import SimulationOptions
from stockguidance.simulation.result import SimulationResult
from stockguidance.features.specification import FeatureSpec


def simulate_recommendations(configuration: SimulationConfiguration, options: SimulationOptions, source: SourceConfig, connect: WriteConnectionFactory, names: TableNames) -> SimulationResult:
	if options.model_policy is not EvaluationPolicy.CHRONOLOGICAL_RETRAIN or configuration.retraining is None or options.artifacts_directory is None or configuration.recommendation_setup is None:
		raise ValueError("Recommendation simulation requires chronological_retrain with an artifact directory")
	if configuration.availability_policy.retrospective:
		raise ValueError("Recommendation simulation uses current revised history, not receipt-time replay")
	settings = configuration.retraining
	if configuration.publication.after_close is None:
		raise ValueError("Recommendation simulation requires a publication policy")
	feature_fields = json.loads(json.dumps(asdict(configuration.features)))
	feature_fields.pop("ticker_ids")
	training_fields = json.loads(json.dumps(asdict(settings.configuration)))
	model_fields = training_fields.pop("model")
	base = {"database_config":str(source.database_config),"data_root":str(source.data_root),"features":feature_fields,
		"model":model_fields,"training":training_fields,"start_date":str(settings.start),
		"publication":{"delay_minutes":configuration.publication.after_close.total_seconds()/60,"evidence":configuration.publication.provenance},
		"eligibility":{"minimum_transactions":configuration.eligibility.minimum_transactions},"top_k":configuration.top_k,
		"runs_root":str(configuration.runs_root or "runs")}
	base.update(configuration.recommendation_setup)
	# Data identity comes from the simulation's verified source, never from a
	# nested recommendation setting for a different database.
	base.update(database_config=str(source.database_config),data_root=str(source.data_root))
	setup = load_setup(None,template=base,automatic_artifact=False)
	clock = SystemClock()
	calendar = ExchangeCalendar(source.calendar_id,configuration.start-timedelta(days=30),
		(configuration.end or configuration.start+timedelta(days=configuration.horizon_sessions*5))+timedelta(days=30))
	first_end = calendar.shift(calendar.session(configuration.start),-1).session_date
	initial_options = RecommendationOptions(ModelRefreshPolicy.RETRAIN,options.artifacts_directory / "initial.sgmodel")
	from dataclasses import replace
	initial,_ = resolve_setup(replace(setup,end=str(first_end)),initial_options,connect=connect,clock=clock)
	if initial.start is None or not isinstance(initial.features,FeatureSpec):
		raise ValueError("The investment simulator requires previous-close v1 feature semantics")
	earliest = min(initial.start,resolve_start(setup.start,first_end))
	calendar = ExchangeCalendar(source.calendar_id,earliest-timedelta(days=max(60,(initial.features.lookback_sessions+2)*3)),calendar.end)
	end = configuration.end or calendar.shift(calendar.session(configuration.start),configuration.horizon_sessions-1).session_date
	execution_sessions = calendar.sessions(configuration.start,end)
	prepare_raw_history(source,connect,calendar,execution_sessions,refresh=True,provider=ConfiguredPolygonProvider(),clock=clock,names=names)
	validation = validate_simplified_database(source,connect,calendar,execution_sessions,table_names=names)
	if validation.state=="unfinished_operation":
		raise ValueError("Pending maintenance overlaps historical execution")
	boundary = HistoryBoundary(source.identity,calendar.calendar_id,earliest,HistoryProvenance.SOURCE_HISTORY,"Current validated historical data",configuration.audit_cutoff)
	request = SimulationRequest(source.identity,initial.features,TargetSpec(),AllocationSpec(configuration.allocation_policy,calendar.session(configuration.anchor_date)),
		configuration.start,end,configuration.initial_capital,configuration.decision_lead,configuration.eligibility,configuration.top_k,configuration.execution)
	scores = RecommendationReplayScores(setup,options.artifacts_directory,every_buy_decisions=settings.every_buy_decisions,
		resolver=lambda s,o:resolve_setup(s,o,connect=connect,clock=clock))
	return simulate(request,boundary,calendar,scores,RepositoryExecutionPrices(boundary,SimplifiedCloseRepository(source,connect,names),configuration.audit_cutoff))
