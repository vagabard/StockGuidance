from dataclasses import asdict
from datetime import date, timedelta

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
from stockguidance.simulation.score_source import ChronologicalModelScores, FixedModelScores, HistoricalSignals, PreEvaluationTraining, ScoreSource, TrainThenSimulateScores, WalkForwardTraining
from stockguidance.runs.contracts import RunConfig, RunOperation
from stockguidance.runs.service import RunDirectoryService
from stockguidance.simulation.persistence import persist_simulation_result
from stockguidance.targets.specification import TargetSpec
from stockguidance.workflows.simplified_readiness import (
	SimplifiedCloseRepository,
	SimplifiedEligibilityRepository,
	SimplifiedReadinessWorkflow,
	boundary_for_scope,
	required_session_scope,
)


def _calendar_end(configuration: SimulationConfiguration) -> date:
	end = configuration.end
	if end is None:
		end = configuration.start + timedelta(days=max(45, configuration.horizon_sessions * 5))
	if configuration.retraining is not None:
		end = max(end, configuration.retraining.end + timedelta(days=14))
	return end + timedelta(days=30)


def _resolved_end(configuration: SimulationConfiguration, calendar: ExchangeCalendar) -> date:
	end = configuration.end
	if end is not None:
		return end
	return calendar.shift(calendar.session(configuration.start), configuration.horizon_sessions - 1).session_date


def _training_policy(policy: EvaluationPolicy) -> bool:
	return policy in (EvaluationPolicy.TRAIN_THEN_SIMULATE, EvaluationPolicy.CHRONOLOGICAL_RETRAIN)


def _score_source(arguments: SimulationOptions, configuration: SimulationConfiguration, signals: HistoricalSignals, training_decisions: tuple[DecisionTiming, ...]) -> ScoreSource:
	policy = EvaluationPolicy(arguments.model_policy)
	if policy is EvaluationPolicy.FIXED:
		if arguments.artifact is None or arguments.artifact_id is None:
			raise ValueError("Fixed evaluation requires an explicit artifact")
		return FixedModelScores(ArtifactReference(arguments.artifact, arguments.artifact_id), signals, ArtifactKind(arguments.artifact_kind))
	settings = configuration.retraining
	if settings is None or arguments.artifacts_directory is None:
		raise ValueError("Training-based evaluation requires explicit historical training settings")
	if policy is EvaluationPolicy.TRAIN_THEN_SIMULATE:
		return TrainThenSimulateScores(PreEvaluationTraining(training_decisions, settings.configuration, arguments.artifacts_directory), signals)
	return ChronologicalModelScores(WalkForwardTraining(training_decisions, settings.configuration, arguments.artifacts_directory, settings.every_buy_decisions), signals)


def _target_simulation(configuration: SimulationConfiguration, arguments: SimulationOptions, connect: WriteConnectionFactory, names: TableNames) -> SimulationResult:
	source, names = source_from_database(configuration.source.database_config, configuration.source.data_root, connect, names)
	if source.identity != configuration.source.identity:
		raise ValueError("Simulation source differs from the database-owned configuration")
	if (configuration.retraining is not None) != _training_policy(arguments.model_policy):
		raise ValueError("Model policy and training configuration differ")
	requested_start = configuration.start if configuration.retraining is None else min(configuration.start, configuration.retraining.start)
	calendar = ExchangeCalendar(source.calendar_id, requested_start - timedelta(days=60), _calendar_end(configuration))
	end = _resolved_end(configuration, calendar)
	request = SimulationRequest(source.identity, configuration.features, TargetSpec(), AllocationSpec(configuration.allocation_policy, calendar.session(configuration.anchor_date)), configuration.start, end, configuration.initial_capital, configuration.decision_lead, configuration.eligibility, configuration.top_k, configuration.execution, configuration.availability_policy, configuration.data_snapshot)
	evaluation_decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - configuration.decision_lead) for session in calendar.sessions(configuration.start, end))
	training_decisions: tuple[DecisionTiming, ...] = ()
	if configuration.retraining is not None:
		settings = configuration.retraining
		training_decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - settings.decision_lead) for session in calendar.sessions(settings.start, settings.end))
	all_decisions = tuple(sorted((*training_decisions, *evaluation_decisions), key=lambda item: item.entry_session.sequence))
	scope = required_session_scope(calendar, all_decisions, configuration.features, request.target, include_labels=configuration.retraining is not None, extra_sessions=tuple(calendar.sessions(configuration.start, end)))
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
	scores = _score_source(arguments, configuration, signals, training_decisions)
	return simulate(request, boundary, calendar, scores, RepositoryExecutionPrices(boundary, closes, configuration.audit_cutoff))


