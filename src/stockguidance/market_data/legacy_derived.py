from dataclasses import dataclass
from decimal import Decimal

from stockguidance.market_data.identity import content_id
from stockguidance.market_data.records import DailyBar


LEGACY_DERIVED_SPEC = "legacy-open-to-close-v1"
DERIVED_STUDY_SPEC = "legacy-open-to-close-v1-study"
DERIVED_COLUMNS = (
	"open_ratio_to_prior", "high_ratio_to_prior", "low_ratio_to_prior", "close_ratio_to_prior",
	"volume_ratio_to_prior", "vwap_ratio_to_prior", "transactions_ratio_to_prior", "change_in_price_over_day",
	"next_day_change_in_price", "zero_centered_next_day_price_change", "next_day_price_change_to_extremes_0_1",
	"next_day_raise1_lower0", "next_day_top100_raise1_lower0", "zero_center_extremes_neg1_pos1",
	"zero_center_top100_1_other_neg1", "zero_center_raise1_lower_neg1",
)


@dataclass(frozen=True)
class LegacyDerivedRow:
	ticker: str
	values: tuple[float | None, ...]

	def __post_init__(self) -> None:
		if len(self.values) != len(DERIVED_COLUMNS):
			raise ValueError("Invalid legacy derived layout")


def _ratio(numerator: Decimal | int | None, denominator: Decimal | int | None) -> float | None:
	return None if numerator is None or denominator in (None, 0) else float(Decimal(numerator) / Decimal(denominator))


def legacy_rows(prior: tuple[DailyBar, ...], current: tuple[DailyBar, ...], following: tuple[DailyBar, ...]) -> tuple[LegacyDerivedRow, ...]:
	"""Repair only the existing schema; Stage 5 supplies the new strategy targets."""
	priors = {bar.ticker: bar for bar in prior}
	nexts = {bar.ticker: bar for bar in following}
	ranked = sorted(((bar.ticker, float(nexts[bar.ticker].close / nexts[bar.ticker].open)) for bar in current if bar.ticker in nexts), key=lambda item: (-item[1], item[0]))
	top = {ticker for ticker, score in ranked[:100]}
	rows: list[LegacyDerivedRow] = []
	for bar in current:
		previous, next_bar = priors.get(bar.ticker), nexts.get(bar.ticker)
		ratios = tuple(_ratio(getattr(bar, name), getattr(previous, name)) for name in ("open", "high", "low", "close", "volume", "vwap", "transactions")) if previous is not None else (None,) * 7
		outcome = None if next_bar is None else float(next_bar.close / next_bar.open)
		extreme = None if outcome is None else (0.5 if outcome >= 1 else 0) + outcome / (2 * (outcome + 1))
		raise_flag = None if outcome is None else float(outcome >= 1)
		top_flag = None if outcome is None else float(bar.ticker in top)
		values = (*ratios, float(bar.close / bar.open), outcome, None if outcome is None else outcome - 1, extreme, raise_flag, top_flag, None if extreme is None else extreme * 2 - 1, None if top_flag is None else top_flag * 2 - 1, None if raise_flag is None else raise_flag * 2 - 1)
		rows.append(LegacyDerivedRow(bar.ticker, values))
	return tuple(sorted(rows, key=lambda row: row.ticker))


def derived_id(rows: tuple[LegacyDerivedRow, ...]) -> str:
	return content_id([(row.ticker, row.values) for row in sorted(rows, key=lambda item: item.ticker)])


def _study_value(value: float | None) -> str | None:
	if value is None:
		return None
	if value == 0:
		return "0"
	return format(Decimal(str(value)).normalize(), "f")


def derived_study_id(rows: tuple[LegacyDerivedRow, ...]) -> str:
	return content_id([
		[row.ticker, *(_study_value(value) for value in row.values)]
		for row in sorted(rows, key=lambda item: item.ticker)
	])
