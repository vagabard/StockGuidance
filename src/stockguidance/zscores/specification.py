from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from stockguidance.config.validation import require_enum, require_text


CALCULATION_VERSION: Final = "rolling-population-zscore-v1"
FLOAT32_PRECISION: Final = "float32"
DELTA_DEFINITION: Final = "close_ratio_to_prior_minus_one"


class ZScoreFamily(StrEnum):
	PRICE = "price"
	PRICE_DELTA = "price_delta"


@dataclass(frozen=True)
class ZScoreSpecification:
	"""Immutable definition of one or more rolling z-score series."""

	family: ZScoreFamily
	windows: tuple[int, ...]
	calculation_version: str = CALCULATION_VERSION
	precision: str = FLOAT32_PRECISION
	delta_definition: str = "not_applicable"

	def __post_init__(self) -> None:
		require_enum(self.family, ZScoreFamily, "z-score family")
		if not isinstance(self.windows, tuple) or not self.windows:
			raise ValueError("z-score windows must be a nonempty tuple")
		if len(set(self.windows)) != len(self.windows):
			raise ValueError("z-score windows must be distinct")
		for window in self.windows:
			if type(window) is not int or window < 2:
				raise ValueError("z-score windows must be integers >= 2")
		require_text(self.calculation_version, "z-score calculation version")
		if self.precision != FLOAT32_PRECISION:
			raise ValueError("z-score precision must be float32")
		expected_delta_definition = DELTA_DEFINITION if self.family is ZScoreFamily.PRICE_DELTA else "not_applicable"
		if self.delta_definition != expected_delta_definition:
			raise ValueError("z-score delta definition does not match its family")

	@property
	def dependency_order(self) -> tuple[str, ...]:
		if self.family is ZScoreFamily.PRICE:
			return ("session", "ticker_id", "verified_close", "actual_receipt")
		return ("session", "ticker_id", "verified_close_ratio_to_prior", "prior_verified_close", "actual_receipt")

	def required_value_count(self, window: int) -> int:
		if type(window) is not int or window < 2:
			raise ValueError("z-score window must be an integer >= 2")
		return window

	def required_close_count(self, window: int) -> int:
		return self.required_value_count(window) + (1 if self.family is ZScoreFamily.PRICE_DELTA else 0)


ZScoreSpec = ZScoreSpecification
