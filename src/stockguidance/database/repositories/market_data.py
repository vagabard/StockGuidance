from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime, require_positive_int
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.legacy_derived import LegacyDerivedRow, derived_id
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.market_data.records import DailyBar, records_id


@dataclass(frozen=True)
class ImportReceipt:
	raw_id: str
	available_at: datetime
	derived_dependency_id: str | None = None
	derived_content_id: str | None = None

	def __post_init__(self) -> None:
		require_aware_datetime(self.available_at, "import availability")
		for identity in (self.raw_id, self.derived_dependency_id, self.derived_content_id):
			if identity is not None and (not isinstance(identity, str) or len(identity) != 64 or any(character not in "0123456789abcdef" for character in identity)):
				raise ValueError("Invalid import revision identity")
		if (self.derived_dependency_id is None) != (self.derived_content_id is None):
			raise ValueError("Derived verification requires dependency and content identities")


@dataclass(frozen=True)
class SessionSnapshot:
	session: TradingSession
	date_id: int
	records: tuple[DailyBar, ...]
	derived: tuple[LegacyDerivedRow, ...]
	receipt: ImportReceipt | None

	def __post_init__(self) -> None:
		require_positive_int(self.date_id, "snapshot date_id")
		if not isinstance(self.session, TradingSession) or not isinstance(self.records, tuple) or not isinstance(self.derived, tuple):
			raise ValueError("Invalid database snapshot layout")
		if any(not isinstance(bar, DailyBar) for bar in self.records) or any(not isinstance(row, LegacyDerivedRow) for row in self.derived):
			raise ValueError("Invalid database snapshot records")
		if len({bar.ticker for bar in self.records}) != len(self.records) or len({row.ticker for row in self.derived}) != len(self.derived):
			raise ValueError("Duplicate database snapshot keys")
		if self.receipt is not None and (not isinstance(self.receipt, ImportReceipt) or self.receipt.available_at < self.session.closes_at):
			raise ValueError("Invalid database availability receipt")

	@property
	def raw_verified(self) -> bool:
		return self.receipt is not None and self.receipt.raw_id == self.raw_content_id

	@cached_property
	def raw_content_id(self) -> str:
		return records_id(self.records)

	@cached_property
	def derived_content_id(self) -> str:
		return derived_id(self.derived)

	def derived_verified(self, dependency: str) -> bool:
		return self.raw_verified and self.receipt is not None and self.receipt.derived_dependency_id == dependency and self.receipt.derived_content_id == self.derived_content_id and {row.ticker for row in self.derived} == {bar.ticker for bar in self.records}


@dataclass(frozen=True)
class DerivedUpdate:
	snapshot: SessionSnapshot
	rows: tuple[LegacyDerivedRow, ...]
	dependency_id: str


class MarketDataRepository(Protocol):
	def inspect(self, query: SessionQuery) -> tuple[SessionSnapshot, ...]: ...
	def import_session(self, query: SessionQuery, download: DownloadedSession) -> None: ...
	def replace_derived(self, query: SessionQuery, update: DerivedUpdate) -> None: ...