def _assemble_and_simulate(configuration: SimulationConfiguration, arguments: SimulationOptions) -> SimulationResult:
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
	if (configuration.retraining is not None) != _training_policy(arguments.model_policy):
		raise ValueError("Model policy and training configuration differ")
	calendar = ExchangeCalendar(boundary.calendar_id, min(boundary.history_start, configuration.anchor_date) - timedelta(days=14), _calendar_end(configuration))
	end = _resolved_end(configuration, calendar)
	if configuration.availability_policy.retrospective:
		latest_signal = calendar.shift(calendar.session(end), -1)
		expected = configuration.publication.expected_at(latest_signal)
		if expected is None or configuration.data_snapshot is None or configuration.data_snapshot < expected:
			raise ValueError("Retrospective data_snapshot must include the latest previous-close publication")
	if configuration.audit_cutoff < calendar.session(end).closes_at:
		raise ValueError("Execution audit cutoff must include the evaluation end close")
	request = SimulationRequest(configuration.source.identity, configuration.features, TargetSpec(), AllocationSpec(configuration.allocation_policy, calendar.session(configuration.anchor_date)), configuration.start, end, configuration.initial_capital, configuration.decision_lead, configuration.eligibility, configuration.top_k, configuration.execution, configuration.availability_policy, configuration.data_snapshot)
	closes = VerifiedCloseRepository(configuration.source, MySQLValidationRepository(configuration.source, connect, names, (20, 60)))
	signals = HistoricalSignals(boundary, calendar, configuration.publication, SignalRepository(closes, OpeningStore(configuration.source.data_root)), VerifiedEligibilityRepository(closes), configuration.availability_policy, configuration.data_snapshot)
	scores: ScoreSource
	training_decisions: tuple[DecisionTiming, ...] = ()
	if configuration.retraining is not None:
		settings = configuration.retraining
		training_decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - settings.decision_lead) for session in calendar.sessions(settings.start, settings.end))
	scores = _score_source(arguments, configuration, signals, training_decisions)
	return simulate(request, boundary, calendar, scores, RepositoryExecutionPrices(boundary, closes, configuration.audit_cutoff))


def _run_settings(configuration: SimulationConfiguration, arguments: SimulationOptions) -> dict[str, object]:
	return {"model_policy": arguments.model_policy.value, "source": asdict(configuration.source.identity), "data_root": configuration.source.data_root, "database_config": configuration.source.database_config, "start_date": configuration.start, "end_date": configuration.end, "horizon_sessions": configuration.horizon_sessions, "allocation": configuration.allocation_policy.value, "initial_capital": configuration.initial_capital, "simulation_top_k": configuration.top_k, "runs_root": configuration.runs_root}


def assemble_and_simulate(configuration: SimulationConfiguration, arguments: SimulationOptions) -> SimulationResult:
	"""Assemble one historical simulation and persist every attempt."""
	service = RunDirectoryService(configuration.runs_root)
	handle = service.create(RunConfig(RunOperation.SIMULATION, _run_settings(configuration, arguments)))
	service.start(handle.run_id)
	try:
		result = _assemble_and_simulate(configuration, arguments)
		artifact_ids = () if arguments.artifact_id is None else (arguments.artifact_id,)
		persist_simulation_result(service, handle, result, artifact_ids=artifact_ids)
		return result
	except Exception as error:
		try:
			service.fail(handle.run_id, stage="assembly", code=type(error).__name__, message=str(error))
		except Exception:
			pass
		raise
