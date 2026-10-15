from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from stockguidance.config.validation import require_enum, require_text
from stockguidance.market_data.intervals import ObservationInterval, require_daily_interval


class PriceAdjustment(StrEnum):
	ADJUSTED = "adjusted"
	UNADJUSTED = "unadjusted"


@dataclass(frozen=True)
class SourceIdentity:
	"""Non-secret provider request scope, including the entire requested universe."""

	provider: str
	endpoint: str
	market: str
	universe: str
	adjustment: PriceAdjustment
	interval: ObservationInterval = ObservationInterval.DAILY

	def __post_init__(self) -> None:
		for name in ("provider", "endpoint", "market", "universe"):
			require_text(getattr(self, name), name)
		require_enum(self.adjustment, PriceAdjustment, "adjustment")
		require_daily_interval(self.interval)


@dataclass(frozen=True)
class SourceConfig:
	identity: SourceIdentity
	data_root: Path
	database_config: Path
	credential_reference: str | None = field(default=None, repr=False)

	def __post_init__(self) -> None:
		if not isinstance(self.identity, SourceIdentity):
			raise ValueError("identity must be a SourceIdentity")
		if not isinstance(self.data_root, Path) or not isinstance(self.database_config, Path):
			raise ValueError("data_root and database_config must be Paths")
		if self.credential_reference is not None:
			require_text(self.credential_reference, "credential_reference")
