from datetime import timedelta

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.simulation.jobs import SimulationConfiguration
from stockguidance.database.repositories.eligibility import VerifiedEligibilityRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.database.repositories.signals import SignalRepository
from stockguidance.database.validity import enforce_minimum_valid_session
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.simplified_runtime import source_from_database, target_schema_state
from stockguidance.database.table_names import TableNames
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.market_data.history import UnknownHistoryBoundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.simulation.configuration import EvaluationPolicy, SimulationRequest
from stockguidance.simulation.options import SimulationOptions
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.prices import RepositoryExecutionPrices
from stockguidance.simulation.result import SimulationResult
from stockguidance.simulation.score_source import ChronologicalModelScores, FixedModelScores, HistoricalSignals, ScoreSource, WalkForwardTraining
from stockguidance.targets.specification import TargetSpec
from stockguidance.workflows.simplified_readiness import (
	SimplifiedCloseRepository,
	SimplifiedEligibilityRepository,
	SimplifiedReadinessWorkflow,
	boundary_for_scope,
	required_session_scope,
)


def _target_simulation(configuration: SimulationConfiguration, arguments: SimulationOptions, connect: WriteConnectionFactory, names: TableNames) -> SimulationResult:
	source, names = source_from_database(configuration.source.database_config, configuration.source.data_root, connect, names)
	if source.identity != configuration.source.identity:
		raise ValueError("Simulation source differs from the database-owned configuration")
	if (configuration.retraining is not None) != (arguments.model_policy is EvaluationPolicy.CHRONOLOGICAL_RETRAIN):
		raise ValueError("Model policy and training configuration differ")
	requested_start = configuration.start if configuration.retraining is None else min(configuration.start, configuration.retraining.start)
	calendar = ExchangeCalendar(source.calendar_id, requested_start - timedelta(days=60), configuration.end + timedelta(days=30))
	request = SimulationRequest(source.identity, configuration.features, TargetSpec(), AllocationSpec(configuration.allocation_policy, calendar.session(configuration.anchor_date)), configuration.start, configuration.end, configuration.initial_capital, configuration.decision_lead, configuration.eligibility, configuration.top_k, configuration.execution, configuration.availability_policy, configuration.data_snapshot)
	evaluation_decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - configuration.decision_lead) for session in calendar.sessions(configuration.start, configuration.end))
	training_decisions: tuple[DecisionTiming, ...] = ()
	if configuration.retraining is not None:
		settings = configuration.retraining
		training_decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - settings.decision_lead) for session in calendar.sessions(settings.start, settings.end))
	all_decisions = tuple(sorted((*training_decisions, *evaluation_decisions), key=lambda item: item.entry_session.sequence))
	scope = required_session_scope(calendar, all_decisions, configuration.features, request.target, include_labels=configuration.retraining is not None, extra_sessions=tuple(calendar.sessions(configuration.start, configuration.end)))
	boundary = boundary_for_scope(source, calendar, scope, configuration.audit_cutoff)
	provider = ConfiguredPolygonProvider()
	readiness = SimplifiedReadinessWorkflow(source, connect, calendar, configuration.publication, provider, SystemClock(), table_names=names, staging_root=source.data_root / ".database-simplification" / "pending")
	readiness.ensure(
		all_decisions,
		request.features,
		request.target,
		label_cutoff=configuration.audit_cutoff if configuration.retraining is not None else None,
		as_of=configuration.audit_cutoff,
		availability_policy=configuration.availability_policy,
		data_snapshot=configuration.data_snapshot,
	)
	closes = SimplifiedCloseRepository(source, connect, names)
	signals = HistoricalSignals(boundary, calendar, configuration.publication, SignalRepository(closes, OpeningStore(source.data_root)), SimplifiedEligibilityRepository(closes), configuration.availability_policy, configuration.data_snapshot)
	scores: ScoreSource
	if EvaluationPolicy(arguments.model_policy) is EvaluationPolicy.FIXED:
		if arguments.artifact is None or arguments.artifact_id is None:
			raise ValueError("Fixed evaluation requires an explicit artifact")
		scores = FixedModelScores(ArtifactReference(arguments.artifact, arguments.artifact_id), signals, ArtifactKind(arguments.artifact_kind))
	else:
		walk_forward = configuration.retraining
		if walk_forward is None or arguments.artifacts_directory is None:
			raise ValueError("Chronological retraining requires explicit historical training settings")
		scores = ChronologicalModelScores(WalkForwardTraining(training_decisions, walk_forward.configuration, arguments.artifacts_directory, walk_forward.every_buy_decisions), signals)
	return simulate(request, boundary, calendar, scores, RepositoryExecutionPrices(boundary, closes, configuration.audit_cutoff))


