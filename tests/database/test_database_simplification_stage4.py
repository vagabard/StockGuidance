from datetime import date, datetime, timedelta, timezone
from dataclasses import replace

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.database.dependency_planner import (
	CacheEntry,
	CacheRevision,
	DatabaseSnapshot,
	DependencyPlanner,
	InterpolationInterval,
	SnapshotSession,
	UpdateRequest,
	ZScoreSeriesSnapshot,
)
from stockguidance.database.simplified_contract import OperationKind, PendingScope, UnitKind, WorkUnit
from stockguidance.market_data.identity import data_configuration_key
from stockguidance.features.specification import FeatureSpec
from stockguidance.targets.specification import TargetSpec
from stockguidance.zscores.specification import ZScoreFamily
from stockguidance.zscores.storage import ZScoreSeriesDefinition


def _sessions() -> tuple[TradingSession, ...]:
	dates = (date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 5), date(2024, 7, 8), date(2024, 7, 9), date(2024, 7, 10))
	return tuple(
		TradingSession(
			"XNYS", session_date, 100 + index,
			datetime.combine(session_date, datetime.min.time(), timezone.utc) + timedelta(hours=13, minutes=30),
			datetime.combine(session_date, datetime.min.time(), timezone.utc) + timedelta(hours=20),
		)
		for index, session_date in enumerate(dates)
	)


def _snapshot(source_config: SourceConfig, *, incomplete_date: date | None = None, with_interpolation: bool = False) -> tuple[tuple[TradingSession, ...], DatabaseSnapshot]:
	sessions = _sessions()
	items = tuple(
		SnapshotSession(
			session, index + 1, session.session_date != incomplete_date, session.session_date != incomplete_date,
			(1, 2), () if session.session_date == incomplete_date else (1, 2),
			(1,) if with_interpolation and index in (2, 3) else (), (1, 2),
		)
		for index, session in enumerate(sessions)
	)
	configuration_key = data_configuration_key(source_config.data_configuration)
	definitions = (
		ZScoreSeriesSnapshot(ZScoreSeriesDefinition(configuration_key, "XNYS", ZScoreFamily.PRICE, 3, "rolling-population-zscore-v1", "float32", "not_applicable"), (1, 2), frozenset(item.date_id for item in items)),
		ZScoreSeriesSnapshot(ZScoreSeriesDefinition(configuration_key, "XNYS", ZScoreFamily.PRICE_DELTA, 3, "rolling-population-zscore-v1", "float32", "close_ratio_to_prior_minus_one"), (1, 2), frozenset(item.date_id for item in items)),
	)
	intervals: tuple[InterpolationInterval, ...] = ()
	if with_interpolation:
		intervals = (InterpolationInterval(1, items[1].date_id, sessions[1].session_date, items[4].date_id, sessions[4].session_date),)
	cache = (
		CacheRevision(
			"training-dataset",
			entries=(
				CacheEntry(sessions[3].session_date, (sessions[2].session_date,)),
				CacheEntry(sessions[5].session_date, (sessions[2].session_date,)),
			),
		),
	)
	return sessions, DatabaseSnapshot(
		items, configuration_key=configuration_key, calendar_id="XNYS", interpolation_intervals=intervals,
		zscore_series=definitions, cache_revisions=cache,
		feature_specs=(FeatureSpec((1, 2), 2),), target_specs=(TargetSpec(),), normalization_warmup_sessions=1,
	)


def _request(source_config: SourceConfig, sessions: tuple[TradingSession, ...], kind: OperationKind, changed: tuple[date, ...] = ()) -> UpdateRequest:
	return UpdateRequest(source_config, sessions, datetime(2024, 7, 10, 23, tzinfo=timezone.utc), kind, frozenset(changed))


def _unit_dates(units: tuple[WorkUnit, ...], kind: str) -> tuple[date, ...]:
	return tuple(unit.session_date for unit in units if unit.unit_kind == kind and unit.session_date is not None)


def test_single_correction_closes_formula_neighbors_ranks_and_rolling_windows(source_config: SourceConfig) -> None:
	sessions, snapshot = _snapshot(source_config)
	changed = sessions[2].session_date
	scope = DependencyPlanner().plan(_request(source_config, sessions, OperationKind.FORCED_REPLACEMENT, (changed,)), snapshot)

	assert _unit_dates(scope.clear.raw, UnitKind.RAW_SESSION.value) == (changed,)
	assert set(_unit_dates(scope.clear.deltas, UnitKind.DELTA_SESSION.value)) == {sessions[1].session_date, changed, sessions[3].session_date}
	assert {unit.ticker_id for unit in scope.clear.deltas} == {1, 2}
	price = next(series for series in snapshot.zscore_series if series.definition.family is ZScoreFamily.PRICE)
	price_dates = {unit.session_date for unit in scope.clear.zscores if unit.series_key == price.series_key}
	assert price_dates == {sessions[2].session_date, sessions[3].session_date, sessions[4].session_date}
	assert any(unit.unit_kind == UnitKind.ZSCORE_SESSION.value and unit.ticker_id is None for unit in scope.clear.completion)
	assert tuple(scope.cache_invalidations[0].session_dates) == (sessions[3].session_date, sessions[5].session_date)


