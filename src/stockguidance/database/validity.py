from dataclasses import dataclass
from datetime import date

from stockguidance.config.source import SourceConfig


MINIMUM_VALID_SESSION = date(2006, 11, 16)


def enforce_minimum_valid_session(source: SourceConfig, first_session: date) -> None:
	if not isinstance(first_session, date):
		raise ValueError("Minimum valid session requires a date")
	if source.data_configuration.source_family == "polygon_massive" and first_session < MINIMUM_VALID_SESSION:
		raise ValueError(f"Production data access cannot begin before {MINIMUM_VALID_SESSION.isoformat()}")


class DataValidityWarning(UserWarning):
	pass


@dataclass(frozen=True)
class DataValidityNotice:
	window: int
	family: str
	session_date: date
	status: int
	row_count: int

	@property
	def detail(self) -> str:
		return f"{self.family} window {self.window} has {self.row_count} non-ready row(s) with status {self.status} on {self.session_date.isoformat()}"
