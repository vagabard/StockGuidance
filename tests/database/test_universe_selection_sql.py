from datetime import date

from stockguidance.database.table_names import TableNames
from stockguidance.database.universe_selection import UniverseSqlScope, render_universe_selection_sql


def names() -> TableNames:
	return TableNames(
		"fixture_source", "fixture_sessions", "fixture_tickers", "fixture_prices", "fixture_deltas", "fixture_imports", "fixture_series", "fixture_zscore", "fixture_price_{window}", "fixture_delta_{window}", "fixture_volume_{window}",
		data_verifications="fixture_verifications", data_verification_samples="fixture_verification_samples", schema="fixture_schema", data_states="fixture_states", pending_updates="fixture_pending", repair_metadata="fixture_repairs",
	)


def test_rendered_sql_is_read_only_bounded_and_uses_custom_registry() -> None:
	scope = UniverseSqlScope((date(2026, 1, 5), date(2026, 1, 7)), (7, 2), True, False)
	rendered = render_universe_selection_sql(scope, "select", names())
	assert rendered.read_only
	assert "`fixture_sessions`" in rendered.script
	assert "`fixture_tickers`" in rendered.script
	assert "`fixture_prices`" in rendered.script
	assert "p.volume" in rendered.script
	assert "p.transactions" not in rendered.script
	assert "%s" not in rendered.script
	assert "INSERT" not in rendered.script.upper()
	assert "ORDER BY s.transaction_date, t.id" in rendered.script


def test_sql_plan_and_validation_include_explain_and_verification_selects() -> None:
	scope = UniverseSqlScope((date(2026, 1, 5),), None, False, False)
	plan = render_universe_selection_sql(scope, "plan", names())
	validation = render_universe_selection_sql(scope, "validate", names())
	assert "EXPLAIN SELECT" in plan.script
	assert "GROUP BY s.transaction_date" in validation.script
	assert "rows_by_state" in validation.script
