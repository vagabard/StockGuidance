import math

from stockguidance.database.repositories.market_data import SessionSnapshot
from stockguidance.market_data.legacy_derived import LegacyDerivedRow, legacy_rows
from stockguidance.market_data.records import DailyBar


def differing_columns(actual: LegacyDerivedRow, expected: LegacyDerivedRow) -> tuple[int, ...]:
	return tuple(index for index, (observed, required) in enumerate(zip(actual.values, expected.values, strict=True)) if not _equal(observed, required))


def _equal(observed: float | None, required: float | None) -> bool:
	if observed is None or required is None:
		return observed is required
	return math.isfinite(observed) and math.isclose(observed, required, rel_tol=1e-10, abs_tol=1e-12)


def derived_values_match(snapshot: SessionSnapshot, prior: tuple[DailyBar, ...], following: tuple[DailyBar, ...]) -> bool:
	expected = legacy_rows(prior, snapshot.records, following)
	actual = {row.ticker: row for row in snapshot.derived}
	return set(actual) == {row.ticker for row in expected} and all(not differing_columns(actual[row.ticker], row) for row in expected)
