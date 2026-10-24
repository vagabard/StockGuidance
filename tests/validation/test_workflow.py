from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.validation.findings import FindingCategory, ValidationCheck
from stockguidance.validation.reporting import report_payload
from stockguidance.workflows.validate_database import ValidationSettings


def matches(result, check, category=None, session_date=None):
	return [finding for finding in result.findings if finding.check is check and (category is None or finding.category is category) and (session_date is None or finding.session_date == session_date)]


def test_clean_scope_is_complete_and_source_evidence_is_explicit(harness, validator):
	assert harness.run().successful
	result = validator.run()
	assert result.complete and result.exit_code == 0
	assert not result.repair_plan.items
	payload = report_payload(result)
	assert payload["history_start"] == "2024-07-01"
	assert payload["observed_first_in_scope"] == "2024-07-01"
	assert payload["source"]["universe"] == harness.source.identity.universe
	assert payload["sessions"][0]["raw_valid_keys"] == ["AAA"]
	assert payload["derived_spec"] == "legacy-open-to-close-v1"
	assert result.samples and result.samples[0].targets.raw == (0.0,)
	assert matches(result, ValidationCheck.FEATURE, FindingCategory.NOT_ASSESSED)
	assert matches(result, ValidationCheck.HISTORY, FindingCategory.EXPECTED_ABSENCE, date(2024, 7, 1))


@pytest.mark.parametrize("missing", ["2024-07-01", "2024-07-03"])
def test_leading_and_interior_gaps_survive_later_success(harness, validator, missing):
	harness.run()
	harness.database.sql("DELETE FROM transaction_dates WHERE transaction_date = ?", (missing,))
	result = validator.run()
	assert not result.complete and result.exit_code == 2
	assert matches(result, ValidationCheck.SESSION, FindingCategory.MISSING, date.fromisoformat(missing))
	assert any(item.session_date == date.fromisoformat(missing) for item in result.repair_plan.items)
	assert result.plan.boundary.history_start == date(2024, 7, 1)
	if missing == "2024-07-01":
		assert result.plan.observed_first_in_scope == date(2024, 7, 2)


def test_plausible_counts_do_not_hide_missing_source_keys(harness, validator):
	harness.run()
	harness.database.sql("UPDATE ticker_symbol SET label = 'WRONG' WHERE label = 'AAA'")
	result = validator.run()
	assert matches(result, ValidationCheck.RAW_KEYS, FindingCategory.MISSING)[0].tickers == ("AAA",)
	assert matches(result, ValidationCheck.RAW_KEYS, FindingCategory.INVALID)[0].tickers == ("WRONG",)
	assert not result.complete


def test_ticker_lifetimes_are_not_a_cartesian_product(harness, validator):
	harness.provider.responses[date(2024, 7, 2)] = harness.response(ticker="NEW")
	assert harness.run().successful
	result = validator.run()
	assert result.complete
	assert not matches(result, ValidationCheck.RAW_KEYS)
	assert matches(result, ValidationCheck.OUTCOME, FindingCategory.EXPECTED_ABSENCE)
	assert matches(result, ValidationCheck.HISTORY, FindingCategory.EXPECTED_ABSENCE)


def test_invalid_prices_do_not_abort_assessment_of_other_sessions(harness, validator):
	harness.run()
	harness.database.sql("UPDATE raw_stock_data SET close = -1 WHERE date_id = (SELECT id FROM transaction_dates WHERE transaction_date = '2024-07-02')")
	result = validator.run()
	assert not result.failed and not result.complete
	assert matches(result, ValidationCheck.RAW_VALUES, FindingCategory.INVALID, date(2024, 7, 2))[0].tickers == ("AAA",)
	assert len(result.coverage.sessions) == 5


@pytest.mark.parametrize("column,value", [("volume", -1), ("transactions", -1), ("open", 0), ("high", 1), ("low", 50)])
def test_invalid_ohlc_and_counts(harness, validator, column, value):
	harness.run()
	harness.database.sql(f"UPDATE raw_stock_data SET {column} = ? WHERE date_id = 1", (value,))
	assert matches(validator.run(), ValidationCheck.RAW_VALUES, FindingCategory.INVALID)


def test_content_drift_is_invalid_even_with_unchanged_keys(harness, validator):
	harness.run()
	harness.database.sql("UPDATE raw_stock_data SET close = 12 WHERE date_id = 1")
	result = validator.run()
	assert matches(result, ValidationCheck.RAW_VALUES, FindingCategory.INVALID)
	assert matches(result, ValidationCheck.IMPORT, FindingCategory.INCOMPLETE)


