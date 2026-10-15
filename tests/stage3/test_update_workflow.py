from dataclasses import replace
from datetime import date, timedelta
import json

import pytest

from stockguidance.config.source import PriceAdjustment
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.validation.coverage import CoverageState
from stockguidance.workflows.failures import StageExecutionError

def test_empty_database_and_custom_configuration_are_reconciled(harness):
	result = harness.run()
	assert result.successful
	assert result.closed_dates == (date(2024, 7, 4), date(2024, 7, 6), date(2024, 7, 7))
	assert len(harness.provider.calls) == 5
	assert all(source == harness.source for source, session in harness.provider.calls)
	assert set(harness.database.configs) == {harness.source.database_config}
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(5,)]
	assert all(manifest.import_complete and manifest.derived_complete for manifest in result.manifests)
	assert all(item.state is CoverageState.COMPLETE for item in result.coverage.sessions)
	assert result.data_revision
	assert list((harness.source.data_root / "market_data").rglob("*.manifest.json"))


def test_repeated_run_does_not_download_duplicate_or_rewrite_verified_rows(harness):
	first = harness.run()
	calls, commits = len(harness.provider.calls), harness.database.commits
	second = harness.run()
	assert second.successful and first.data_revision == second.data_revision
	assert second.downloaded_sessions == 0 and len(harness.provider.calls) == calls
	assert harness.database.commits == commits
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(5,)]


def test_interior_provider_failure_does_not_hide_behind_later_success(harness):
	missing = date(2024, 7, 2)
	harness.provider.errors.add(missing)
	first = harness.run()
	assert not first.successful and first.failures
	assert harness.database.sql("SELECT transaction_date FROM transaction_dates ORDER BY transaction_date")[-1] == ("2024-07-08",)
	assert next(item for item in first.coverage.sessions if item.session.session_date == missing).state is CoverageState.UNVERIFIED
	harness.provider.errors.clear()
	harness.provider.calls.clear()
	second = harness.run()
	assert second.successful
	assert [session.session_date for source, session in harness.provider.calls] == [missing]
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(5,)]


@pytest.mark.parametrize("damage", ["manifest", "missing-response", "truncated-response", "same-count-wrong-key"])
def test_corrupt_or_partial_source_evidence_is_refetched(harness, damage):
	first = harness.run()
	manifest = first.manifests[1]
	store = ManifestStore(harness.source.data_root)
	path = store.path_for(harness.source.identity, manifest.download.session)
	if damage == "manifest":
		path.write_text('{"schema_version":', encoding="utf-8")
	elif damage == "same-count-wrong-key":
		payload = json.loads(path.read_text())
		payload["record_keys"] = ["WRONG"]
		path.write_text(json.dumps(payload), encoding="utf-8")
	else:
		raw_path = path.parent / "responses" / f"{manifest.content_id}.json"
		if damage == "missing-response":
			raw_path.unlink()
		else:
			raw_path.write_text('{"records":[', encoding="utf-8")
	harness.provider.calls.clear()
	assert harness.run().successful
	assert manifest.download.session.session_date in [session.session_date for source, session in harness.provider.calls]


def test_interrupted_raw_import_rolls_back_then_reuses_verified_download(harness):
	harness.database.fail_on = "INSERT INTO raw_stock_data"
	first = harness.run()
	assert not first.successful
	assert harness.database.rollbacks > 0
	assert harness.database.sql("SELECT transaction_date FROM transaction_dates WHERE transaction_date = '2024-07-01'") == []
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(4,)]
	harness.provider.calls.clear()
	assert harness.run().successful
	assert harness.provider.calls == []
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(5,)]


def test_interrupted_derived_import_recovers_without_duplicate_raw_data(harness):
	harness.database.fail_on = "INSERT INTO daily_delta_stock_data"
	first = harness.run()
	assert not first.successful
	manifest = ManifestStore(harness.source.data_root).load(harness.source.identity, first.plan.sessions[0].session)
	assert manifest.import_complete and not manifest.derived_complete
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(5,)]
	harness.provider.calls.clear()
	assert harness.run().successful and harness.provider.calls == []
	assert harness.database.sql("SELECT COUNT(*) FROM daily_delta_stock_data") == [(5,)]


def test_late_correction_replaces_existing_values_and_dependent_transformations(harness):
	first = harness.run()
	harness.clock.value += timedelta(hours=2)
	harness.provider.responses[date(2024, 7, 2)] = harness.response("14")
	second = harness.run()
	assert second.successful and first.data_revision != second.data_revision
	rows = harness.database.sql("SELECT t.transaction_date, d.close_ratio_to_prior, d.next_day_change_in_price, d.zero_centered_next_day_price_change FROM daily_delta_stock_data d JOIN transaction_dates t ON t.id = d.date_id ORDER BY t.transaction_date")
	assert rows[0][2:] == pytest.approx((1.4, 0.4))
	assert rows[1][1] == pytest.approx(14 / 11)
	assert rows[2][1] == pytest.approx(11 / 14)
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(5,)]


