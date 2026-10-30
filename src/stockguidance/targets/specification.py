from dataclasses import dataclass
from enum import StrEnum

from stockguidance.config.validation import require_enum


class HoldingHorizon(StrEnum):
	NEXT_TRADING_SESSION = "next_trading_session"


class ExecutionPrice(StrEnum):
	CLOSE = "close"


class TargetTransform(StrEnum):
	RAW_RETURN = "raw_return"


class TargetKind(StrEnum):
	"""Versioned target meanings introduced by the configurable workflow."""

	RAW_RETURN = "raw_return"
	RETURN_ZSCORE_PER_TICKER = "return_zscore_per_ticker"
	RETURN_ZSCORE_CROSS_SECTIONAL = "return_zscore_cross_sectional"


@dataclass(frozen=True)
class TargetSpec:
	"""The unchanged v1 raw close-to-close target contract."""

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

	@property
	def kind(self) -> TargetKind:
		return TargetKind.RAW_RETURN

	@property
	def version(self) -> str:
		return "raw_return-v1"

	@property
	def rolling_window_sessions(self) -> int | None:
		return None

	@property
	def target_identity(self) -> str:
		return self.version

	@property
	def baseline_offsets(self) -> tuple[int, ...]:
		return ()

	def to_mapping(self) -> dict[str, object]:
		return {
			"schema": "targets-v1",
			"horizon": self.horizon.value,
			"entry_price": self.entry_price.value,
			"exit_price": self.exit_price.value,
			"transform": self.transform.value,
		}


@dataclass(frozen=True)
class TargetSpecV2(TargetSpec):
	"""Explicit raw, per-ticker, or cross-sectional return target semantics.

	The inherited fields retain the v1 timing contract.  ``kind`` is deliberately
	separate from ``TargetTransform`` so a v1 artifact cannot be relabeled as a
	transformed target by changing one string in its metadata.
	"""

	kind: TargetKind = TargetKind.RAW_RETURN
	rolling_window_sessions: int | None = None

	def __post_init__(self) -> None:
		super().__post_init__()
		if not isinstance(self.kind, TargetKind):
			try:
				object.__setattr__(self, "kind", TargetKind(str(self.kind)))
			except ValueError:
				raise ValueError("Unsupported target kind") from None
		if self.transform is not TargetTransform.RAW_RETURN:
			raise ValueError("TargetSpecV2 uses kind rather than a legacy transform")
		if self.kind is TargetKind.RETURN_ZSCORE_PER_TICKER:
			if self.rolling_window_sessions not in (20, 60):
				raise ValueError("Per-ticker return z-score requires a 20- or 60-session window")
		elif self.rolling_window_sessions is not None:
			raise ValueError("A rolling target window is only valid for per-ticker z-scores")

	@property
	def version(self) -> str:
		if self.kind is TargetKind.RAW_RETURN:
			return "raw_return-v2"
		if self.kind is TargetKind.RETURN_ZSCORE_PER_TICKER:
			assert self.rolling_window_sessions is not None
			return f"return_zscore_per_ticker-v2-window-{self.rolling_window_sessions}"
		return "return_zscore_cross_sectional-v2"

	@property
	def target_identity(self) -> str:
		return self.version

	@property
	def baseline_offsets(self) -> tuple[int, ...]:
		if self.kind is not TargetKind.RETURN_ZSCORE_PER_TICKER:
			return ()
		assert self.rolling_window_sessions is not None
		return tuple(range(-self.rolling_window_sessions, 1))

	def to_mapping(self) -> dict[str, object]:
		return {
			"schema": "targets-v2",
			"kind": self.kind.value,
			"horizon": self.horizon.value,
			"entry_price": self.entry_price.value,
			"exit_price": self.exit_price.value,
			"return_formula": self.raw_formula,
			"rolling_window_sessions": self.rolling_window_sessions,
		}


TargetSpecification = TargetSpec | TargetSpecV2
