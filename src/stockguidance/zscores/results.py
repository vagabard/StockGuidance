import math
from dataclasses import dataclass
from enum import IntEnum

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_enum, require_positive_int, require_text
from stockguidance.zscores.specification import ZScoreSpecification


class ZScoreStatus(IntEnum):
	READY = 0
	INSUFFICIENT_HISTORY = 1
	MISSING_INPUT = 2
	UNVERIFIED_INPUT = 3
	INVALID_INPUT = 4
	ZERO_VARIANCE = 5


@dataclass(frozen=True)
class ZScoreRow:
	window: int
	session: TradingSession
	ticker_id: int
	score: float | None
	status: ZScoreStatus
	reason: str

	def __post_init__(self) -> None:
		if type(self.window) is not int or self.window < 2:
			raise ValueError("z-score row window must be an integer >= 2")
		if not isinstance(self.session, TradingSession):
			raise ValueError("z-score row requires a TradingSession")
		require_positive_int(self.ticker_id, "z-score row ticker_id")
		require_enum(self.status, ZScoreStatus, "z-score row status")
		require_text(self.reason, "z-score row reason")
		if self.status is ZScoreStatus.READY:
			if self.score is None or not math.isfinite(self.score):
				raise ValueError("ready z-score rows require a finite score")
		elif self.score is not None:
			raise ValueError("unready z-score rows must have a null score")

	@property
	def zscore(self) -> float | None:
		return self.score

	@property
	def status_code(self) -> int:
		return int(self.status)


@dataclass(frozen=True)
class ZScoreResult:
	specification: ZScoreSpecification
	input_sessions: tuple[TradingSession, ...]
	scored_sessions: tuple[TradingSession, ...]
	dependency_sessions: tuple[TradingSession, ...]
	rows: tuple[ZScoreRow, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.input_sessions, tuple) or not isinstance(self.scored_sessions, tuple) or not isinstance(self.dependency_sessions, tuple) or not isinstance(self.rows, tuple):
			raise ValueError("z-score result collections must be tuples")
		if any(not isinstance(row, ZScoreRow) for row in self.rows):
			raise ValueError("z-score result rows must be ZScoreRow values")

	def rows_for(self, window: int) -> tuple[ZScoreRow, ...]:
		return tuple(row for row in self.rows if row.window == window)

	def row(self, window: int, session: TradingSession, ticker_id: int) -> ZScoreRow:
		for candidate in self.rows:
			if candidate.window == window and candidate.session == session and candidate.ticker_id == ticker_id:
				return candidate
		raise KeyError((window, session.session_date, ticker_id))

	def status_counts(self, window: int | None = None) -> dict[ZScoreStatus, int]:
		counts = {status: 0 for status in ZScoreStatus}
		for row in self.rows if window is None else self.rows_for(window):
			counts[row.status] += 1
		return counts


ZScoreOutput = ZScoreResult