def test_consecutive_corrections_are_deduplicated_and_bounded(source_config: SourceConfig) -> None:
	sessions, snapshot = _snapshot(source_config)
	scope = DependencyPlanner().plan(_request(source_config, sessions, OperationKind.FORCED_REPLACEMENT, (sessions[2].session_date, sessions[3].session_date)), snapshot)

	assert _unit_dates(scope.clear.raw, UnitKind.RAW_SESSION.value) == (sessions[2].session_date, sessions[3].session_date)
	assert set(_unit_dates(scope.clear.deltas, UnitKind.DELTA_SESSION.value)) == {sessions[1].session_date, sessions[2].session_date, sessions[3].session_date, sessions[4].session_date}
	assert len(scope.clear.deltas) == len(set(scope.clear.deltas))
	assert sessions[0].session_date not in _unit_dates(scope.clear.deltas, UnitKind.DELTA_SESSION.value)
	assert sessions[5].session_date not in _unit_dates(scope.clear.deltas, UnitKind.DELTA_SESSION.value)


def test_changed_interpolation_endpoint_closes_only_existing_interval_rows(source_config: SourceConfig) -> None:
	sessions, snapshot = _snapshot(source_config, with_interpolation=True)
	scope = DependencyPlanner().plan(_request(source_config, sessions, OperationKind.FORCED_REPLACEMENT, (sessions[1].session_date,)), snapshot)

	assert {(unit.session_date, unit.ticker_id) for unit in scope.clear.repairs} == {(sessions[2].session_date, 1), (sessions[3].session_date, 1)}
	assert all(unit.ticker_id == 1 for unit in scope.rebuild if unit.unit_kind == UnitKind.REPAIR_ROW.value)
	assert sessions[5].session_date not in {unit.session_date for unit in scope.clear.repairs}


def test_withdrawn_ticker_does_not_create_placeholders_but_rank_boundary_is_session_wide(source_config: SourceConfig) -> None:
	sessions, snapshot = _snapshot(source_config)
	changed_item = replace(snapshot.sessions[2], raw_ticker_ids=(1,), expected_ticker_ids=(1,), delta_ticker_ids=(1,))
	changed_snapshot = replace(snapshot, sessions=snapshot.sessions[:2] + (changed_item,) + snapshot.sessions[3:])
	scope = DependencyPlanner().plan(_request(source_config, sessions, OperationKind.FORCED_REPLACEMENT, (sessions[2].session_date,)), changed_snapshot)

	current_date = {(unit.session_date, unit.ticker_id) for unit in scope.clear.deltas if unit.session_date == sessions[2].session_date}
	prior_date = {(unit.session_date, unit.ticker_id) for unit in scope.clear.deltas if unit.session_date == sessions[1].session_date}
	assert current_date == {(sessions[2].session_date, 1)}
	assert prior_date == {(sessions[1].session_date, 1), (sessions[1].session_date, 2)}


def test_derived_only_hole_does_not_clear_raw_and_repairs_missing_zscores(source_config: SourceConfig) -> None:
	sessions_date = _sessions()[2].session_date
	sessions, snapshot = _snapshot(source_config, incomplete_date=sessions_date)
	scope = DependencyPlanner().plan(_request(source_config, sessions, OperationKind.DERIVED_REPAIR), snapshot)

	assert scope.clear.raw == ()
	assert scope.clear.repairs == ()
	assert {unit.ticker_id for unit in scope.clear.deltas} == {1, 2}
	assert {unit.session_date for unit in scope.clear.deltas} == {sessions_date}
	assert any(unit.series_key and unit.session_date == sessions_date for unit in scope.clear.zscores)


def test_holiday_boundaries_use_session_ordinals_not_calendar_days(source_config: SourceConfig) -> None:
	sessions, snapshot = _snapshot(source_config)
	changed = sessions[1].session_date
	scope = DependencyPlanner().plan(_request(source_config, sessions, OperationKind.FORCED_REPLACEMENT, (changed,)), snapshot)

	assert set(_unit_dates(scope.clear.deltas, UnitKind.DELTA_SESSION.value)) == {sessions[0].session_date, sessions[1].session_date, sessions[2].session_date}
	assert date(2024, 7, 3) not in _unit_dates(scope.clear.deltas, UnitKind.DELTA_SESSION.value)


def test_latest_incomplete_target_does_not_extend_beyond_snapshot(source_config: SourceConfig) -> None:
	sessions, snapshot = _snapshot(source_config)
	changed = sessions[-1].session_date
	scope = DependencyPlanner().plan(_request(source_config, sessions, OperationKind.FORCED_REPLACEMENT, (changed,)), snapshot)

	assert set(_unit_dates(scope.clear.deltas, UnitKind.DELTA_SESSION.value)) == {sessions[-2].session_date, sessions[-1].session_date}
	assert max(_unit_dates(scope.clear.deltas, UnitKind.DELTA_SESSION.value)) == changed
	assert max(_unit_dates(scope.clear.zscores, UnitKind.ZSCORE_SESSION.value)) == changed


def test_scope_round_trip_is_deterministic_and_preserves_explicit_closure(source_config: SourceConfig) -> None:
	sessions, snapshot = _snapshot(source_config)
	request = _request(source_config, sessions, OperationKind.FORCED_REPLACEMENT, (sessions[2].session_date,))
	first = DependencyPlanner().plan(request, snapshot)
	second = DependencyPlanner().plan(request, snapshot)

	assert first == second
	assert PendingScope.from_json(first.to_json()) == first
	order = {UnitKind.RAW_SESSION.value: 0, UnitKind.REPAIR_ROW.value: 1, UnitKind.DELTA_SESSION.value: 2, UnitKind.ZSCORE_SESSION.value: 3}
	assert first.rebuild == tuple(sorted(first.rebuild, key=lambda unit: (order[unit.unit_kind], unit.session_date or date.min, unit.date_id or 0, unit.ticker_id or 0, unit.series_key or "", unit.window or 0)))
