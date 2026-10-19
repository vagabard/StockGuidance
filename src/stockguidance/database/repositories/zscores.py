from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_enum
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.zscores.inputs import ZScoreInput
from stockguidance.zscores.results import ZScoreResult
from stockguidance.zscores.specification import ZScoreFamily
from stockguidance.zscores.storage import VerifiedZScoreRead, ZScoreDependencySnapshot, ZScoreSeriesDefinition


@dataclass(frozen=True)
class ZScoreInputQuery:
	"""Bounded local input request, including warm-up dependency sessions."""

	scope: SessionQuery
	family: ZScoreFamily
	ticker_ids: tuple[int, ...]
	scored_sessions: tuple[TradingSession, ...] | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.scope, SessionQuery):
			raise ValueError("Z-score input query requires a session scope")
		require_enum(self.family, ZScoreFamily, "z-score family")
		if not isinstance(self.ticker_ids, tuple) or not self.ticker_ids or len(set(self.ticker_ids)) != len(self.ticker_ids) or any(type(value) is not int or value < 1 for value in self.ticker_ids):
			raise ValueError("Z-score input query requires ordered unique ticker IDs")
		scored = self.scope.sessions if self.scored_sessions is None else self.scored_sessions
		if not isinstance(scored, tuple) or not scored or any(session not in self.scope.sessions for session in scored):
			raise ValueError("Z-score scored sessions must be in the bounded input scope")
		if tuple(sorted(scored, key=lambda session: session.sequence)) != scored or len(set(scored)) != len(scored):
			raise ValueError("Z-score scored sessions must be chronological and unique")
		object.__setattr__(self, "scored_sessions", scored)


@dataclass(frozen=True)
class LoadedZScoreInputs:
	query: ZScoreInputQuery
	input: ZScoreInput
	dependencies: tuple[ZScoreDependencySnapshot, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.query, ZScoreInputQuery) or not isinstance(self.input, ZScoreInput) or not isinstance(self.dependencies, tuple):
			raise ValueError("Invalid loaded z-score inputs")
		if len(self.dependencies) != len(self.query.scope.sessions):
			raise ValueError("Z-score input dependencies must cover the bounded scope")
		if any(dependency.session != session for dependency, session in zip(self.dependencies, self.query.scope.sessions, strict=True)):
			raise ValueError("Z-score dependencies must preserve session order")


class ZScoreRepository(Protocol):
	def list_ticker_ids(self) -> tuple[int, ...]: ...

	def load_inputs(self, query: ZScoreInputQuery) -> LoadedZScoreInputs: ...

	def publish(
		self,
		definition: ZScoreSeriesDefinition,
		result: ZScoreResult,
		dependencies: tuple[ZScoreDependencySnapshot, ...],
		computed_at: datetime,
	) -> None: ...

	def read_verified(self, definition: ZScoreSeriesDefinition, dependency: ZScoreDependencySnapshot) -> VerifiedZScoreRead: ...
