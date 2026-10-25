from dataclasses import asdict, dataclass
from typing import Protocol

from stockguidance.config.validation import require_positive_int
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.closes import CloseIssue, CloseObservation
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.quality import QualityPolicy


@dataclass(frozen=True)
class CloseQuery:
	scope: SessionQuery
	ticker_ids: tuple[int, ...]
	quality_policy: QualityPolicy = QualityPolicy()

	def __post_init__(self) -> None:
		if not isinstance(self.scope, SessionQuery) or not isinstance(self.ticker_ids, tuple) or not self.ticker_ids or len(set(self.ticker_ids)) != len(self.ticker_ids):
			raise ValueError("Close query requires exact sessions and ordered unique tickers")
		for ticker_id in self.ticker_ids:
			require_positive_int(ticker_id, "query ticker_id")


@dataclass(frozen=True)
class CloseData:
	query: CloseQuery
	observations: tuple[CloseObservation, ...]
	issues: tuple[CloseIssue, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.query, CloseQuery) or not isinstance(self.observations, tuple) or not isinstance(self.issues, tuple):
			raise ValueError("Invalid close result layout")
		expected = {(session, ticker) for session in self.query.scope.sessions for ticker in self.query.ticker_ids}
		keys = []
		for observation in self.observations:
			if not isinstance(observation, CloseObservation) or not observation.timing.is_available_at(self.query.scope.as_of):
				raise ValueError("Close repository returned an unavailable observation")
			keys.append((observation.timing.session, observation.ticker_id))
		for issue in self.issues:
			if not isinstance(issue, CloseIssue):
				raise ValueError("Invalid close result issue")
			keys.append((issue.session, issue.ticker_id))
		if len(keys) != len(set(keys)) or set(keys) != expected:
			raise ValueError("Close result must account for every exact session/ticker key once")

	@property
	def revision(self) -> str:
		return content_id({
			"source": asdict(self.query.scope.boundary.source),
			"observations": sorted((item.timing.session.session_date.isoformat(), item.ticker_id, item.close, item.timing.available_at.isoformat(), item.revision, int(item.quality), item.gap_length, item.provenance) if item.quality else (item.timing.session.session_date.isoformat(), item.ticker_id, item.close, item.timing.available_at.isoformat(), item.revision) for item in self.observations),
			"issues": sorted((item.session.session_date.isoformat(), item.ticker_id, item.state.value, int(item.quality), item.gap_length, item.provenance) if item.quality else (item.session.session_date.isoformat(), item.ticker_id, item.state.value) for item in self.issues),
			"quality_policy": self.query.quality_policy.identity,
		})


class CloseRepository(Protocol):
	def read(self, query: CloseQuery) -> CloseData: ...