def test_narrow_correction_repairs_neighbors_outside_requested_dates(harness):
	harness.run()
	harness.clock.value += timedelta(hours=2)
	harness.provider.responses[date(2024, 7, 2)] = harness.response("14")
	settings = replace(harness.settings, start=date(2024, 7, 2), end=date(2024, 7, 2))
	assert harness.run(settings).successful
	rows = harness.database.sql("SELECT t.transaction_date, d.close_ratio_to_prior, d.next_day_change_in_price FROM daily_delta_stock_data d JOIN transaction_dates t ON t.id = d.date_id ORDER BY t.transaction_date")
	assert rows[0][2] == pytest.approx(1.4)
	assert rows[2][1] == pytest.approx(11 / 14)
	assert rows[2][2] == pytest.approx(1.1)


def test_same_count_wrong_database_key_and_partial_import_are_repaired_locally(harness):
	first = harness.run()
	harness.database.sql("UPDATE ticker_symbol SET label = 'WRONG' WHERE label = 'AAA'")
	harness.provider.calls.clear()
	assert harness.run().successful and harness.provider.calls == []
	assert harness.database.sql("SELECT DISTINCT s.label FROM raw_stock_data r JOIN ticker_symbol s ON s.id=r.ticker_id") == [("AAA",)]
	assert first.data_revision == harness.run().data_revision


def test_deleted_earliest_rows_are_detected_even_with_later_successes(harness):
	first = harness.run()
	date_id = harness.database.sql("SELECT id FROM transaction_dates ORDER BY transaction_date LIMIT 1")[0][0]
	harness.database.sql("DELETE FROM raw_stock_data WHERE date_id=?", (date_id,))
	harness.provider.calls.clear()
	second = harness.run()
	assert second.successful and second.data_revision == first.data_revision
	assert harness.provider.calls == []


@pytest.mark.parametrize("state", [ResponseState.UNVERIFIED, ResponseState.NOT_YET_PUBLISHED])
def test_unknown_empty_or_delayed_payload_never_becomes_complete(harness, state):
	harness.provider.responses[date(2024, 7, 2)] = ProviderResponse(state, (), 200, "OK", (("resultsCount", 0),))
	result = harness.run()
	assert not result.successful
	assert next(item for item in result.coverage.sessions if item.session.session_date == date(2024, 7, 2)).state is not CoverageState.COMPLETE
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(4,)]


def test_explicit_no_trading_is_distinct_from_closure_or_unknown_empty(harness):
	harness.provider.responses[date(2024, 7, 2)] = ProviderResponse(ResponseState.NO_TRADING, (), 200, "OK", (("resultsCount", 0),), evidence="Fixture authoritative source confirms zero trades in this request scope")
	result = harness.run()
	assert result.successful
	assert any(manifest.download.response.state is ResponseState.NO_TRADING for manifest in result.manifests)
	assert date(2024, 7, 2) not in result.closed_dates


def test_unknown_publication_policy_does_not_access_provider_or_database(harness):
	settings = replace(harness.settings, publication=DailyPublicationPolicy(None, "Fixture unknown policy"))
	harness.database.statements.clear()
	result = harness.run(settings)
	assert not result.successful
	assert harness.provider.calls == [] and harness.database.statements == []
	assert all(item.state is CoverageState.UNVERIFIED for item in result.coverage.sessions)


def test_source_mismatch_is_rejected_without_overwriting_other_scope(harness):
	harness.source = replace(harness.source, identity=replace(harness.source.identity, adjustment=PriceAdjustment.UNADJUSTED))
	with pytest.raises(StageExecutionError):
		harness.run()
	assert harness.provider.calls == []


def test_error_reports_and_new_metadata_do_not_contain_provider_secrets(harness, capsys):
	harness.provider.errors.add(date(2024, 7, 2))
	result = harness.run()
	assert "fixture secret" not in repr(result)
	assert capsys.readouterr().out == ""
	for path in harness.source.data_root.rglob("*.json"):
		assert "fixture secret" not in path.read_text()
		assert "fixture-credential-reference" not in path.read_text()


def test_correction_removes_withdrawn_keys_without_requiring_every_ticker_each_day(harness):
	from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState

	first_bar = harness.response().records[0]
	second_bar = harness.response(ticker="BBB").records[0]
	harness.provider.responses[date(2024, 7, 2)] = ProviderResponse(ResponseState.COMPLETE, (first_bar, second_bar), 200, "OK", (("resultsCount", 2),))
	assert harness.run().successful
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(6,)]
	harness.clock.value += timedelta(hours=2)
	harness.provider.responses[date(2024, 7, 2)] = harness.response()
	assert harness.run().successful
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(5,)]
	assert harness.database.sql("SELECT COUNT(*) FROM daily_delta_stock_data") == [(5,)]


def test_partial_bulk_insert_rolls_back_every_row_and_receipt(harness):
	first_bar = harness.response().records[0]
	second_bar = harness.response(ticker="BBB").records[0]
	harness.provider.responses[date(2024, 7, 1)] = ProviderResponse(ResponseState.COMPLETE, (first_bar, second_bar), 200, "OK", (("resultsCount", 2),))
	harness.database.fail_on = "INSERT INTO raw_stock_data"
	harness.database.fail_after_matches = 1
	assert not harness.run().successful
	assert harness.database.sql("SELECT session_date FROM stockguidance_imports WHERE session_date='2024-07-01'") == []
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(4,)]
	harness.provider.calls.clear()
	assert harness.run().successful and harness.provider.calls == []
	assert harness.database.sql("SELECT COUNT(*) FROM raw_stock_data") == [(6,)]
