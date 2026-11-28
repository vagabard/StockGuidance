"""Recommendation-style simulation with one frozen, fully covered universe."""
from dataclasses import asdict, replace
from datetime import timedelta

from stockguidance.artifacts.decoding import decode_features, decode_target
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import SourceConfig
from stockguidance.config.training import decode_training_configuration
from stockguidance.database.prepared_history import load_prepared_history
from stockguidance.database.table_names import TableNames
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.datasets.prepared import PreparedLiquidity
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.recommendations.resolution import resolve_start
from stockguidance.simulation.configuration import SimulationRequest, EvaluationPolicy
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.jobs import SimulationConfiguration
from stockguidance.simulation.options import SimulationOptions
from stockguidance.simulation.prices import RepositoryExecutionPrices
from stockguidance.simulation.result import SimulationResult
from stockguidance.simulation.score_source import HistoricalSignals, ChronologicalModelScores, WalkForwardTraining, DecisionForecast
from stockguidance.workflows.flexible_recommendation import load_setup


class PreparedRecommendationScores(ChronologicalModelScores):
	"""Preserve relative retraining ranges while retaining one loaded universe."""
	def __init__(self, plan: WalkForwardTraining, signals: HistoricalSignals, start_specification: str) -> None:
		super().__init__(plan,signals)
		self.all_decisions = plan.decisions
		self.start_specification = start_specification

	def forecast(self, request: SimulationRequest, timing: DecisionTiming, buy_index: int) -> DecisionForecast:
		start = resolve_start(self.start_specification,timing.previous_session.session_date)
		decisions = tuple(d for d in self.all_decisions if d.entry_session.session_date>=start)
		self.plan = replace(self.plan,decisions=decisions)
		return super().forecast(request,timing,buy_index)


def simulate_recommendations(configuration: SimulationConfiguration, options: SimulationOptions, source: SourceConfig, connect: WriteConnectionFactory, names: TableNames) -> SimulationResult:
	if options.model_policy is not EvaluationPolicy.CHRONOLOGICAL_RETRAIN or configuration.retraining is None or options.artifacts_directory is None or configuration.recommendation_setup is None:
		raise ValueError("Recommendation simulation requires chronological_retrain with an artifact directory")
	settings = configuration.retraining
	base = configuration.recommendation_setup
	setup = load_setup(None, start=str(base.get("start_date",settings.start)), template=base)
	anchor_calendar = ExchangeCalendar(source.calendar_id,configuration.start-timedelta(days=14),configuration.start+timedelta(days=14))
	first_end = anchor_calendar.shift(anchor_calendar.session(configuration.start),-1).session_date
	start = resolve_start(setup.start,first_end)
	fields = configuration.features.to_mapping() if hasattr(configuration.features,"to_mapping") else asdict(configuration.features)
	fields.update(base.get("features",{}))
	fields["ticker_ids"] = list(setup.selection.ticker_ids or configuration.features.ticker_ids)
	if "channels" in fields:
		fields.pop("feature_order",None)
		fields.setdefault("schema","configured-features-v2")
		fields.setdefault("layout","session_oldest_first,ticker,feature:C")
	features = decode_features(fields)
	target = decode_target(base["target"]) if "target" in base else configuration.target
	padding = max(200, (getattr(features,"required_history_sessions",features.lookback_sessions)+2)*3, (len(target.baseline_offsets)+2)*3)
	calendar = ExchangeCalendar(source.calendar_id,start-timedelta(days=padding),(configuration.end or configuration.start+timedelta(days=configuration.horizon_sessions*5))+timedelta(days=30))
	end = configuration.end or calendar.shift(calendar.session(configuration.start),configuration.horizon_sessions-1).session_date
	sessions = calendar.sessions(start,end)
	decisions = tuple(DecisionTiming(calendar.shift(s,-1),s,calendar.shift(s,1),s.closes_at-configuration.decision_lead) for s in sessions)
	evaluation = tuple(d for d in decisions if d.entry_session.session_date>=configuration.start)
	labels = tuple(d for d in decisions if d.exit_session.session_date<=end)
	history = load_prepared_history(source,connect,calendar,decisions,features,target,label_decisions=labels,
		extra_sessions=sessions,selection=setup.selection,transaction_sessions=tuple(d.previous_session for d in evaluation),table_names=names,options=configuration.data_loading)
	boundary = HistoryBoundary(source.identity,calendar.calendar_id,history.sessions[0].session_date,HistoryProvenance.EXPLICIT_BOUNDARY,"Current prepared history",configuration.audit_cutoff)
	request = SimulationRequest(source.identity,history.features,target,AllocationSpec(configuration.allocation_policy,calendar.session(configuration.anchor_date)),configuration.start,end,configuration.initial_capital,configuration.decision_lead,configuration.eligibility,configuration.top_k,configuration.execution,AvailabilityPolicy.CURRENT_PREPARED)
	signals = HistoricalSignals(boundary,calendar,configuration.publication,history,PreparedLiquidity(history),AvailabilityPolicy.CURRENT_PREPARED)
	training_config = settings.configuration
	training_overrides = base.get("retraining") or {}
	if "training" in base or "model" in base or training_overrides:
		default_training = asdict(training_config)
		default_training.pop("model")
		training_config = decode_training_configuration(training_overrides.get("training",base.get("training",default_training)),training_overrides.get("model",base.get("model",asdict(training_config.model))))
	plan = WalkForwardTraining(labels,training_config,options.artifacts_directory,settings.every_buy_decisions)
	result = simulate(request,boundary,calendar,PreparedRecommendationScores(plan,signals,setup.start),RepositoryExecutionPrices(boundary,history,configuration.audit_cutoff))
	return replace(result,data_preparation={"selection":history.selection,"timings":history.timings})
