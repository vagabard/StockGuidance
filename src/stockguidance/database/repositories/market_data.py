from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from typing import Protocol

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_aware_datetime, require_positive_int
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.legacy_derived import LegacyDerivedRow, derived_id
from stockguidance.market_data.providers.contracts import DownloadedSession
from stockguidance.market_data.records import DailyBar, records_id
from stockguidance.market_data.quality import QualityRecord
from stockguidance.validation.findings import ValidationFinding


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
	quality: tuple[QualityRecord, ...] = ()
	original_content_id: str | None = None

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
		return self.receipt is not None and self.receipt.raw_id == (self.original_content_id or self.raw_content_id)

	@cached_property
	def raw_content_id(self) -> str:
		return records_id(self.records)

	@cached_property
	def derived_content_id(self) -> str:
		return derived_id(self.derived)

	def derived_verified(self, dependency: str) -> bool:
		return self.raw_verified and self.receipt is not None and self.receipt.derived_dependency_id == dependency and self.receipt.derived_content_id == self.derived_content_id and {row.ticker for row in self.derived} == {bar.ticker for bar in self.records}


@dataclass(frozen=True)
class SessionMetadata:
	session: TradingSession
	date_id: int
	receipt: ImportReceipt | None
	raw_count: int | None
	derived_count: int | None
	raw_keys: tuple[str, ...]

	def __post_init__(self) -> None:
		require_positive_int(self.date_id, "session metadata date_id")
		if not isinstance(self.session, TradingSession):
			raise ValueError("Invalid session metadata session")
		if self.receipt is not None and (not isinstance(self.receipt, ImportReceipt) or self.receipt.available_at < self.session.closes_at):
			raise ValueError("Invalid session metadata receipt")
		if any(value is not None and (type(value) is not int or value < 0) for value in (self.raw_count, self.derived_count)):
			raise ValueError("Invalid session metadata counts")
		if not isinstance(self.raw_keys, tuple) or any(not isinstance(key, str) for key in self.raw_keys) or len(set(self.raw_keys)) != len(self.raw_keys):
			raise ValueError("Invalid session metadata keys")


@dataclass(frozen=True)
class DerivedUpdate:
	snapshot: SessionSnapshot
	rows: tuple[LegacyDerivedRow, ...]
	dependency_id: str


class MarketDataRepository(Protocol):
	def reconcile_quality(self, query: SessionQuery) -> tuple[date, ...]: ...
	def reconcile_quality_dependencies(self, query: SessionQuery, affected: frozenset[date]) -> tuple[date, ...]: ...
	def scan_quality(self, query: SessionQuery) -> tuple[date, ...]: ...
	def has_orphans(self, query: SessionQuery) -> bool: ...
	def inspect_metadata(self, query: SessionQuery, *, force_verify: bool = False) -> tuple[SessionMetadata, ...]: ...
	def cleanup_placeholders(self, query: SessionQuery) -> tuple[date, ...]: ...
	def inspect(self, query: SessionQuery) -> tuple[SessionSnapshot, ...]: ...
	def inspect_integrity(self, query: SessionQuery) -> tuple[ValidationFinding, ...]: ...
	def import_session(self, query: SessionQuery, download: DownloadedSession) -> None: ...
	def replace_derived(self, query: SessionQuery, update: DerivedUpdate) -> None: ...
