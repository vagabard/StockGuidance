from dataclasses import dataclass
from enum import StrEnum

from stockguidance.config.validation import require_enum


class HoldingHorizon(StrEnum):
	NEXT_TRADING_SESSION = "next_trading_session"


class ExecutionPrice(StrEnum):
	CLOSE = "close"


class TargetTransform(StrEnum):
	RAW_RETURN = "raw_return"


@dataclass(frozen=True)
class TargetSpec:
	"""No implicit ranking or normalization, and no open-to-close compatibility."""

	horizon: HoldingHorizon = HoldingHorizon.NEXT_TRADING_SESSION
	entry_price: ExecutionPrice = ExecutionPrice.CLOSE
	exit_price: ExecutionPrice = ExecutionPrice.CLOSE
	transform: TargetTransform = TargetTransform.RAW_RETURN

	def __post_init__(self) -> None:
		require_enum(self.horizon, HoldingHorizon, "horizon")
		require_enum(self.entry_price, ExecutionPrice, "entry_price")
		require_enum(self.exit_price, ExecutionPrice, "exit_price")
		require_enum(self.transform, TargetTransform, "transform")

	@property
	def raw_formula(self) -> str:
		return "close(T+1) / close(T) - 1"
