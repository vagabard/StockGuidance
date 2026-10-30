from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from support.ingestion import FakeDatabase

from stockguidance.database.preparation import (
	PreparationStore,
	VolumeInputRow,
	VolumePreparationRequest,
	apply_volume_result,
	calculate_volume_zscores,
	plan_volume_zscore,
	render_volume_sql,
	select_volume_inputs,
	validate_volume_result,
)
from stockguidance.database.simplified_schema import initialize_simplified_schema
from stockguidance.database.table_names import load_table_names
from stockguidance.market_data.identity import data_configuration_key, source_key
from stockguidance.zscores.inputs import InputState
from stockguidance.zscores.results import ZScoreStatus


class FixtureLock:
	def __enter__(self) -> "FixtureLock":
		return self

	def __exit__(self, *_: object) -> None:
		return None

	def assert_held(self) -> None:
		return None


def _request(source, start: date, end: date) -> VolumePreparationRequest:
	return VolumePreparationRequest(source_key(source.identity), data_configuration_key(source.data_configuration), source.calendar_id, start, end, (1,), (2,), 2)


def _database(source, calendar, tmp_path: Path):
	database = FakeDatabase(tmp_path / "preparation.sqlite")
	names = load_table_names()
	initialize_simplified_schema(source, database, windows=(2,), table_names=names)
	sessions = calendar.sessions(date(2024, 6, 24), date(2024, 7, 5))
	for index, session in enumerate(sessions):
		database.sql("INSERT INTO transaction_dates (transaction_date, raw_complete, raw_available_at, raw_completed_at, raw_revision) VALUES (?, ?, ?, ?, ?)", (session.session_date.isoformat(), 1, "2024-07-10T00:00:00Z", "2024-07-10T00:00:00Z", f"{index + 1:064x}"))
	database.sql("INSERT INTO ticker_symbol (label, exchange) VALUES (?, ?)", ("AAA", "XNYS"))
	date_rows = database.sql("SELECT id, transaction_date FROM transaction_dates ORDER BY transaction_date")
	for index, (date_id, _) in enumerate(date_rows):
		database.sql("INSERT INTO raw_stock_data (ticker_id, date_id, close, volume, state_id) VALUES (?, ?, ?, ?, ?)", (1, date_id, "10.00000000", str(10 + index), 1))
	return database, names, sessions


def test_volume_calculation_matches_population_fixture(calendar, source_config, tmp_path):
	request = _request(source_config, date(2024, 7, 3), date(2024, 7, 5))
	plan = plan_volume_zscore(request, calendar)
	rows = tuple(VolumeInputRow(index + 1, session.session_date, 1, Decimal(index + 1), InputState.AVAILABLE, True, f"{index + 1:064x}") for index, session in enumerate(plan.dependency_sessions))
	result = calculate_volume_zscores(plan, rows, datetime(2024, 7, 10, tzinfo=timezone.utc))
	assert [row.row.status for row in result.rows] == [ZScoreStatus.READY] * len(plan.write_sessions)
	assert [row.row.score for row in result.rows] == [1.0] * len(plan.write_sessions)

	missing = list(rows)
	missing[-1] = replace(missing[-1], volume=None, state=InputState.MISSING)
	missing_result = calculate_volume_zscores(plan, missing, datetime(2024, 7, 10, tzinfo=timezone.utc))
	assert missing_result.rows[-1].row.status is ZScoreStatus.MISSING_INPUT
	assert missing_result.rows[-1].row.score is None


def test_preparation_export_render_and_apply_are_rerunnable(calendar, source_config, tmp_path):
	database, names, sessions = _database(source_config, calendar, tmp_path)
	request = _request(source_config, sessions[-3].session_date, sessions[-1].session_date)
	plan = plan_volume_zscore(request, calendar, names)
	rows = select_volume_inputs(source_config, database, plan, names)
	assert len(rows) == len(plan.dependency_sessions)
	assert all(row.raw_revision for row in rows)
	rendered = render_volume_sql(plan, "select", names)
	assert rendered.read_only
	assert "fixture" not in rendered.script
	assert "`transaction_dates`" in rendered.script
	assert "%s" not in rendered.script
	assert "AS `window`" in render_volume_sql(plan, "calculate", names).script
	result = calculate_volume_zscores(plan, rows, datetime(2024, 7, 10, tzinfo=timezone.utc))
	lock = lambda: FixtureLock()
	apply_volume_result(source_config, database, plan, result, table_names=names, lock_factory=lock)
	apply_volume_result(source_config, database, plan, result, table_names=names, lock_factory=lock)
	validation = validate_volume_result(source_config, database, plan, result, table_names=names)
	assert validation.rows_checked == len(result.rows)
	assert database.sql("SELECT COUNT(*) FROM zscores_volume_2") == [(len(result.rows),)]
	assert database.sql("SELECT COUNT(*) FROM stockguidance_pending_updates") == [(0,)]


def test_source_change_between_export_and_apply_is_blocked(calendar, source_config, tmp_path):
	database, names, sessions = _database(source_config, calendar, tmp_path)
	request = _request(source_config, sessions[-2].session_date, sessions[-1].session_date)
	plan = plan_volume_zscore(request, calendar, names)
	rows = select_volume_inputs(source_config, database, plan, names)
	result = calculate_volume_zscores(plan, rows, datetime(2024, 7, 10, tzinfo=timezone.utc))
	date_id = database.sql("SELECT id FROM transaction_dates ORDER BY transaction_date DESC LIMIT 1")[0][0]
	database.sql("UPDATE raw_stock_data SET volume = ? WHERE date_id = ? AND ticker_id = 1", (999, date_id))
	with pytest.raises(Exception, match="changed between"):
		apply_volume_result(source_config, database, plan, result, table_names=names, lock_factory=lambda: FixtureLock())
	assert database.sql("SELECT COUNT(*) FROM stockguidance_pending_updates") == [(0,)]


def test_interruption_after_clear_is_resumable(calendar, source_config, tmp_path):
	database, names, sessions = _database(source_config, calendar, tmp_path)
	plan = plan_volume_zscore(_request(source_config, sessions[-2].session_date, sessions[-1].session_date), calendar, names)
	result = calculate_volume_zscores(plan, select_volume_inputs(source_config, database, plan, names), datetime(2024, 7, 10, tzinfo=timezone.utc))
	with pytest.raises(Exception, match="interrupted"):
		apply_volume_result(source_config, database, plan, result, table_names=names, lock_factory=lambda: FixtureLock(), interrupt_after_clear=True)
	assert database.sql("SELECT affected_rows_cleared FROM stockguidance_pending_updates") == [(1,)]
	apply_volume_result(source_config, database, plan, result, table_names=names, lock_factory=lambda: FixtureLock())
	assert database.sql("SELECT COUNT(*) FROM stockguidance_pending_updates") == [(0,)]


def test_preparation_store_is_atomic_and_portable(calendar, source_config, tmp_path):
	plan = plan_volume_zscore(_request(source_config, date(2024, 7, 3), date(2024, 7, 5)), calendar)
	store = PreparationStore(tmp_path / "operations")
	operation_id = store.create(plan, datetime(2024, 7, 10, tzinfo=timezone.utc))
	manifest = store.read(operation_id)
	assert manifest["schema"] == "stockguidance-preparation-1"
	assert Path(store.path(operation_id), "manifest.json").is_file()
	store.update(operation_id, status="failed", failure={"code": "fixture", "detail": "safe"})
	assert store.read(operation_id)["status"] == "failed"
