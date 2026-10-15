from dataclasses import dataclass
import math

from stockguidance.calendar.sessions import TradingSession, require_next_session
from stockguidance.database.repositories.closes import CloseData
from stockguidance.market_data.closes import CloseIssue, valid_close
from stockguidance.targets.specification import TargetSpec


def close_to_close_return(entry_close: float, exit_close: float, specification: TargetSpec) -> float:
	if not isinstance(specification, TargetSpec):
		raise ValueError("Close target requires an explicit target specification")
	result = valid_close(exit_close) / valid_close(entry_close) - 1
	if not math.isfinite(result):
		raise ValueError("Close target is not finite")
	return result


@dataclass(frozen=True)
class TargetValues:
	raw: tuple[float | None, ...]
	transformed: tuple[float | None, ...]
	issues: tuple[CloseIssue, ...] = ()

	def __post_init__(self) -> None:
		if not isinstance(self.raw, tuple) or not self.raw or self.raw != self.transformed or not isinstance(self.issues, tuple):
			raise ValueError("Raw-return targets require identical immutable raw/transformed layouts")
		if any(not isinstance(issue, CloseIssue) for issue in self.issues):
			raise ValueError("Invalid target issues")
		if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < -1) for value in self.raw):
			raise ValueError("Invalid raw close return")

	@property
	def ready(self) -> bool:
		return not self.issues and all(value is not None for value in self.raw)


def construct_targets(specification: TargetSpec, ticker_ids: tuple[int, ...], entry: TradingSession, exit_session: TradingSession, data: CloseData) -> TargetValues:
	require_next_session(entry, exit_session)
	by_key = {(item.timing.session, item.ticker_id): item for item in data.observations}
	issues = tuple(issue for issue in data.issues if issue.session in {entry, exit_session})
	values: list[float | None] = []
	for ticker_id in ticker_ids:
		entry_price, exit_price = by_key.get((entry, ticker_id)), by_key.get((exit_session, ticker_id))
		if entry_price is None or exit_price is None:
			if not any(issue.ticker_id == ticker_id for issue in issues):
				raise ValueError("Target result omits a required session/ticker key")
			values.append(None)
		else:
			values.append(close_to_close_return(entry_price.close, exit_price.close, specification))
	return TargetValues(tuple(values), tuple(values), issues)
