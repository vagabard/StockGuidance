from dataclasses import dataclass
from enum import StrEnum

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.validation import require_enum


class AllocationPolicy(StrEnum):
	ALTERNATE_SESSIONS = "alternate_sessions"
	STAGGERED_HALVES = "staggered_halves"


class CashRestRule(StrEnum):
	NEXT_SESSION_AFTER_SALE = "next_session_after_sale"


@dataclass(frozen=True)
class AllocationSpec:
	"""Anchor buys to session ordinals, including skipped or failed decisions.

	Alternate sessions uses one allocation buying at anchor, anchor+2, ... .
	Staggered halves splits initial capital once into independent A/B balances:
	A buys at anchor, anchor+2, ...; B at anchor+1, anchor+3, ... . Each sells
	at the next close and reinvests its own proceeds after the overnight rest.
	Balances are never rebalanced or transferred. State and planning live here.
	"""

	policy: AllocationPolicy
	anchor_session: TradingSession
	cash_rest: CashRestRule = CashRestRule.NEXT_SESSION_AFTER_SALE

	def __post_init__(self) -> None:
		require_enum(self.policy, AllocationPolicy, "policy")
		require_enum(self.cash_rest, CashRestRule, "cash_rest")
		if not isinstance(self.anchor_session, TradingSession):
			raise ValueError("anchor_session must be a TradingSession")

	@property
	def allocation_count(self) -> int:
		return 2 if self.policy is AllocationPolicy.STAGGERED_HALVES else 1

	@property
	def buy_cadence_sessions(self) -> int:
		"""Each independent allocation buys every two trading sessions."""
		return 2
