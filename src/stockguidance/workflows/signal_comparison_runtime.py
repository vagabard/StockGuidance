from datetime import timedelta
from pathlib import Path

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.repositories.eligibility import VerifiedEligibilityRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.signals import SignalRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.market_data.history import UnknownHistoryBoundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.portfolio.policy import AllocationSpec
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.simulation.jobs import SimulationConfiguration
from stockguidance.simulation.prices import RepositoryExecutionPrices
from stockguidance.simulation.score_source import HistoricalSignals, WalkForwardTraining
from stockguidance.simulation.signal_comparison import SignalComparison, compare_signal_modes
from stockguidance.targets.specification import TargetSpec


def assemble_signal_comparison(configuration: SimulationConfiguration, artifacts_directory: Path) -> SignalComparison:
	settings = configuration.retraining
	if settings is None:
		raise ValueError("Signal comparison requires chronological retraining settings")
	boundary = JsonHistoryStore(configuration.source.data_root / "history").load(configuration.source.identity)
	if boundary is None:
		raise UnknownHistoryBoundary("Signal comparison requires evidenced supported history")
	calendar = ExchangeCalendar(boundary.calendar_id, min(boundary.history_start, configuration.anchor_date) - timedelta(days=14), configuration.end + timedelta(days=14))
	closes = VerifiedCloseRepository(configuration.source, MySQLValidationRepository(configuration.source, MySQLConnectionFactory()))
	signals = HistoricalSignals(boundary, calendar, configuration.publication, SignalRepository(closes, OpeningStore(configuration.source.data_root)), VerifiedEligibilityRepository(closes))
	request = SimulationRequest(configuration.source.identity, configuration.features, TargetSpec(), AllocationSpec(configuration.allocation_policy, calendar.session(configuration.anchor_date)), configuration.start, configuration.end, configuration.initial_capital, configuration.decision_lead, configuration.eligibility, configuration.top_k, configuration.execution)
	decisions = tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - settings.decision_lead) for session in calendar.sessions(settings.start, settings.end))
	plan = WalkForwardTraining(decisions, settings.configuration, artifacts_directory, settings.every_buy_decisions)
	return compare_signal_modes(request, boundary, calendar, signals, RepositoryExecutionPrices(boundary, closes, configuration.audit_cutoff), plan)
