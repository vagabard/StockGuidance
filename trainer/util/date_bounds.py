from __future__ import annotations

from enum import Enum
from typing import Optional
from database import database_date_id_queries
from database.database_date_id_queries import get_offset_date_id


class DateBoundType(Enum):
	START = 1
	END = 2


class DateBoundResolver:
	"""Resolve user-specified train/validation window bounds to concrete date_ids.

	This is extracted from the `_resolve_bound` helper that previously lived as a
	nested function inside `train_pytorch_network._run_one`.
	"""

	def __init__(self, feature_vector_day_size_buffer:Optional[int]=None):
		(lowest_bound, highest_bound) = database_date_id_queries.load_date_id_range(dbinfo_file="dbinfo.id", feature_vector_day_size_buffer=feature_vector_day_size_buffer)
		self.lowest_date_id = lowest_bound
		self.highest_date_id = highest_bound
		print(f"Loaded date range: {lowest_bound} to {highest_bound}")

	def resolve_bound(self, raw_bound: str, which: DateBoundType) -> int:
		"""Resolve a user-specified bound to an absolute ``date_id``.

		``raw_bound`` may be ``None`` (use first/last), an absolute integer date_id,
		or a relative index in string form:

		* "+N" -> date at index N from the *start* (0-based) of ``usable_dates``
		* "-N" -> date at index ``len(usable_dates) - N`` from the *start*, i.e.
		  N steps back from the last available date.
		"""

		#print(raw_bound, "   is type ", type(raw_bound))

		if raw_bound is None:
			return int(self.lowest_date_id if which == DateBoundType.START else self.highest_date_id)
		raw_bound = raw_bound.lower().strip()
		if raw_bound.startswith("+") or raw_bound.startswith("-"):
			relative_sign = raw_bound[0]
			offset = int(raw_bound)
			if relative_sign == '+':
				# '+N' (or 'N' with leading '+'): index from start, 0-based
				offset_date_id = get_offset_date_id(base_value=self.lowest_date_id, offset=offset)
			else:
				# '-N': N steps back from the end
				offset_date_id = get_offset_date_id(base_value=self.highest_date_id, offset=offset)
			return int(offset_date_id)
		else :
			return int(raw_bound)
		# Normalise to string for parsing
		# Relative from start or end


__all__ = ["DateBoundResolver", "DateBoundType"]