def test_missing_derived_rows_and_interrupted_import(harness, validator):
	harness.run()
	harness.database.sql("DELETE FROM daily_delta_stock_data WHERE date_id = 1")
	harness.database.sql("UPDATE stockguidance_imports SET derived_dependency_id = NULL, derived_content_id = NULL WHERE session_date = '2024-07-02'")
	result = validator.run()
	assert matches(result, ValidationCheck.DERIVED, FindingCategory.MISSING, date(2024, 7, 1))
	assert matches(result, ValidationCheck.DERIVED, FindingCategory.INCOMPLETE, date(2024, 7, 2))


def test_mature_outcome_is_required_when_next_response_has_the_key(harness, validator):
	harness.run()
	harness.database.sql("UPDATE daily_delta_stock_data SET next_day_change_in_price = NULL WHERE date_id = 1")
	result = validator.run()
	assert matches(result, ValidationCheck.OUTCOME, FindingCategory.MISSING, date(2024, 7, 1))
	assert not result.complete


def test_immature_final_label_is_expected_and_does_not_fail_readiness(harness, validator):
	harness.clock.value = datetime(2024, 7, 8, 21, tzinfo=timezone.utc)
	assert harness.run().successful
	result = validator.run()
	assert result.complete
	assert matches(result, ValidationCheck.OUTCOME, FindingCategory.NOT_YET_AVAILABLE, date(2024, 7, 8))
	assert not matches(result, ValidationCheck.OUTCOME, FindingCategory.MISSING, date(2024, 7, 8))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "count"])
def test_provider_evidence_is_required_beyond_internal_consistency(harness, validator, damage):
	updated = harness.run()
	store = ManifestStore(harness.source.data_root)
	path = store.path_for(harness.source.identity, updated.manifests[0].download.session)
	if damage == "missing":
		path.unlink()
	elif damage == "corrupt":
		path.write_text("broken", encoding="utf-8")
	else:
		payload = json.loads(path.read_text())
		payload["record_count"] = 22
		path.write_text(json.dumps(payload), encoding="utf-8")
	result = validator.run()
	assert matches(result, ValidationCheck.SOURCE, FindingCategory.UNVERIFIED, date(2024, 7, 1))
	assert not result.complete


def test_unknown_publication_policy_does_not_query_or_write(harness, validator):
	settings = ValidationSettings(harness.settings.start, harness.settings.end, DailyPublicationPolicy(None, "Unknown fixture policy"))
	harness.database.statements.clear()
	result = validator.run(settings)
	assert not result.complete
	assert matches(result, ValidationCheck.SESSION, FindingCategory.UNVERIFIED)
	assert not harness.database.statements


def test_unverified_response_and_explicit_provider_delay(harness, validator):
	from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
	harness.provider.responses[date(2024, 7, 2)] = ProviderResponse(ResponseState.UNVERIFIED, (), 200, "OK")
	harness.provider.responses[date(2024, 7, 3)] = ProviderResponse(ResponseState.NOT_YET_PUBLISHED, (), 200, "DELAYED")
	harness.run()
	result = validator.run()
	assert matches(result, ValidationCheck.SOURCE, FindingCategory.UNVERIFIED, date(2024, 7, 2))
	assert matches(result, ValidationCheck.SOURCE, FindingCategory.NOT_YET_AVAILABLE, date(2024, 7, 3))
	assert not result.complete


def test_explicit_no_trading_requires_evidence_and_is_complete(harness, validator):
	from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
	harness.provider.responses[date(2024, 7, 2)] = ProviderResponse(ResponseState.NO_TRADING, (), 200, "OK", evidence="Authoritative fixture-only no trading")
	assert harness.run().successful
	assert validator.run().complete


def test_historical_cutoff_does_not_validate_future_receipts(harness, validator):
	harness.run()
	settings = ValidationSettings(harness.settings.start, harness.settings.end, harness.settings.publication, datetime(2024, 7, 3, 18, tzinfo=timezone.utc))
	harness.database.statements.clear()
	result = validator.run(settings)
	assert not result.complete
	assert matches(result, ValidationCheck.SESSION, FindingCategory.NOT_YET_AVAILABLE)
	assert all(query_date <= date(2024, 7, 3) for operation, parameters in harness.database.statements for query_date in parameters if type(query_date) is date)


def test_unfinished_manifest_is_reported_without_mutation(harness, validator):
	updated = harness.run()
	store = ManifestStore(harness.source.data_root)
	manifest = updated.manifests[0]
	store.save(replace(manifest, derived_complete=False))
	before = store.path_for(harness.source.identity, manifest.download.session).read_bytes()
	result = validator.run()
	assert not result.complete
	assert store.path_for(harness.source.identity, manifest.download.session).read_bytes() == before


def test_unavailable_daily_sessions_are_deferred(harness, validator):
	harness.clock.value = datetime(2024, 7, 3, 16, tzinfo=timezone.utc)
	harness.run()
	result = validator.run()
	assert result.complete
	assert result.coverage.sessions[-1].state.value == "not_yet_available"
	assert result.plan.excluded_after_cutoff
