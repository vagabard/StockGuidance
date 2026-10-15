from dataclasses import dataclass
from enum import StrEnum

from stockguidance.database.repositories.sessions import StoredSession


class DateBoundType(StrEnum):
	START = "start"
	END = "end"


@dataclass(frozen=True)
class DateBoundResolver:
	"""Resolve within supplied, already bounded chronology; never query a DB.

	+N indexes from the start; -N indexes from the end, with -1 and -0
	selecting the last row. Absolute IDs must exist in this supplied scope.
	Stored neighbors do not prove exchange adjacency across a coverage gap.
	"""

	rows: tuple[StoredSession, ...]

	def __post_init__(self) -> None:
		if not isinstance(self.rows, tuple) or not self.rows:
			raise ValueError("Date resolution requires nonempty bounded session rows")
		if any(not isinstance(row, StoredSession) for row in self.rows):
			raise ValueError("Date resolution requires StoredSession values")
		dates = tuple(row.session_date for row in self.rows)
		if dates != tuple(sorted(set(dates))) or len({row.date_id for row in self.rows}) != len(self.rows):
			raise ValueError("Stored session chronology must have unique increasing dates and unique IDs")

	def resolve_bound(self, raw_bound: str | None, which: DateBoundType) -> int:
		if not isinstance(which, DateBoundType):
			raise ValueError("Bound kind must be a DateBoundType")
		if raw_bound is None:
			return self.rows[0 if which is DateBoundType.START else -1].date_id
		if not isinstance(raw_bound, str) or not raw_bound.strip():
			raise ValueError("Date bound must be an integer string or omitted")
		bound = raw_bound.strip()
		value = int(bound)
		if bound.startswith(("+", "-")):
			index = value if bound.startswith("+") else len(self.rows) + min(value, -1)
			if not 0 <= index < len(self.rows):
				raise ValueError("Relative date bound exceeds the supplied history window")
			return self.rows[index].date_id
		if value not in {row.date_id for row in self.rows}:
			raise ValueError("Absolute date ID is not present in the supplied history window")
		return value

	def offset_date_id(self, base_id: int, offset: int) -> int:
		if type(base_id) is not int or type(offset) is not int:
			raise ValueError("Date ID and offset must be integers")
		indices = {row.date_id: index for index, row in enumerate(self.rows)}
		if base_id not in indices or not 0 <= indices[base_id] + offset < len(self.rows):
			raise ValueError("Date offset exceeds the supplied history window")
		return self.rows[indices[base_id] + offset].date_id
