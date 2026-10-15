from dataclasses import dataclass
from datetime import timedelta

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.close_projection import project_closes
from stockguidance.database.repositories.closes import CloseData, CloseQuery
from stockguidance.database.repositories.validation import DatabaseInspection
from stockguidance.datasets.building import build_sample
from stockguidance.datasets.sample import Sample, TargetState
from stockguidance.features.history_window import WindowState
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.closes import PriceState
from stockguidance.market_data.coverage import CoveragePlan
from stockguidance.market_data.manifests import SourceManifest
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.specification import TargetSpec
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding


@dataclass(frozen=True)
class InspectedCloseRepository:
	inspection: DatabaseInspection
	manifests: tuple[SourceManifest, ...]

	def read(self, query: CloseQuery) -> CloseData:
		return project_closes(query, self.inspection, self.manifests)


def validate_close_samples(
	plan: CoveragePlan, inspection: DatabaseInspection, manifests: tuple[SourceManifest, ...],
	calendar: SessionCalendar, publication: DailyPublicationPolicy,
	features: FeatureSpec | None, target: TargetSpec,
) -> tuple[tuple[ValidationFinding, ...], tuple[Sample, ...]]:
	findings: list[ValidationFinding] = []
	samples: list[Sample] = []
	snapshots = {snapshot.session: snapshot for snapshot in inspection.snapshots}
	symbols = dict(inspection.ticker_symbols)
	requested = {item.session for item in plan.sessions}
	repository = InspectedCloseRepository(inspection, manifests)
	if features is None:
		findings.append(ValidationFinding(FindingCategory.NOT_ASSESSED, ValidationCheck.FEATURE, "Model feature validation requires an explicit ordered ticker universe and lookback; targets are computed independently from verified raw closes"))
	for item in plan.sessions:
		entry = item.session
		snapshot = snapshots.get(entry)
		if snapshot is None or item.expected_available_at is None or item.expected_available_at > plan.request.as_of or (snapshot.receipt is not None and snapshot.receipt.available_at > plan.request.as_of):
			continue
		tickers = tuple(bar.ticker for bar in snapshot.records)
		ticker_ids = tuple(ticker_id for ticker_id, symbol in inspection.ticker_symbols if symbol in tickers)
		if features is None and not ticker_ids:
			continue
		if entry.session_date <= calendar.start or entry.session_date >= calendar.end:
			findings.append(ValidationFinding(FindingCategory.NOT_ASSESSED, ValidationCheck.TARGET, "Sample neighbors are outside the loaded calendar", entry.session_date, tickers))
			continue
		previous, exit_session = calendar.shift(entry, -1), calendar.shift(entry, 1)
		decision = min(entry.closes_at - timedelta(microseconds=1), plan.request.as_of)
		if decision < previous.closes_at:
			continue
		specification = features or FeatureSpec(ticker_ids, 1, adjustment=plan.request.source.adjustment)
		sample = build_sample(specification, target, DecisionTiming(previous, entry, exit_session, decision), plan.boundary, calendar, publication, repository, label_cutoff=plan.request.as_of)
		samples.append(sample)
		if sample.target_state is TargetState.NOT_YET_AVAILABLE:
			findings.append(ValidationFinding(FindingCategory.NOT_YET_AVAILABLE, ValidationCheck.TARGET, "Next-session closing-price target is immature at the assessment cutoff", entry.session_date, tickers))
		elif sample.target_state is TargetState.UNVERIFIED and not sample.targets.issues:
			findings.append(ValidationFinding(FindingCategory.UNVERIFIED, ValidationCheck.TARGET, "Target publication policy is unknown", entry.session_date, tickers))
		for issue in sample.targets.issues:
			category = FindingCategory(issue.state.value)
			if issue.session not in requested and issue.state in {PriceState.MISSING, PriceState.UNVERIFIED}:
				category = FindingCategory.NOT_ASSESSED
			findings.append(ValidationFinding(category, ValidationCheck.TARGET, issue.detail, entry.session_date, (symbols.get(issue.ticker_id, f"ticker_id={issue.ticker_id}"),), dependency_session_date=issue.session.session_date))
		if features is None:
			continue
		if sample.feature_state is WindowState.INSUFFICIENT_HISTORY:
			findings.append(ValidationFinding(FindingCategory.EXPECTED_ABSENCE, ValidationCheck.FEATURE, "Exact close window crosses supported history; sample is unavailable", entry.session_date))
		elif sample.feature_state is not WindowState.EXPECTED_AVAILABLE:
			category = FindingCategory.UNVERIFIED if sample.feature_state is WindowState.UNVERIFIED else FindingCategory.NOT_YET_AVAILABLE
			findings.append(ValidationFinding(category, ValidationCheck.FEATURE, "Close window is not published by the pre-close decision", entry.session_date))
		for issue in sample.features.issues:
			category = FindingCategory(issue.state.value)
			if issue.session not in requested and issue.state in {PriceState.MISSING, PriceState.UNVERIFIED}:
				category = FindingCategory.NOT_ASSESSED
			findings.append(ValidationFinding(category, ValidationCheck.FEATURE, issue.detail, entry.session_date, (symbols.get(issue.ticker_id, f"ticker_id={issue.ticker_id}"),), dependency_session_date=issue.session.session_date))
	return tuple(findings), tuple(samples)
