from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN, localcontext


CENT = Decimal("0.01")
SHARE = Decimal("0.00000001")


def require_decimal(value: Decimal, name: str, *, signed: bool = False) -> None:
	if not isinstance(value, Decimal) or not value.is_finite() or (not signed and value < 0):
		raise ValueError(f"{name} must be a finite {'signed' if signed else 'nonnegative'} Decimal")


def money(value: Decimal, *, rounding: str = ROUND_HALF_EVEN) -> Decimal:
	require_decimal(value, "money", signed=True)
	with localcontext() as context:
		context.prec = 38
		return value.quantize(CENT, rounding=rounding)


def require_money(value: Decimal, name: str, *, signed: bool = False) -> None:
	require_decimal(value, name, signed=signed)
	if money(value) != value:
		raise ValueError(f"{name} must use whole cents")


@dataclass(frozen=True)
class ExecutionPolicy:
	fee_per_fill: Decimal = Decimal("0.00")

	def __post_init__(self) -> None:
		require_money(self.fee_per_fill, "fee_per_fill")

	def size(self, budget: Decimal, price: Decimal) -> tuple[Decimal, Decimal]:
		require_money(budget, "order budget")
		require_decimal(price, "execution price")
		if price <= 0:
			raise ValueError("Execution price must be positive")
		if budget <= self.fee_per_fill:
			return Decimal(0), Decimal(0)
		with localcontext() as context:
			context.prec = 38
			quantity = ((budget - self.fee_per_fill) / price).quantize(SHARE, rounding=ROUND_DOWN)
			debit = money(quantity * price, rounding=ROUND_CEILING) + self.fee_per_fill
		return quantity, debit
