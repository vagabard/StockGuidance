from dataclasses import dataclass

from stockguidance.calendar.sessions import TradingSession
from stockguidance.database.repositories.market_data import SessionSnapshot
from stockguidance.database.repositories.validation import SessionRecordCounts
from stockguidance.market_data.coverage import CoveragePlan
from stockguidance.market_data.manifests import SourceManifest
from stockguidance.validation.coverage import CoverageResult, CoverageState
from stockguidance.validation.findings import FindingCategory, ValidationFinding
from stockguidance.validation.repair import RepairPlan


@dataclass(frozen=True)
class ValidationResult:
	plan: CoveragePlan
	coverage: CoverageResult
	findings: tuple[ValidationFinding, ...]
	repair_plan: RepairPlan
	snapshots: tuple[SessionSnapshot, ...]
	manifests: tuple[SourceManifest, ...]
	queried_sessions: tuple[TradingSession, ...]
	record_counts: tuple[SessionRecordCounts, ...]

	@property
	def failed(self) -> bool:
		return any(finding.category is FindingCategory.FAILED for finding in self.findings)

	@property
	def complete(self) -> bool:
		if any(finding.blocking for finding in self.findings):
			return False
		return any(item.state is CoverageState.COMPLETE for item in self.coverage.sessions) or (not self.plan.sessions and max(self.plan.request.start, self.plan.boundary.history_start) <= min(self.plan.request.end, self.plan.request.as_of.date()))

	@property
	def exit_code(self) -> int:
		return 1 if self.failed else 0 if self.complete else 2
