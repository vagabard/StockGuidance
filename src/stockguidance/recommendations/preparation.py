from stockguidance.calendar.clock import Clock
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.resolution import resolve_decision
from stockguidance.features.history_window import WindowState, plan_feature_history
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.requests import MarketDataRequest
from stockguidance.recommendations.configuration import RecommendationJob
from stockguidance.recommendations.request import RecommendationRequest


def prepare_request(job: RecommendationJob, boundary: HistoryBoundary, calendar: SessionCalendar, clock: Clock) -> RecommendationRequest:
	boundary.validate_for(job.source.identity, calendar)
	timing = resolve_decision(calendar, job.entry_date, clock)
	request = RecommendationRequest(job.source, MarketDataRequest(job.source.identity, job.start, job.end, timing.decision_cutoff), timing, job.features, job.target, job.model, job.allocation, job.eligibility, job.top_k, job.availability_policy)
	if job.start < boundary.history_start or job.end < timing.previous_session.session_date:
		raise ValueError("Coverage must respect supported history and include the previous session")
	if calendar.session(job.allocation.anchor_session.session_date) != job.allocation.anchor_session:
		raise ValueError("Allocation anchor differs from the authoritative calendar")
	window = plan_feature_history(calendar, boundary, job.features, timing, job.publication)
	if window.state is not WindowState.EXPECTED_AVAILABLE or job.start > window.sessions[0].session_date:
		raise ValueError("Required previous-close history or publication policy is unavailable within coverage")
	if job.retraining is not None:
		for decision in job.retraining.decisions:
			if decision.entry_session.session_date < job.start or decision.entry_session.session_date >= job.entry_date or decision.decision_cutoff > timing.decision_cutoff:
				raise ValueError("Training decisions must lie in coverage before the entry decision")
			if any(calendar.session(session.session_date) != session for session in (decision.previous_session, decision.entry_session, decision.exit_session)):
				raise ValueError("Training decision differs from the authoritative calendar")
			training_window = plan_feature_history(calendar, boundary, job.features, decision, job.publication)
			if training_window.sessions and job.start > training_window.sessions[0].session_date:
				raise ValueError("Coverage omits required historical training inputs")
	return request