def assemble_and_simulate(configuration: SimulationConfiguration, arguments: SimulationOptions) -> SimulationResult:
	connect = MySQLConnectionFactory()
	names = load_database_configuration(configuration.source.database_config).load_table_names()
	try:
		if target_schema_state(connect, configuration.source.database_config, names) == "target":
			return _target_simulation(configuration, arguments, connect, names)
	except (RuntimeError, TypeError):
		pass
	boundary = JsonHistoryStore(configuration.source.data_root / "history").load(configuration.source.identity)
	if boundary is None:
		raise UnknownHistoryBoundary("Simulation requires evidenced, persisted source history")
	requested_start = configuration.start if configuration.retraining is None else min(configuration.start, configuration.retraining.start)
	enforce_minimum_valid_session(configuration.source, requested_start)
	if (configuration.retraining is not None) != (arguments.model_policy is EvaluationPolicy.CHRONOLOGICAL_RETRAIN):
		raise ValueError("Model policy and training configuration differ")
	calendar = ExchangeCalendar(boundary.calendar_id, min(boundary.history_start, configuration.anchor_date) - timedelta(days=14), configuration.end + timedelta(days=14))
	if configuration.availability_policy.retrospective:
		latest_signal = calendar.shift(calendar.session(configuration.end), -1)
		expected = configuration.publication.expected_at(latest_signal)
		if expected is None or configuration.data_snapshot is None or configuration.data_snapshot < expected:
			raise ValueError("Retrospective data_snapshot must include the latest previous-close publication")
	if configuration.audit_cutoff < calendar.session(configuration.end).closes_at:
		raise ValueError("Execution audit cutoff must include the evaluation end close")
	request = SimulationRequest(configuration.source.identity, configuration.features, TargetSpec(), AllocationSpec(configuration.allocation_policy, calendar.session(configuration.anchor_date)), configuration.start, configuration.end, configuration.initial_capital, configuration.decision_lead, configuration.eligibility, configuration.top_k, configuration.execution, configuration.availability_policy, configuration.data_snapshot)
	closes = VerifiedCloseRepository(configuration.source, MySQLValidationRepository(configuration.source, connect, names, (20, 60)))
	signals = HistoricalSignals(boundary, calendar, configuration.publication, SignalRepository(closes, OpeningStore(configuration.source.data_root)), VerifiedEligibilityRepository(closes), configuration.availability_policy, configuration.data_snapshot)
	scores: ScoreSource
	if EvaluationPolicy(arguments.model_policy) is EvaluationPolicy.FIXED:
		if arguments.artifact is None or arguments.artifact_id is None:
			raise ValueError("Fixed evaluation requires an explicit artifact")
		scores = FixedModelScores(ArtifactReference(arguments.artifact, arguments.artifact_id), signals, ArtifactKind(arguments.artifact_kind))
	else:
		settings = configuration.retraining
		if settings is None or arguments.artifacts_directory is None:
			raise ValueError("Chronological retraining requires explicit historical training settings")
		decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - settings.decision_lead) for session in calendar.sessions(settings.start, settings.end))
		scores = ChronologicalModelScores(WalkForwardTraining(decisions, settings.configuration, arguments.artifacts_directory, settings.every_buy_decisions), signals)
	return simulate(request, boundary, calendar, scores, RepositoryExecutionPrices(boundary, closes, configuration.audit_cutoff))
