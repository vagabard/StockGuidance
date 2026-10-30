"""Read-only SQL rendering for Stage 4 universe inspection.

This module renders bounded input and explanation queries only.  Selection,
coverage classification, filter calculation, and evidence persistence remain in
``stockguidance.universe.selection`` and the typed repository adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.universe.selection import UniverseReadRequest


UNIVERSE_SQL_SCHEMA = "stockguidance-universe-sql-1"


@dataclass(frozen=True)
class UniverseSqlScope:
	session_dates: tuple[date, ...]
	ticker_ids: tuple[int, ...] | None
	read_volume: bool
	read_transactions: bool

	def __post_init__(self) -> None:
		if not isinstance(self.session_dates, tuple) or not self.session_dates or any(not isinstance(item, date) for item in self.session_dates) or tuple(sorted(set(self.session_dates))) != self.session_dates:
			raise ValueError("SQL session dates must be sorted and unique")
		if self.ticker_ids is not None and (not self.ticker_ids or len(set(self.ticker_ids)) != len(self.ticker_ids) or any(type(item) is not int or item <= 0 for item in self.ticker_ids)):
			raise ValueError("SQL ticker IDs must be unique positive integers")
		if type(self.read_volume) is not bool or type(self.read_transactions) is not bool:
			raise ValueError("SQL optional reads must be boolean")

	@classmethod
	def from_read_request(cls, request: UniverseReadRequest) -> "UniverseSqlScope":
		return cls(tuple(item.session_date for item in request.sessions), request.ticker_ids, request.read_volume, request.read_transactions)


@dataclass(frozen=True)
class RenderedUniverseSql:
	step: str
	script: str
	result_columns: tuple[tuple[str, str], ...]
	read_only: bool = True
	calculation_route: str = "python_reference"


def _dates(scope: UniverseSqlScope) -> str:
	return ", ".join(f"'{item.isoformat()}'" for item in scope.session_dates)


def _ids(scope: UniverseSqlScope) -> str:
	return "" if scope.ticker_ids is None else ", ".join(str(item) for item in scope.ticker_ids)


def _queries(scope: UniverseSqlScope, names: TableNames) -> tuple[str, str, str]:
	sessions = quote_identifier(names.table(TableRole.SESSIONS))
	tickers = quote_identifier(names.table(TableRole.TICKERS))
	prices = quote_identifier(names.table(TableRole.PRICES))
	states = quote_identifier(names.table(TableRole.DATA_STATES))
	dates = _dates(scope)
	ids = _ids(scope)
	ticker_filter = "" if scope.ticker_ids is None else f" WHERE t.id IN ({ids})"
	ticker_query = f"SELECT t.id AS ticker_id, t.label FROM {tickers} t{ticker_filter} ORDER BY t.id;"
	coverage_query = f"SELECT s.id AS date_id, s.transaction_date, s.raw_complete, s.raw_available_at, s.raw_revision FROM {sessions} s WHERE s.transaction_date IN ({dates}) ORDER BY s.transaction_date;"
	columns = ["s.transaction_date", "t.id AS ticker_id", "p.close"]
	if scope.read_volume:
		columns.append("p.volume")
	if scope.read_transactions:
		columns.append("p.transactions")
	columns.extend(("p.state_id", "ds.code AS state_code", "s.raw_complete", "s.raw_revision"))
	ticker_filter = "" if scope.ticker_ids is None else f" AND t.id IN ({ids})"
	raw_query = (
		f"SELECT {', '.join(columns)} FROM {sessions} s CROSS JOIN {tickers} t "
		f"LEFT JOIN {prices} p ON p.date_id = s.id AND p.ticker_id = t.id "
		f"LEFT JOIN {states} ds ON ds.id = p.state_id "
		f"WHERE s.transaction_date IN ({dates}){ticker_filter} ORDER BY s.transaction_date, t.id;"
	)
	return ticker_query, coverage_query, raw_query


def render_universe_selection_sql(scope: UniverseSqlScope | UniverseReadRequest, step: str = "select", table_names: TableNames | None = None) -> RenderedUniverseSql:
	"""Render inspect/plan/select/validate SQL without executing it."""
	if isinstance(scope, UniverseReadRequest):
		scope = UniverseSqlScope.from_read_request(scope)
	if not isinstance(scope, UniverseSqlScope):
		raise ValueError("Universe SQL rendering requires a typed scope")
	if step not in {"plan", "select", "validate"}:
		raise ValueError("Universe SQL supports plan, select, and validate steps only")
	names = table_names or load_table_names()
	ticker_query, coverage_query, raw_query = _queries(scope, names)
	header = "\n".join((
		f"-- {UNIVERSE_SQL_SCHEMA}; read-only rendering; no statements have been executed.",
		"-- Selection and filter calculation happen in the typed Python reference path.",
		"-- This script exports bounded inputs and completion evidence for database-interface debugging.",
		f"-- session_dates={_dates(scope)}",
		f"-- ticker_ids={'all' if scope.ticker_ids is None else _ids(scope)}",
		"-- Review the resolved registry identifiers before execution.",
	)) + "\n"
	if step == "plan":
		script = header + "-- EXPLAIN is also read-only.\nEXPLAIN " + raw_query
		return RenderedUniverseSql(step, script, (), True)
	columns: list[tuple[str, str]] = [("transaction_date", "DATE"), ("ticker_id", "INT"), ("close", "DECIMAL(28,8)")]
	if scope.read_volume:
		columns.append(("volume", "DECIMAL(20,8)"))
	if scope.read_transactions:
		columns.append(("transactions", "BIGINT"))
	columns.extend((("state_id", "SMALLINT"), ("state_code", "VARCHAR(16)"), ("raw_complete", "BOOLEAN"), ("raw_revision", "CHAR(64)")))
	if step == "select":
		script = header + "-- Ordered statements: ticker labels, whole-session completion, then bounded raw rows.\n" + ticker_query + "\n" + coverage_query + "\n" + raw_query + "\n"
		return RenderedUniverseSql(step, script, tuple(columns), True)
	verification = (
		f"SELECT s.transaction_date, s.raw_complete, s.raw_revision, COUNT(p.ticker_id) AS raw_rows "
		f"FROM {quote_identifier(names.table(TableRole.SESSIONS))} s "
		f"LEFT JOIN {quote_identifier(names.table(TableRole.PRICES))} p ON p.date_id = s.id "
		f"WHERE s.transaction_date IN ({_dates(scope)}) GROUP BY s.transaction_date, s.raw_complete, s.raw_revision ORDER BY s.transaction_date;\n"
		f"SELECT s.transaction_date, p.state_id, COUNT(*) AS rows_by_state "
		f"FROM {quote_identifier(names.table(TableRole.SESSIONS))} s "
		f"JOIN {quote_identifier(names.table(TableRole.PRICES))} p ON p.date_id = s.id "
		f"WHERE s.transaction_date IN ({_dates(scope)}) GROUP BY s.transaction_date, p.state_id ORDER BY s.transaction_date, p.state_id;\n"
	)
	return RenderedUniverseSql(step, header + "-- Verification SELECTs; compare counts and state codes before using exported rows.\n" + verification, tuple(columns), True)


__all__ = ["RenderedUniverseSql", "UNIVERSE_SQL_SCHEMA", "UniverseSqlScope", "render_universe_selection_sql"]
