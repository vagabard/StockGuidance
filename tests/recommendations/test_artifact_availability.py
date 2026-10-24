from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.artifacts.ensembles import export_ensemble, load_ensemble
from stockguidance.artifacts.exporting import export_artifact
from stockguidance.artifacts.loading import load_artifact
from stockguidance.datasets.building import build_sample
from stockguidance.features.specification import CloseFeature
from stockguidance.inference.prediction import predict_sample
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.workflows import recommend as workflow
from stockguidance.workflows.failures import StageExecutionError, WorkflowStage
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


@pytest.mark.parametrize("problem", ["missing", "changed_identity", "transform", "source", "information_cutoff", "corrupt"])
def test_current_artifacts_fail_before_refresh_or_database_side_effects(recommender, monkeypatch, tmp_path, problem):
	job = recommender.job
	if problem == "missing":
		job = replace(job, current_artifact=ArtifactReference(tmp_path / "missing.sgmodel", job.model.artifact_id))
	elif problem == "changed_identity":
		loaded = load_artifact(job.current_artifact)
		metadata = replace(loaded.metadata, training=replace(loaded.metadata.training, seed=999))
		changed = export_artifact(job.current_artifact.path, loaded.model, metadata)
		assert changed.artifact_id != job.current_artifact.artifact_id
	elif problem == "transform":
		job = replace(job, features=replace(job.features, feature_order=(CloseFeature.LOG_CLOSE,)))
	elif problem == "source":
		job = replace(job, source=replace(job.source, data_root=job.source.data_root / "missing-evidence"))
		loaded = workflow.load_recommendation_artifact(job.current_artifact, job.artifact_kind)
		metadata = replace(loaded.metadata, semantics=replace(loaded.metadata.semantics, source=replace(job.source.identity, universe="different-universe")))
		monkeypatch.setattr(workflow, "load_recommendation_artifact", lambda *arguments: replace(loaded, metadata=metadata))
	elif problem == "information_cutoff":
		loaded = workflow.load_recommendation_artifact(job.current_artifact, job.artifact_kind)
		future = job.allocation.anchor_session.closes_at + timedelta(days=10)
		metadata = replace(loaded.metadata, training=replace(loaded.metadata.training, information_cutoff=future, training_cutoff=future))
		monkeypatch.setattr(workflow, "load_recommendation_artifact", lambda *arguments: replace(loaded, metadata=metadata))
	else:
		job.current_artifact.path.write_bytes(b"legacy-manifest-or-controller")
	monkeypatch.setattr(workflow, "update_market_data", lambda *arguments: pytest.fail("Incompatible artifact reached refresh"))
	with pytest.raises(StageExecutionError) as error:
		recommender.run(job)
	assert error.value.failure.stage is WorkflowStage.ARTIFACT
	assert not recommender.storage.database.statements and not recommender.storage.provider.calls


def test_no_current_session_finalized_input_is_queried_even_if_future_rows_exist(recommender):
	queries = []

	class RecordingCloses:
		def read(self, query):
			queries.append(query)
			assert all(session.session_date < recommender.job.entry_date for session in query.scope.sessions)
			return recommender.closes.read(query)

	class RecordingLiquidity:
		def read(self, query):
			queries.append(query)
			assert query.scope.sessions == (recommender.calendar.session(date(2024, 7, 8)),)
			return recommender.services.eligibility.read(query)

	run = recommender.run(services_override=replace(recommender.services, closes=RecordingCloses(), eligibility=RecordingLiquidity()))
	assert queries and all(query.scope.as_of == run.sample.timing.decision_cutoff for query in queries)
	assert run.sample.targets.raw == (None,)
	assert all("2024-07-09" not in str(parameters) for statement, parameters in recommender.storage.database.statements if statement.startswith("SELECT"))


def test_new_data_availability_uses_the_post_refresh_decision_cutoff(recommender, monkeypatch):
	original = workflow.update_market_data
	started = recommender.storage.clock.now()

	def update(*arguments):
		result = original(*arguments)
		recommender.storage.clock.value += timedelta(minutes=3)
		return result

	monkeypatch.setattr(workflow, "update_market_data", update)
	run = recommender.run()
	assert run.recommendation.request.timing.decision_cutoff == started + timedelta(minutes=3)
	assert run.validation.plan.request.as_of == run.sample.timing.decision_cutoff
	assert run.recommendation.request.coverage.as_of == run.sample.timing.decision_cutoff


def test_ensemble_members_and_weights_are_preserved_without_score_normalization(recommender, tmp_path):
	plan = recommender.plan
	second = train_from_history(HistoryTrainingRequest(plan.decisions, recommender.storage.clock.now(), recommender.job.features, recommender.job.target, replace(plan.configuration, seed=32), tmp_path / "second.sgmodel"), recommender.storage.boundary, recommender.calendar, recommender.job.publication, recommender.closes)
	reference = export_ensemble(tmp_path / "ensemble.sgmodel", (recommender.training.artifact, second.artifact), (0.25, 0.75))
	job = replace(recommender.job, model=replace(recommender.job.model, artifact_id=reference.artifact_id), current_artifact=reference, artifact_kind=ArtifactKind.ENSEMBLE)
	run = recommender.run(job)
	assert run.member_artifact_ids == (recommender.training.artifact.artifact_id, second.artifact.artifact_id)
	assert run.member_weights == (0.25, 0.75)
	assert tuple(item.score for item in run.scores) == predict_sample(load_ensemble(reference), run.sample)
	assert run.recommendation.artifact_id == reference.artifact_id


def test_missing_or_late_previous_closes_fail_without_current_day_substitution(recommender):
	class UnavailableCloses:
		def read(self, query):
			from stockguidance.database.repositories.closes import CloseData
			from stockguidance.market_data.closes import CloseIssue, PriceState
			return CloseData(query, (), tuple(CloseIssue(ticker_id, session, PriceState.NOT_YET_AVAILABLE, "Fixture receipt is after cutoff") for session in query.scope.sessions for ticker_id in query.ticker_ids))

	with pytest.raises(StageExecutionError) as error:
		recommender.run(services_override=replace(recommender.services, closes=UnavailableCloses()))
	assert error.value.failure.stage is WorkflowStage.VALIDATION


def test_missing_transactions_in_a_verified_bar_are_unavailable(recommender, monkeypatch):
	from stockguidance.database.repositories.closes import CloseQuery
	from stockguidance.database.repositories.sessions import SessionQuery

	query = CloseQuery(SessionQuery(recommender.storage.boundary, (recommender.calendar.session(date(2024, 7, 8)),), recommender.storage.clock.now()), recommender.job.features.ticker_ids)
	data, inspection = recommender.closes.inspect(query)
	snapshot = inspection.snapshots[0]
	inspection = replace(inspection, snapshots=(replace(snapshot, records=tuple(replace(bar, transactions=None) for bar in snapshot.records)),))
	monkeypatch.setattr(recommender.closes, "inspect", lambda request: (data, inspection))
	assert recommender.services.eligibility.read(query) == ()
