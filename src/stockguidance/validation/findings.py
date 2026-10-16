from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from stockguidance.config.validation import require_date, require_enum, require_text


class FindingCategory(StrEnum):
	MISSING = "missing"
	INCOMPLETE = "incomplete"
	INVALID = "invalid"
	UNVERIFIED = "unverified"
	EXPECTED_ABSENCE = "expected_absence"
	NOT_YET_AVAILABLE = "not_yet_available"
	NOT_ASSESSED = "not_assessed"
	FAILED = "failed"


class ValidationCheck(StrEnum):
	SCHEMA = "schema"
	SESSION = "session"
	SOURCE = "source_manifest"
	RAW_KEYS = "raw_keys"
	RAW_VALUES = "raw_values"
	IMPORT = "import_receipt"
	DUPLICATE = "duplicate"
	ORPHAN = "orphan"
	DERIVED = "legacy_derived"
	HISTORY = "predecessor_history"
	OUTCOME = "legacy_outcome"
	TARGET = "close_to_close_target"
	FEATURE = "previous_close_features"
	OPENING = "entry_open_observations"


@dataclass(frozen=True)
class ValidationFinding:
	category: FindingCategory
	check: ValidationCheck
	detail: str
	session_date: date | None = None
	tickers: tuple[str, ...] = ()
	record_ids: tuple[int, ...] = ()
	dependency_session_date: date | None = None

	def __post_init__(self) -> None:
		require_enum(self.category, FindingCategory, "finding category")
		require_enum(self.check, ValidationCheck, "validation check")
		require_text(self.detail, "finding detail")
		if self.session_date is not None:
			require_date(self.session_date, "finding session")
		if self.dependency_session_date is not None:
			require_date(self.dependency_session_date, "finding dependency session")
		if not isinstance(self.tickers, tuple) or any(not isinstance(ticker, str) or not ticker for ticker in self.tickers):
			raise ValueError("Finding tickers must be an immutable text layout")
		if not isinstance(self.record_ids, tuple) or any(type(record_id) is not int for record_id in self.record_ids):
			raise ValueError("Finding record IDs must be integers")

	@property
	def blocking(self) -> bool:
		return self.category in {FindingCategory.MISSING, FindingCategory.INCOMPLETE, FindingCategory.INVALID, FindingCategory.UNVERIFIED, FindingCategory.FAILED}
