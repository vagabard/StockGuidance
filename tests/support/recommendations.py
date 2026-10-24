from dataclasses import asdict, replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from support.samples import calendar, chronological_storage, harness, samples
from support.training import cpu_threads
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.database.repositories.eligibility import VerifiedEligibilityRepository
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.models.policy import ModelRefreshPolicy, ModelSelection
from stockguidance.models.specification import ModelSpec
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec
from stockguidance.recommendations.configuration import RecommendationJob, RetrainingPlan
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.targets.specification import TargetSpec
from stockguidance.training.configuration import DeviceChoice, TrainingConfig
from stockguidance.workflows.recommend import recommend
from stockguidance.workflows.recommendation_dependencies import RecommendationServices
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


@pytest.fixture
def recommender(chronological_storage, calendar, tmp_path):
	storage = chronological_storage
	validation = MySQLValidationRepository(storage.source, storage.database)
	closes = VerifiedCloseRepository(storage.source, validation)
	ticker_id = storage.database.sql("SELECT id FROM ticker_symbol WHERE label = 'AAA'")[0][0]
	features = FeatureSpec((ticker_id,), 2)
	configuration = TrainingConfig(model=ModelSpec(hidden_widths=(4,)), epochs=2, batch_size=2, learning_rate=0.01, device=DeviceChoice.CPU, seed=31)
	decisions = tuple(DecisionTiming(calendar.shift(entry, -1), entry, calendar.shift(entry, 1), entry.closes_at - timedelta(minutes=30)) for entry in calendar.sessions(date(2024, 7, 3), date(2024, 7, 8)))
	plan = RetrainingPlan(decisions, configuration, tmp_path / "requested-output.sgmodel")
	training = train_from_history(HistoryTrainingRequest(decisions, storage.clock.now(), features, TargetSpec(), configuration, tmp_path / "current.sgmodel"), storage.boundary, calendar, storage.settings.publication, closes)
	services = RecommendationServices(storage.provider, storage.repository, validation, closes, VerifiedEligibilityRepository(closes))
	job = RecommendationJob(storage.source, storage.boundary.history_start, date(2024, 7, 9), date(2024, 7, 9), features, TargetSpec(), ModelSelection(ModelRefreshPolicy.CURRENT, training.artifact.artifact_id), AllocationSpec(AllocationPolicy.ALTERNATE_SESSIONS, calendar.session(date(2024, 7, 1))), storage.settings.publication, replace(storage.settings.corrections, recent_recheck=timedelta(days=30)), EligibilityPolicy(1), 5, training.artifact)
	JsonHistoryStore(storage.source.data_root / "history").save(storage.boundary)
	storage.provider.calls.clear()
	storage.database.statements.clear()

	def run(job_override=None, *, services_override=None, trainer=None):
		return recommend(job_override or job, storage.boundary, calendar, services_override or services, storage.clock, trainer=trainer)

	return SimpleNamespace(storage=storage, calendar=calendar, closes=closes, services=services, job=job, plan=plan, training=training, run=run)


@pytest.fixture
def job_json(recommender):
	job = recommender.job
	return {
		"source": asdict(job.source.identity), "data_root": str(job.source.data_root), "database_config": str(job.source.database_config),
		"features": asdict(job.features), "start_date": job.start.isoformat(), "end_date": job.end.isoformat(), "entry_date": job.entry_date.isoformat(),
		"allocation": {"policy": job.allocation.policy.value, "anchor_date": job.allocation.anchor_session.session_date.isoformat()},
		"publication": {"delay_minutes": 15, "evidence": "Fixture-only publication"},
		"corrections": {"recent_window_days": 30, "recent_refresh_hours": 720, "archive_refresh_days": 30, "evidence": "Fixture-only corrections"},
		"eligibility": {"minimum_transactions": 1}, "top_k": 5, "retraining": None,
	}


def retraining_job(recommender):
	return replace(recommender.job, model=ModelSelection(ModelRefreshPolicy.RETRAIN), current_artifact=None, retraining=recommender.plan)
