from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.cli.validate_database import main
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.datasets.building import build_sample
from stockguidance.datasets.sample import TargetState
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.targets.specification import TargetSpec
from stockguidance.validation.findings import FindingCategory, ValidationCheck
from stockguidance.validation.reporting import report_payload
from stockguidance.workflows.validate_database import ValidationSettings, validate_database


def decision(calendar, day=5):
	entry = calendar.session(date(2024, 7, day))
	return DecisionTiming(calendar.shift(entry, -1), entry, calendar.shift(entry, 1), entry.closes_at - timedelta(minutes=30))


def build(harness, calendar, *, labels=False):
	repository = VerifiedCloseRepository(harness.source, MySQLValidationRepository(harness.source, harness.database))
	return build_sample(FeatureSpec((1,), 3), TargetSpec(), decision(calendar), harness.boundary, calendar, harness.settings.publication, repository, label_cutoff=harness.clock.now() if labels else None)


def validate(harness, calendar, features=None):
	settings = ValidationSettings(harness.settings.start, harness.settings.end, harness.settings.publication, features=features)
	return validate_database(harness.source, settings, harness.boundary, calendar, MySQLValidationRepository(harness.source, harness.database), harness.clock)


def test_training_and_inference_share_real_read_only_database_projection(chronological_storage, calendar):
	harness = chronological_storage
	commits = harness.database.commits
	files = {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	harness.database.statements.clear()
	inference = build(harness, calendar)
	training = build(harness, calendar, labels=True)
	assert inference.features.values == pytest.approx((0, .1, .21))
	assert training.features == inference.features
	assert training.targets.raw == pytest.approx((.05,))
	assert training.training_eligible
	assert inference.feature_cache_key == training.feature_cache_key
	assert harness.database.commits == commits
	assert all(statement.startswith(("SELECT", "START TRANSACTION")) for statement, parameters in harness.database.statements)
	assert all(config == harness.source.database_config for config in harness.database.configs)
	assert files == {path: path.read_bytes() for path in harness.source.data_root.rglob("*") if path.is_file()}
	for statement, parameters in harness.database.statements:
		if "WHERE t.transaction_date" in statement:
			assert all(value >= date(2024, 7, 1) for value in parameters)
			assert all(calendar.session(value).closes_at <= harness.clock.now() for value in parameters)


def test_nonconsecutive_nonchronological_database_ids_do_not_change_samples(chronological_storage, calendar):
	harness = chronological_storage
	before = build(harness, calendar, labels=True)
	for old, new in ((1, 901), (2, 11), (3, 450), (4, 2), (5, 100)):
		for table, column in (("raw_stock_data", "date_id"), ("daily_delta_stock_data", "date_id"), ("transaction_dates", "id")):
			harness.database.sql(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new, old))
	after = build(harness, calendar, labels=True)
	assert after == before


@pytest.mark.parametrize("damage", ["manifest", "raw", "price", "receipt", "duplicate"])
def test_unverified_invalid_and_missing_database_values_cannot_become_zero_samples(chronological_storage, calendar, damage):
	harness = chronological_storage
	if damage == "manifest":
		ManifestStore(harness.source.data_root).path_for(harness.source.identity, calendar.session(date(2024, 7, 2))).unlink()
	elif damage == "raw":
		harness.database.sql("DELETE FROM raw_stock_data WHERE date_id = 2")
	elif damage == "price":
		harness.database.sql("UPDATE raw_stock_data SET close = 0 WHERE date_id = 2")
	elif damage == "receipt":
		harness.database.sql("DELETE FROM stockguidance_imports WHERE session_date = '2024-07-02'")
	else:
		harness.database.sql("CREATE TABLE raw_copy AS SELECT * FROM raw_stock_data")
		harness.database.sql("DROP TABLE raw_stock_data")
		harness.database.sql("ALTER TABLE raw_copy RENAME TO raw_stock_data")
		harness.database.sql("INSERT INTO raw_stock_data SELECT * FROM raw_stock_data WHERE date_id = 2")
	sample = build(harness, calendar, labels=True)
	assert sample.features.values is None
	assert sample.features.issues and not sample.training_eligible
	assert sample.targets.raw == pytest.approx((.05,))


def test_provider_absence_preserves_fixed_ticker_layout(chronological_storage, calendar):
	harness = chronological_storage
	repository = VerifiedCloseRepository(harness.source, MySQLValidationRepository(harness.source, harness.database))
	sample = build_sample(FeatureSpec((99, 1), 3), TargetSpec(), decision(calendar), harness.boundary, calendar, harness.settings.publication, repository, label_cutoff=harness.clock.now())
	assert sample.features.values is None
	assert sample.features.specification.ticker_ids == (99, 1)
	assert sample.targets.raw == pytest.approx((None, .05))
	assert not sample.training_eligible


def test_correction_invalidates_revision_and_does_not_reconstruct_prior_availability(chronological_storage, calendar):
	harness = chronological_storage
	before = build(harness, calendar)
	harness.provider.responses[date(2024, 7, 2)] = harness.response(close="120.0000")
	harness.clock.value = datetime(2024, 7, 10, 23, tzinfo=timezone.utc)
	assert harness.run().successful
	after = build(harness, calendar)
	assert before.features.values is not None and after.features.values is None
	assert before.feature_cache_key != after.feature_cache_key
	assert not after.training_eligible


