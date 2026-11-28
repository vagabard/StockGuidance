"""Shared bounds for prepared-history database reads."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryLoadOptions:
	ticker_batch_size: int = 1000
	fetch_rows: int = 8192
	max_array_bytes: int | None = None

	def __post_init__(self) -> None:
		for value in (self.ticker_batch_size, self.fetch_rows):
			if type(value) is not int or value < 1:
				raise ValueError("Data loading batch sizes must be positive integers")
		if self.max_array_bytes is not None and (type(self.max_array_bytes) is not int or self.max_array_bytes < 1):
			raise ValueError("Data loading memory budget must be a positive integer")

	@classmethod
	def decode(cls, value: object) -> "HistoryLoadOptions":
		if value is None:
			return cls()
		if not isinstance(value, dict) or set(value)-{"ticker_batch_size","fetch_rows","max_array_bytes"}:
			raise ValueError("Unsupported data_loading settings")
		return cls(value.get("ticker_batch_size",1000),value.get("fetch_rows",8192),value.get("max_array_bytes"))
