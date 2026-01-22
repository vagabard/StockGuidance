import _pydatetime
from dataclasses import dataclass
from typing import Any
from datetime import date

@dataclass(frozen=True, init=False)
class TimeTrainingInstance:
	"""
	Represents a single training instance for a specific date.
	"""
	feature_vector: Any
	one_day_feature_vector_size: int
	date: date
	date_id: int
	training_label: Any
	num_days: int

	def __init__(
		self,
		*,
		feature_vector: Any,
		date: date,
		date_id: int,
		training_label: Any,
		num_days: int,
		one_day_feature_vector_size: int | None = None
	) -> None:
		object.__setattr__(self, 'feature_vector', feature_vector)
		object.__setattr__(self, 'date', date)
		object.__setattr__(self, 'date_id', date_id)
		object.__setattr__(self, 'training_label', training_label)
		object.__setattr__(self, 'num_days', num_days)

		if one_day_feature_vector_size is None:
			one_day_feature_vector_size = len(feature_vector) // num_days

		object.__setattr__(self, 'one_day_feature_vector_size', one_day_feature_vector_size)


	def getFeatureVectorForRelativeDay(self, relative_day: int) -> Any:
		"""
		Returns the part of the feature vector representing a specific relative day.
		0 returns the part representing just the instance date.
		1 returns the part representing 1 day before the instance date.
		2 returns 2 days prior.
		Negative numbers return an error.
		"""
		if relative_day < 0:
			raise ValueError("relative_day must be non-negative")
		if relative_day >= self.num_days:
			raise ValueError(f"relative_day {relative_day} is out of range (num_days={self.num_days})")

		# Assuming feature_vector is a 1D array or similar that can be sliced
		# features are concatenated [Day 0, Day 1, ..., Day N]
		day_size = len(self.feature_vector) // self.num_days
		start_idx = relative_day * day_size
		end_idx = (relative_day + 1) * day_size
		return self.feature_vector[start_idx:end_idx]

	@property
	def features(self) -> Any:
		"""Accessor for the feature vector."""
		return self.feature_vector

	@property
	def date_val(self) -> _pydatetime.date:
		"""Accessor for the date."""
		return self.date

	@property
	def date_id_val(self) -> int:
		"""Accessor for the date."""
		return self.date_id

	@property
	def label(self) -> Any:
		"""Accessor for the training label."""
		return self.training_label

	def to_dict(self) -> dict[str, Any]:
		"""
		Returns a dictionary representation of the instance.
		Converts feature_vector and training_label to lists if they have a tolist method.
		Converts date to ISO string.
		"""
		def _convert(val: Any) -> Any:
			if hasattr(val, 'tolist'):
				return val.tolist()
			return val

		return {
			'feature_vector': _convert(self.feature_vector),
			'one_day_feature_vector_size': self.one_day_feature_vector_size,
			'date': self.date.isoformat() if hasattr(self.date, 'isoformat') else str(self.date),
			'date_id': self.date_id,
			'training_label': _convert(self.training_label),
			'num_days': self.num_days
		}


