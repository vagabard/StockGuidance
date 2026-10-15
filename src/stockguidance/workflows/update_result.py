from dataclasses import dataclass
from datetime import date

from stockguidance.market_data.coverage import CoveragePlan
from stockguidance.market_data.manifests import SourceManifest
from stockguidance.validation.coverage import CoverageResult, CoverageState
from stockguidance.workflows.failures import StageFailure


@dataclass(frozen=True)
class UpdateResult:
	plan: CoveragePlan
	coverage: CoverageResult
	manifests: tuple[SourceManifest, ...]
	closed_dates: tuple[date, ...]
	data_revision: str
	failures: tuple[StageFailure, ...]
	downloaded_sessions: int

	@property
	def successful(self) -> bool:
		states = tuple(item.state for item in self.coverage.sessions)
		return not self.failures and all(state in {CoverageState.COMPLETE, CoverageState.NOT_YET_AVAILABLE} for state in states) and (CoverageState.COMPLETE in states or (not states and bool(self.closed_dates)))
