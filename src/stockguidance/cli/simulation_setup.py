import argparse
from datetime import timedelta

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.simulation_configuration import SimulationConfiguration
from stockguidance.database.repositories.eligibility import VerifiedEligibilityRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.database.write_connections import LegacyWriteConnectionFactory
from stockguidance.market_data.history import UnknownHistoryBoundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.simulation.configuration import EvaluationPolicy, SimulationRequest
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.prices import RepositoryExecutionPrices
from stockguidance.simulation.result import SimulationResult
from stockguidance.simulation.score_source import ChronologicalModelScores, FixedModelScores, HistoricalSignals, ScoreSource, WalkForwardTraining
from stockguidance.targets.specification import TargetSpec


def assemble_and_simulate(configuration: SimulationConfiguration, arguments: argparse.Namespace) -> SimulationResult:
	boundary = JsonHistoryStore(configuration.source.data_root / "history").load(configuration.source.identity)
	if boundary is None:
		raise UnknownHistoryBoundary("Simulation requires evidenced, persisted source history")
	calendar = ExchangeCalendar(boundary.calendar_id, min(boundary.history_start, configuration.anchor_date) - timedelta(days=14), configuration.end + timedelta(days=14))
	if configuration.audit_cutoff < calendar.session(configuration.end).closes_at:
		raise ValueError("Execution audit cutoff must include the evaluation end close")
	request = SimulationRequest(configuration.source.identity, configuration.features, TargetSpec(), AllocationSpec(configuration.allocation_policy, calendar.session(configuration.anchor_date)), configuration.start, configuration.end, configuration.initial_capital, configuration.decision_lead, configuration.eligibility, configuration.top_k, configuration.execution)
	closes = VerifiedCloseRepository(configuration.source, MySQLValidationRepository(configuration.source, LegacyWriteConnectionFactory()))
	signals = HistoricalSignals(boundary, calendar, configuration.publication, closes, VerifiedEligibilityRepository(closes))
	scores: ScoreSource
	if EvaluationPolicy(arguments.model_policy) is EvaluationPolicy.FIXED:
		scores = FixedModelScores(ArtifactReference(arguments.artifact, arguments.artifact_id), signals, ArtifactKind(arguments.artifact_kind))
	else:
		settings = configuration.retraining
		if settings is None:
			raise ValueError("Chronological retraining requires explicit historical training settings")
		decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - settings.decision_lead) for session in calendar.sessions(settings.start, settings.end))
		scores = ChronologicalModelScores(WalkForwardTraining(decisions, settings.configuration, arguments.artifacts_directory, settings.every_buy_decisions), signals)
	return simulate(request, boundary, calendar, scores, RepositoryExecutionPrices(boundary, closes, configuration.audit_cutoff))
