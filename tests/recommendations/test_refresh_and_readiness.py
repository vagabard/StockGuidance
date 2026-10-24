from dataclasses import replace
from datetime import date, timedelta

import pytest

from support.recommendations import retraining_job
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.validation.findings import FindingCategory, ValidationCheck, ValidationFinding
from stockguidance.workflows import recommend as workflow
from stockguidance.workflows.failures import StageExecutionError, WorkflowStage


def test_newly_ingested_date_mapping_is_read_only_after_refresh(recommender):
	storage = recommender.storage
	old_id = storage.database.sql("SELECT id FROM transaction_dates WHERE transaction_date = ?", ("2024-07-08",))[0][0]
	for table in ("raw_stock_data", "daily_delta_stock_data"):
		storage.database.sql(f"DELETE FROM {table} WHERE date_id = ?", (old_id,))
	storage.database.sql("DELETE FROM stockguidance_imports WHERE session_date = ?", ("2024-07-08",))
	storage.database.sql("DELETE FROM transaction_dates WHERE id = ?", (old_id,))
	events = []
	original = recommender.services.validation

	class AfterIngestion:
		def inspect(self, query):
			new_id = storage.database.sql("SELECT id FROM transaction_dates WHERE transaction_date = ?", ("2024-07-08",))
			assert new_id and new_id[0][0] != old_id
			events.append(new_id[0][0])
			return original.inspect(query)

	validation = AfterIngestion()
	closes = VerifiedCloseRepository(recommender.job.source, validation)
	run = recommender.run(services_override=replace(recommender.services, validation=validation, closes=closes))
	assert events and run.refresh.successful and run.validation.complete
	assert run.sample.features.values == pytest.approx((0.0, 0.05))
	assert all(item.observation.timing.available_at <= run.sample.timing.decision_cutoff for item in run.eligibility)


def test_entire_coverage_repairs_a_leading_gap_before_recommendation(recommender):
	storage = recommender.storage
	first_id = storage.database.sql("SELECT id FROM transaction_dates WHERE transaction_date = ?", ("2024-07-01",))[0][0]
	storage.database.sql("DELETE FROM raw_stock_data WHERE date_id = ?", (first_id,))
	run = recommender.run()
	assert storage.database.sql("SELECT COUNT(*) FROM raw_stock_data WHERE date_id = ?", (first_id,)) == [(1,)]
	assert run.refresh.successful and run.validation.complete


def test_fresh_download_receipt_is_usable_at_the_recorded_post_refresh_cutoff(recommender):
	storage = recommender.storage
	session = recommender.calendar.session(date(2024, 7, 8))
	ManifestStore(storage.source.data_root).path_for(storage.source.identity, session).unlink()
	started = storage.clock.now()

	class Provider:
		def fetch(self, source, requested_session):
			assert source == recommender.job.source and requested_session == session
			storage.clock.value += timedelta(minutes=1)
			return storage.provider.fetch(source, requested_session)

	run = recommender.run(services_override=replace(recommender.services, provider=Provider()))
	assert run.refresh.downloaded_sessions == 1 and len(storage.provider.calls) == 1
	assert run.sample.timing.decision_cutoff == started + timedelta(minutes=1)
	assert run.eligibility[0].observation.timing.available_at == run.sample.timing.decision_cutoff
	assert run.sample.features.values == pytest.approx((0.0, 0.05))


def test_real_provider_failure_prevents_training_and_prediction(recommender):
	storage = recommender.storage
	storage.provider.errors.add(date(2024, 7, 8))
	job = retraining_job(recommender)
	job = replace(job, corrections=replace(job.corrections, recent_recheck=timedelta(minutes=1)))
	with pytest.raises(StageExecutionError) as error:
		recommender.run(job, trainer=lambda *values: pytest.fail("Provider failure reached training"))
	assert error.value.failure.stage is WorkflowStage.REFRESH
	assert not job.retraining.artifact_path.exists()


def test_readiness_with_blocking_findings_prevents_training(recommender, monkeypatch):
	original = workflow.validate_database

	def validation(*values):
		result = original(*values)
		return replace(result, findings=(*result.findings, ValidationFinding(FindingCategory.UNVERIFIED, ValidationCheck.SOURCE, "Fixture unverified evidence")))

	monkeypatch.setattr(workflow, "validate_database", validation)
	with pytest.raises(StageExecutionError) as error:
		recommender.run(retraining_job(recommender), trainer=lambda *values: pytest.fail("Unverified source reached training"))
	assert error.value.failure.stage is WorkflowStage.VALIDATION


def test_refresh_that_passes_entry_close_stops_before_training(recommender, monkeypatch):
	original = workflow.update_market_data

	def update(*values):
		result = original(*values)
		recommender.storage.clock.value = recommender.calendar.session(recommender.job.entry_date).closes_at
		return result

	monkeypatch.setattr(workflow, "update_market_data", update)
	with pytest.raises(StageExecutionError) as error:
		recommender.run(retraining_job(recommender), trainer=lambda *values: pytest.fail("Expired entry reached training"))
	assert error.value.failure.stage is WorkflowStage.VALIDATION


def test_invalid_coverage_fails_before_artifact_loading_or_refresh(recommender, monkeypatch):
	monkeypatch.setattr(workflow, "load_recommendation_artifact", lambda *values: pytest.fail("Invalid request reached artifact loading"))
	for job in (replace(recommender.job, start=date(2024, 6, 28)), replace(recommender.job, start=date(2024, 7, 8)), replace(recommender.job, end=date(2024, 7, 5))):
		with pytest.raises(StageExecutionError) as error:
			recommender.run(job)
		assert error.value.failure.stage is WorkflowStage.REQUEST
	assert not recommender.storage.database.statements and not recommender.storage.provider.calls