def test_validation_reports_exact_close_features_targets_and_legacy_semantics_separately(chronological_storage, calendar):
	harness = chronological_storage
	result = validate(harness, calendar, FeatureSpec((1,), 3))
	assert result.complete
	sample = next(sample for sample in result.samples if sample.timing.entry_session.session_date == date(2024, 7, 5))
	assert sample.features.values == pytest.approx((0, .1, .21))
	assert sample.targets.raw == pytest.approx((.05,))
	assert sample.target_state is TargetState.READY
	payload = report_payload(result)
	assert payload["derived_spec"] == "legacy-open-to-close-v1"
	assert payload["feature_specification"]["ticker_ids"] == (1,)
	day = next(item for item in payload["sessions"] if item["date"] == "2024-07-05")
	assert ValidationCheck.TARGET.value in day["checks"]
	assert ValidationCheck.FEATURE.value in day["checks"]
	assert any(finding.check is ValidationCheck.FEATURE and finding.category is FindingCategory.EXPECTED_ABSENCE for finding in result.findings)
	assert any(sample.target_state is TargetState.NOT_YET_AVAILABLE for sample in result.samples)
	assert "fixture-credential-reference" not in json.dumps(payload)
	assert "custom-source.id" not in json.dumps(payload)


def test_mature_close_target_is_validated_when_feature_history_is_unavailable(chronological_storage, calendar):
	harness = chronological_storage
	result = validate(harness, calendar)
	first = result.samples[0]
	assert first.features.values is None
	assert first.targets.raw == pytest.approx((.1,))
	assert first.targets.raw[0] != pytest.approx(110 / 10 - 1)
	assert not first.training_eligible


def test_narrow_validation_loads_full_feature_window_beyond_legacy_context(chronological_storage, calendar):
	harness = chronological_storage
	settings = ValidationSettings(date(2024, 7, 5), date(2024, 7, 5), harness.settings.publication, features=FeatureSpec((1,), 3))
	result = validate_database(harness.source, settings, harness.boundary, calendar, MySQLValidationRepository(harness.source, harness.database), harness.clock)
	assert result.complete
	assert result.samples[0].features.values == pytest.approx((0, .1, .21))
	assert result.samples[0].targets.raw == pytest.approx((.05,))
	assert result.queried_sessions[0].session_date == harness.boundary.history_start
	assert all(session.session_date >= harness.boundary.history_start and session.closes_at <= harness.clock.now() for session in result.queried_sessions)


def test_long_lookback_does_not_query_before_supported_history(chronological_storage, calendar):
	harness = chronological_storage
	result = validate(harness, calendar, FeatureSpec((1,), 10))
	assert result.complete
	assert all(sample.features.values is None for sample in result.samples)
	assert all(session.session_date >= harness.boundary.history_start for session in result.queried_sessions)
	assert any(finding.check is ValidationCheck.FEATURE and finding.category is FindingCategory.EXPECTED_ABSENCE for finding in result.findings)


def test_missing_mature_dependency_is_blocking_and_repair_uses_source_cause(chronological_storage, calendar):
	harness = chronological_storage
	harness.database.sql("DELETE FROM raw_stock_data WHERE date_id = 5")
	harness.database.sql("DELETE FROM daily_delta_stock_data WHERE date_id = 5")
	result = validate(harness, calendar)
	assert not result.complete
	assert any(finding.check is ValidationCheck.TARGET and finding.blocking and finding.session_date == date(2024, 7, 5) and finding.dependency_session_date == date(2024, 7, 8) for finding in result.findings)
	assert result.repair_plan.items
	result.repair_plan.validate_for_update(harness.source.identity, harness.settings.start, harness.settings.end, harness.clock.now())
	assert harness.run(replace(harness.settings, repair_plan=result.repair_plan)).successful
	assert validate(harness, calendar).complete


def test_cli_exact_feature_validation_remains_read_only(chronological_storage, calendar, monkeypatch, capsys):
	from stockguidance.cli import validation_setup
	harness = chronological_storage
	monkeypatch.setattr(validation_setup, "SystemClock", lambda: harness.clock)
	monkeypatch.setattr(validation_setup, "LegacyWriteConnectionFactory", lambda: harness.database)
	commits = harness.database.commits
	harness.database.statements.clear()
	arguments = ["--data-root", str(harness.source.data_root), "--dbinfo-file", str(harness.source.database_config), "--history-start", "2024-07-01", "--history-evidence", "Fixture-only boundary", "--end-date", "2024-07-08", "--publication-delay-minutes", "15", "--publication-evidence", "Fixture-only policy", "--ticker-ids", "1", "--lookback-sessions", "3", "--format", "json"]
	assert main(arguments) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["feature_specification"]["ticker_ids"] == [1]
	assert any(item["features"] is not None for item in payload["samples"])
	assert harness.database.commits == commits
	assert all(statement.startswith(("SELECT", "START TRANSACTION")) for statement, parameters in harness.database.statements)


@pytest.mark.parametrize("arguments", [["--ticker-ids", "1"], ["--lookback-sessions", "3"], ["--ticker-ids", "1,1", "--lookback-sessions", "3"], ["--ticker-ids", "1,abc", "--lookback-sessions", "3"], ["--ticker-ids", "1", "--lookback-sessions", "0"]])
def test_cli_invalid_feature_specs_fail_before_database_work(tmp_path, monkeypatch, capsys, arguments):
	from stockguidance.cli import validation_setup
	monkeypatch.setattr(validation_setup, "MySQLValidationRepository", lambda *args: pytest.fail("Invalid specs reached storage"))
	assert main(["--data-root", str(tmp_path), "--format", "json", *arguments]) == 1
	assert "Invalid validation configuration" in capsys.readouterr().out
	assert list(tmp_path.iterdir()) == []
