from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig, SourceIdentity
from stockguidance.config.validation import require_aware_datetime, require_date, require_enum
from stockguidance.market_data.records import DailyBar


class ResponseState(StrEnum):
	COMPLETE = "complete"
	NO_TRADING = "no_trading"
	UNVERIFIED = "unverified"
	NOT_YET_PUBLISHED = "not_yet_published"


class CapabilityState(StrEnum):
	VERIFIED = "verified"
	UNKNOWN = "unknown"
	STALE = "stale"
	ENTITLEMENT_LIMIT = "entitlement_limit"
	AUTH_ERROR = "auth_error"
	RATE_LIMITED = "rate_limited"
	TRANSIENT_ERROR = "transient_error"
	DELAYED = "delayed"
	EMPTY = "empty"
	INTERIOR_GAP = "interior_gap"


@dataclass(frozen=True)
class ProviderCapability:
	"""Source-scoped capability evidence, never inferred from bar responses."""

	source: SourceIdentity
	state: CapabilityState
	checked_at: datetime
	provenance: str
	detail: str
	earliest_session: date | None = None
	latest_session: date | None = None
	freshness: timedelta | None = None
	earliest_observed_success: date | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceIdentity):
			raise ValueError("Provider capability requires a SourceIdentity")
		require_enum(self.state, CapabilityState, "provider capability state")
		require_aware_datetime(self.checked_at, "capability check time")
		if not self.provenance or not isinstance(self.provenance, str):
			raise ValueError("Capability provenance must be a nonempty string")
		if not self.detail or not isinstance(self.detail, str):
			raise ValueError("Capability detail must be a nonempty string")
		for name, value in (("earliest_session", self.earliest_session), ("latest_session", self.latest_session), ("earliest_observed_success", self.earliest_observed_success)):
			if value is not None:
				require_date(value, name)
		if self.earliest_session is not None and self.latest_session is not None and self.earliest_session > self.latest_session:
			raise ValueError("Provider capability earliest session must not follow latest session")
		if self.freshness is not None and (not isinstance(self.freshness, timedelta) or self.freshness < timedelta(0)):
			raise ValueError("Capability freshness must be a nonnegative timedelta or unknown")
		if self.state is CapabilityState.VERIFIED and (self.earliest_session is None or self.latest_session is None or self.freshness is None):
			raise ValueError("Verified provider capability requires bounded dates and freshness")

	def is_fresh_at(self, as_of: datetime) -> bool:
		require_aware_datetime(as_of, "capability freshness cutoff")
		return self.state is CapabilityState.VERIFIED and self.checked_at <= as_of and self.freshness is not None and self.checked_at + self.freshness >= as_of


class ProviderCapabilityAdapter(Protocol):
	def discover_capability(self, source: SourceIdentity, checked_at: datetime) -> ProviderCapability: ...


def unknown_capability(source: SourceIdentity, checked_at: datetime, detail: str = "Provider capability metadata is unavailable") -> ProviderCapability:
	return ProviderCapability(source, CapabilityState.UNKNOWN, checked_at, "adapter-capability", detail)


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
