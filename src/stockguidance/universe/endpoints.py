"""Endpoint membership and deterministic percentile selection."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class EndpointSelection:
	tickers: tuple[str, ...] | None = None
	ticker_ids: tuple[int, ...] | None = None
	trim_percent: Decimal = Decimal(0)
	sample_sessions: int | None = None

	def __post_init__(self) -> None:
		p = Decimal(str(self.trim_percent))
		if not p.is_finite() or not Decimal(0) <= p < Decimal(100):
			raise ValueError("Dollar-volume trim must be finite and between 0 and 100 (exclusive)")
		if self.sample_sessions is not None and (type(self.sample_sessions) is not int or self.sample_sessions < 2):
			raise ValueError("Volume sampling requires at least two sessions")
		if self.tickers is not None and self.ticker_ids is not None:
			raise ValueError("Supply symbols or IDs, not both")
		for values in (self.tickers,self.ticker_ids):
			if values is not None and (not values or len(set(values))!=len(values)):
				raise ValueError("Explicit tickers must be nonempty and unique")
		if self.ticker_ids is not None and any(type(v) is not int or v<=0 for v in self.ticker_ids):
			raise ValueError("Ticker IDs must be positive integers")
		if self.tickers is not None and any(not isinstance(v,str) or not v.strip() for v in self.tickers):
			raise ValueError("Ticker symbols must be nonempty strings")
		if (self.tickers is not None or self.ticker_ids is not None) and p:
			raise ValueError("Explicit ticker lists cannot be combined with percentile trimming")
		object.__setattr__(self,"trim_percent",p)


def evenly_spaced(values: tuple[T,...], count: int | None) -> tuple[T,...]:
	if count is None or count >= len(values):
		return values
	if count<2:
		raise ValueError("At least two sampling sessions are required")
	return tuple(values[i*(len(values)-1)//(count-1)] for i in range(count))


def trim_candidates(candidates: tuple[int,...], measurements: dict[int,tuple[Decimal,int]], percentage: Decimal) -> tuple[tuple[int,...],tuple[int,...],tuple[int,...]]:
	"""Return kept, percentile exclusions, and missing-measurement exclusions."""
	if not percentage:
		return candidates,(),()
	missing = tuple(v for v in candidates if v not in measurements or measurements[v][1]<=0)
	missing_ids = set(missing)
	ranked = sorted((v for v in candidates if v not in missing_ids),key=lambda v:(measurements[v][0],v))
	n = int((Decimal(len(ranked))*percentage/100).to_integral_value(rounding=ROUND_FLOOR))
	removed = tuple(ranked[:n])
	excluded = set(removed) | missing_ids
	kept = tuple(v for v in candidates if v not in excluded)
	return kept,removed,missing


def filter_complete_history(candidates: tuple[int,...], observed_sessions: dict[int,int], required_sessions: int) -> tuple[tuple[int,...],tuple[int,...]]:
	"""Require usable observations on every requested exchange session, preserving order."""
	if required_sessions <= 0:
		raise ValueError("History coverage requires at least one session")
	kept = tuple(v for v in candidates if observed_sessions.get(v,0) == required_sessions)
	excluded = tuple(v for v in candidates if observed_sessions.get(v,0) != required_sessions)
	return kept,excluded
