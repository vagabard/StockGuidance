from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_aware_datetime, require_enum
from stockguidance.market_data.records import DailyBar


class ResponseState(StrEnum):
	COMPLETE = "complete"
	NO_TRADING = "no_trading"
	UNVERIFIED = "unverified"
	NOT_YET_PUBLISHED = "not_yet_published"


@dataclass(frozen=True)
class ProviderResponse:
	state: ResponseState
	records: tuple[DailyBar, ...]
	http_status: int
	provider_status: str
	counts: tuple[tuple[str, int], ...] = ()
	has_more: bool = False
	evidence: str | None = None
	source_timestamps: tuple[tuple[str, int], ...] = ()

	def __post_init__(self) -> None:
		require_enum(self.state, ResponseState, "response state")
		if not isinstance(self.records, tuple) or any(not isinstance(bar, DailyBar) for bar in self.records):
			raise ValueError("Provider records must be an immutable validated layout")
		if len({bar.ticker for bar in self.records}) != len(self.records):
			raise ValueError("Duplicate source record keys")
		if type(self.http_status) is not int or self.http_status != 200 or self.provider_status not in {"OK", "DELAYED"}:
			raise ValueError("Successful HTTP and provider status required")
		if not isinstance(self.counts, tuple) or len(dict(self.counts)) != len(self.counts) or any(name not in {"resultsCount", "queryCount", "count"} or type(count) is not int or count < 0 for name, count in self.counts):
			raise ValueError("Invalid provider counts")
		if type(self.has_more) is not bool:
			raise ValueError("Continuation flag must be boolean")
		keys = {bar.ticker for bar in self.records}
		if not isinstance(self.source_timestamps, tuple) or len(dict(self.source_timestamps)) != len(self.source_timestamps) or any(ticker not in keys or type(timestamp) is not int or timestamp < 0 for ticker, timestamp in self.source_timestamps):
			raise ValueError("Invalid source observation timestamps")
		if self.state is ResponseState.COMPLETE and (not self.records or self.has_more or self.provider_status != "OK" or dict(self.counts).get("resultsCount") != len(self.records)):
			raise ValueError("Complete response requires exact count and exhausted continuation")
		if self.state is ResponseState.NO_TRADING and (self.records or self.has_more or not self.evidence):
			raise ValueError("No trading requires explicit authoritative evidence and no records")


@dataclass(frozen=True)
class DownloadedSession:
	session: TradingSession
	response: ProviderResponse
	available_at: datetime

	def __post_init__(self) -> None:
		require_aware_datetime(self.available_at, "actual receipt time")
		if self.available_at < self.session.closes_at:
			raise ValueError("Completed daily data cannot precede the close")


class MarketDataProvider(Protocol):
	def fetch(self, source: SourceConfig, session: TradingSession) -> ProviderResponse: ...
