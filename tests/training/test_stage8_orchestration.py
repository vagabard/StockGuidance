from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from stockguidance.artifacts.loading import load_artifact
from stockguidance.calendar.ranges import (
	AbsoluteDate, DatePlanSpec, DependencySpec, ExplicitRange, SourceCoverageSnapshot,
)
from stockguidance.config.source import SourceConfig
from stockguidance.datasets.building import build_sample
from stockguidance.features.specification import FeatureChannel
from stockguidance.inference.prediction import predict_sample
from stockguidance.models.specification import ModelSpec
from stockguidance.preprocessing import PreprocessingSpec
from stockguidance.runs.service import RunDirectoryService
from stockguidance.targets.specification import TargetKind, TargetSpecV2
from stockguidance.training.configured import (
	ConfiguredTrainingJob, execute_configured_training, resolve_configured_training,
)
from stockguidance.training.configuration import DeviceChoice, LossObjective, LossSpec, TrainingConfig
from stockguidance.universe.selection import (
	RawObservationState, SourceSessionEvidence, TickerIdentity, UniverseObservation,
	UniverseSourceData, UniverseSelectionRequest,
)
from stockguidance.validation.coverage import CoverageState


def _configured_job(samples):
	plan = DatePlanSpec(
		ExplicitRange(AbsoluteDate(date(2024, 7, 3)), AbsoluteDate(date(2024, 7, 5))),
		ExplicitRange(AbsoluteDate(date(2024, 7, 9)), AbsoluteDate(date(2024, 7, 11))),
		DependencySpec(model_lookback_sessions=1, simulation_horizon_sessions=3),
	)
	configuration = TrainingConfig(
		model=ModelSpec(hidden_widths=(), implementation_version="stockguidance-models-v2"),
		loss=LossSpec(LossObjective.RMSE, "loss-v2"), epochs=1, batch_size=1,
		learning_rate=0.01, device=DeviceChoice.CPU, seed=17,
	)
	return ConfiguredTrainingJob(
		SourceConfig(samples.boundary.source, Path("fixture-data"), Path("fixture-db.id")),
		plan, 1, (FeatureChannel.RELATIVE_CLOSE,), configuration,
		samples.publication, timedelta(minutes=30),
		TargetSpecV2(kind=TargetKind.RAW_RETURN), PreprocessingSpec("standard", "min_max"),
	)


def _coverage(samples):
	dates = tuple(date(2024, 7, day) for day in (1, 2, 3, 5, 8, 9, 10, 11))
	return SourceCoverageSnapshot.from_verified_dates(samples.boundary.source, samples.calendar.calendar_id, "stage8-source-revision", dates, snapshot_id="stage8-snapshot")


def _universe(samples, preview):
	request = UniverseSelectionRequest.from_dependency_plan(preview.plan, preview.calendar)
	coverage = tuple(SourceSessionEvidence(session, CoverageState.COMPLETE, "fixture complete", "stage8-source-revision") for session in request.required_sessions)
	by_key = samples.repository.observations
	observations = tuple(
		UniverseObservation(ticker_id, session, RawObservationState.AVAILABLE, Decimal(str(by_key[(session, ticker_id)].close)), revision="stage8-source-revision")
		for session in request.required_sessions for ticker_id in (17, 3)
	)
	return UniverseSourceData((TickerIdentity(17, "AAA"), TickerIdentity(3, "BBB")), coverage, observations, "stage8-source-revision")


def test_stage8_preview_resolves_without_fitting_or_loading_torch(samples):
	job = _configured_job(samples)
	preview = resolve_configured_training(job, _coverage(samples))
	assert preview.plan.training_information_cutoff.session_date == date(2024, 7, 8)
	assert preview.plan.final_evaluation_open_position_session.session_date == date(2024, 7, 11)


def test_stage8_resolution_failure_is_saved_as_a_failed_run(samples, tmp_path):
	runs = RunDirectoryService(tmp_path / "runs", project=tmp_path)
	with pytest.raises(ValueError, match="coverage snapshot"):
		execute_configured_training(_configured_job(samples), tmp_path / "requested.sgmodel", repository=samples.repository, boundary=samples.boundary, runs=runs)
	summaries = runs.list_runs(include_invalid=False)
	assert len(summaries) == 1 and summaries[0].manifest is not None
	assert summaries[0].manifest.status.value == "failed"
	assert summaries[0].manifest.failure is not None and summaries[0].manifest.failure.stage == "resolution"


def test_stage8_configured_training_exports_v2_artifact_and_saved_run(samples, tmp_path):
	job = _configured_job(samples)
	coverage = _coverage(samples)
	preview = resolve_configured_training(job, coverage)
	result = execute_configured_training(
		job, tmp_path / "requested.sgmodel", coverage=coverage, universe_data=_universe(samples, preview),
		repository=samples.repository, boundary=samples.boundary,
		runs=RunDirectoryService(tmp_path / "runs", project=tmp_path), checkpoint=True,
	)
	loaded = load_artifact(result.training.artifact)
	assert result.run_id is not None
	assert loaded.metadata.semantics.features.ticker_ids == (3, 17)
	assert loaded.metadata.universe is not None and loaded.metadata.universe.ticker_labels == ("BBB", "AAA")
	assert loaded.metadata.preprocessing is not None
	assert loaded.metadata.loss is not None and loaded.metadata.loss.objective is LossObjective.RMSE
	manifest = RunDirectoryService(tmp_path / "runs", project=tmp_path).validate(result.run_id)
	assert manifest.status.value == "completed"
	assert manifest.results["predictions"]["rows"]
	checkpoint = tmp_path / "runs" / result.run_id / "checkpoints" / "training.pt"
	assert checkpoint.is_file()
	resumed_job = replace(job, configuration=replace(job.configuration, epochs=2))
	resumed = execute_configured_training(
		resumed_job, tmp_path / "resumed.sgmodel", coverage=coverage, universe_data=_universe(samples, preview),
		repository=samples.repository, boundary=samples.boundary,
		runs=RunDirectoryService(tmp_path / "runs", project=tmp_path), checkpoint=True, resume_from=checkpoint,
	)
	assert resumed.run_id is not None and resumed.training.completed_epochs == 2


def test_stage8_reordered_same_width_universe_is_rejected(samples, tmp_path):
	job = replace(_configured_job(samples), ordered_ticker_ids=(17, 3))
	coverage = _coverage(samples)
	preview = resolve_configured_training(job, coverage)
	result = execute_configured_training(job, coverage=coverage, universe_data=_universe(samples, preview), repository=samples.repository, boundary=samples.boundary, runs=RunDirectoryService(tmp_path / "runs", project=tmp_path))
	loaded = load_artifact(result.training.artifact)
	assert loaded.metadata.universe is not None
	assert loaded.metadata.universe.ticker_ids == (17, 3)
	assert result.universe.ordered_ticker_ids == (17, 3)
	bad_specification = replace(loaded.metadata.semantics.features, ticker_ids=(3, 17))
	bad_sample = build_sample(
		bad_specification, loaded.metadata.semantics.target, samples.decision(9), samples.boundary,
		samples.calendar, samples.publication, samples.repository,
	)
	with pytest.raises(ValueError, match="semantics differ"):
		predict_sample(loaded, bad_sample)
