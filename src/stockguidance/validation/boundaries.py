from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.market_data import SessionSnapshot
from stockguidance.market_data.coverage import CoveragePlan
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.update_planning import SnapshotIndex
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding


def boundary_findings(plan: CoveragePlan, snapshots: tuple[SessionSnapshot, ...], index: SnapshotIndex, calendar: SessionCalendar, publication: DailyPublicationPolicy) -> tuple[ValidationFinding, ...]:
	findings: list[ValidationFinding] = []
	requested = {item.session.sequence for item in plan.sessions}
	for snapshot in snapshots:
		if snapshot.session.sequence not in requested or snapshot.session.sequence not in index.by_sequence:
			continue
		session_date = snapshot.session.session_date
		tickers = tuple(bar.ticker for bar in snapshot.records)
		prior, following = index.neighbors(snapshot)
		prior_keys = {bar.ticker for bar in prior}
		without_prior = tuple(ticker for ticker in tickers if ticker not in prior_keys)
		if session_date == plan.boundary.history_start:
			findings.append(ValidationFinding(FindingCategory.EXPECTED_ABSENCE, ValidationCheck.HISTORY, "Supported-history boundary has no predecessor-based features; observations are unavailable, never zero-filled", session_date, tickers))
		elif prior and without_prior:
			findings.append(ValidationFinding(FindingCategory.EXPECTED_ABSENCE, ValidationCheck.HISTORY, "Ticker absent from the prior request response; no listing lifetime or synthetic history is inferred", session_date, without_prior))
		elif not prior and snapshot.session.sequence - 1 not in requested:
			findings.append(ValidationFinding(FindingCategory.NOT_ASSESSED, ValidationCheck.HISTORY, "Predecessor context is outside requested coverage and has no verified observations", session_date, tickers))
		if session_date >= calendar.end:
			findings.append(ValidationFinding(FindingCategory.NOT_ASSESSED, ValidationCheck.OUTCOME, "Exit session lies outside the loaded calendar", session_date, tickers))
			continue
		exit_session = calendar.shift(snapshot.session, 1)
		expected = publication.expected_at(exit_session)
		if exit_session.closes_at > plan.request.as_of or (expected is not None and expected > plan.request.as_of):
			findings.append(ValidationFinding(FindingCategory.NOT_YET_AVAILABLE, ValidationCheck.OUTCOME, "Next-session daily price is immature at the cutoff; final legacy labels remain unavailable", session_date, tickers))
		elif snapshot.session.sequence + 1 in index.by_sequence:
			following_keys = {bar.ticker for bar in following}
			absent = tuple(ticker for ticker in tickers if ticker not in following_keys)
			if absent:
				findings.append(ValidationFinding(FindingCategory.EXPECTED_ABSENCE, ValidationCheck.OUTCOME, "Ticker absent from the next request response; outcome is unavailable without inventing a delisting or price", session_date, absent))
		elif exit_session.sequence in requested:
			findings.append(ValidationFinding(FindingCategory.MISSING, ValidationCheck.OUTCOME, "Mature next-session dependency is missing or unverified inside requested coverage", session_date, tickers))
		else:
			findings.append(ValidationFinding(FindingCategory.NOT_ASSESSED, ValidationCheck.OUTCOME, "Exit dependency lies beyond requested coverage and has no verified observations; mature outcome is not validated", session_date, tickers))
	return tuple(findings)
