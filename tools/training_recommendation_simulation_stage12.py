"""Run the deterministic Stage 12 workflow rehearsal.

This tool is deliberately fixture-only.  It uses the typed preparation,
date, universe, training, recommendation, simulation, replay, and rerun
interfaces with in-memory source adapters and writes all generated artifacts
below an explicitly supplied output directory.  It never opens a configured
database or provider.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from stockguidance.artifacts.loading import load_artifact
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.calendar.ranges import (
	AbsoluteDate,
	DatePlanSpec,
	DependencySpec,
	ExplicitRange,
	SourceCoverageSnapshot,
)
from stockguidance.config.source import PriceAdjustment, SourceConfig, SourceIdentity
from stockguidance.database.repositories.closes import CloseData, CloseQuery
from stockguidance.features.channels import (
	FeatureChannelData,
	FeatureChannelObservation,
	FeatureChannelQuery,
	FeatureChannelRepository,
)
from stockguidance.features.specification import FeatureSpec, FeatureSpecV2
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.closes import CloseIssue, CloseObservation, PriceState
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.providers.contracts import MarketDataProvider, ProviderResponse
from stockguidance.models.policy import ModelRefreshPolicy, ModelSelection
from stockguidance.models.specification import ModelSpec
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec
from stockguidance.recommendations.configuration import RecommendationJob, RefreshMode
from stockguidance.recommendations.eligibility import (
	EligibilityPolicy,
	LiquidityObservation,
)
from stockguidance.preprocessing import NormalizationMethod, PreprocessingSpec
from stockguidance.runs.contracts import RunOperation
from stockguidance.runs.service import RunDirectoryService
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.simulation.persistence import (
	persist_paired_simulation,
	rerun_pinned_simulation,
	replay_saved_simulation,
)
from stockguidance.simulation.prices import RepositoryExecutionPrices
from stockguidance.simulation.score_source import (
	FixedModelScores,
	HistoricalSignals,
	PreEvaluationTraining,
	TrainThenSimulateScores,
)
from stockguidance.targets.specification import TargetKind, TargetSpec, TargetSpecV2
from stockguidance.training.configuration import DeviceChoice, LossObjective, LossSpec, TrainingConfig
from stockguidance.training.configured import (
	ConfiguredTrainingJob,
	execute_configured_training,
	prepare_configured_training,
	resolve_configured_training,
)
from stockguidance.universe.selection import (
	AverageVolumeFilter,
	RawObservationState,
	SourceSessionEvidence,
	TickerIdentity,
	UniverseFilters,
	UniverseObservation,
	UniverseSelectionRequest,
	UniverseSourceData,
)
from stockguidance.validation.coverage import CoverageState
from stockguidance.workflows.recommend import recommend
from stockguidance.workflows.recommendation_dependencies import RecommendationServices
from stockguidance.workflows.recommendation_result import ValidationOnlyRefresh


TICKERS = ((101, "AAA"), (202, "BBB"), (303, "GAP"), (404, "LOWVOL"))
SELECTED_TICKERS = (101, 202)
SOURCE_REVISION = "stage12-deterministic-source-revision"


@dataclass(frozen=True)
class FrozenClock:
	value: datetime

	def now(self) -> datetime:
		return self.value


class FixtureCloses:
	def __init__(self, calendar: ExchangeCalendar, sessions: tuple[Any, ...]) -> None:
		self.calendar = calendar
		self.observations: dict[tuple[Any, int], CloseObservation] = {}
		for session in sessions:
			for ticker_id, _ in TICKERS:
				base = {101: 100.0, 202: 80.0, 303: 60.0, 404: 40.0}[ticker_id]
				slope = {101: 0.70, 202: 1.40, 303: 0.90, 404: 0.35}[ticker_id]
				close = base + slope * session.sequence + ((session.sequence + ticker_id) % 5) * 0.10
				timing = ObservationTiming(session, session.closes_at, session.closes_at + timedelta(minutes=15))
				self.observations[(session, ticker_id)] = CloseObservation(ticker_id, close, timing, f"{SOURCE_REVISION}-{session.sequence}-{ticker_id}")
		self.missing: set[tuple[Any, int]] = set()

	def read(self, query: CloseQuery) -> CloseData:
		observations: list[CloseObservation] = []
		issues: list[CloseIssue] = []
		for session in query.scope.sessions:
			for ticker_id in query.ticker_ids:
				key = (session, ticker_id)
				observation = self.observations.get(key)
				if key in self.missing or observation is None:
					issues.append(CloseIssue(ticker_id, session, PriceState.MISSING, "Stage 12 interior fixture gap"))
				elif not observation.timing.is_available_at(query.scope.as_of):
					issues.append(CloseIssue(ticker_id, session, PriceState.NOT_YET_AVAILABLE, "Stage 12 fixture receipt follows the cutoff"))
				else:
					observations.append(observation)
		return CloseData(query, tuple(observations), tuple(issues))


class FixtureChannels(FeatureChannelRepository):
	def __init__(self) -> None:
		self.calls: list[FeatureChannelQuery] = []

	def read(self, query: FeatureChannelQuery) -> FeatureChannelData:
		self.calls.append(query)
		observations = []
		for channel_index, channel in enumerate(query.specification.external_channels, start=1):
			for session in query.scored_sessions:
				for ticker_index, ticker_id in enumerate(query.specification.ticker_ids, start=1):
					value = float(session.sequence * channel_index * 0.001 + ticker_index * 0.01)
					observations.append(FeatureChannelObservation(channel, session, ticker_id, value, revision=SOURCE_REVISION))
		return FeatureChannelData(query, tuple(observations), f"{SOURCE_REVISION}-channels")


class FixtureLiquidity:
	def __init__(self, calendar: ExchangeCalendar) -> None:
		self.calendar = calendar
		self.calls: list[object] = []

	def read(self, query: object) -> tuple[LiquidityObservation, ...]:
		self.calls.append(query)
		timing_query = getattr(query, "scope")
		session = timing_query.sessions[0]
		timing = ObservationTiming(session, session.closes_at, session.closes_at + timedelta(minutes=15))
		return tuple(LiquidityObservation(ticker_id, 500, timing, SOURCE_REVISION) for ticker_id in getattr(query, "ticker_ids"))


class FixtureProvider:
	def __init__(self) -> None:
		self.calls: list[object] = []

	def fetch(self, source: SourceConfig, session: Any) -> ProviderResponse:
		self.calls.append((source, session))
		raise AssertionError("The validate-only Stage 12 fixture must not call the provider")


class FixturePreparation:
	def __init__(self) -> None:
		self.validated = 0
		self.prepared = 0

	def validate(self, job: ConfiguredTrainingJob, plan: object) -> None:
		self.validated += 1

	def prepare(self, job: ConfiguredTrainingJob, plan: object) -> None:
		self.prepared += 1


class FixtureReadiness:
	def __init__(self) -> None:
		self.calls = 0

	def validate_only(self, *args: object, **kwargs: object) -> SimpleNamespace:
		self.calls += 1
		return SimpleNamespace(
			update=ValidationOnlyRefresh(f"{SOURCE_REVISION}-validate-only", True),
			validation=SimpleNamespace(complete=True),
		)


def _sessions(calendar: ExchangeCalendar) -> tuple[Any, ...]:
	return calendar.sessions(date(2024, 1, 2), date(2024, 6, 28))


def _timing(calendar: ExchangeCalendar, session: Any, lead: timedelta = timedelta(minutes=30)) -> DecisionTiming:
	return DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - lead)


def _job(
	source: SourceConfig,
	calendar: ExchangeCalendar,
	coverage: SourceCoverageSnapshot,
	training_start: date,
	training_end: date,
	evaluation_start: date,
	evaluation_end: date,
	*,
	target: TargetSpecV2,
	preprocessing: PreprocessingSpec,
	objective: LossObjective,
	channels: tuple[str, ...],
	runs_root: Path,
) -> ConfiguredTrainingJob:
	plan = DatePlanSpec(
		ExplicitRange(AbsoluteDate(training_start), AbsoluteDate(training_end)),
		ExplicitRange(AbsoluteDate(evaluation_start), AbsoluteDate(evaluation_end)),
		DependencySpec(model_lookback_sessions=3, zscore_window_sessions=60, price_delta_predecessor_sessions=1, simulation_horizon_sessions=10),
	)
	configuration = TrainingConfig(
		model=ModelSpec(hidden_widths=(8,), implementation_version="stockguidance-models-v2"),
		loss=LossSpec(objective, "loss-v2", pair_block_size=2 if objective is LossObjective.PAIRWISE_RANKING else None),
		epochs=2, batch_size=1, learning_rate=0.001, device=DeviceChoice.CPU, seed=17,
	)
	return ConfiguredTrainingJob(
		source, plan, 3, channels, configuration,
		DailyPublicationPolicy(timedelta(minutes=15), "Stage 12 fixture publication"), timedelta(minutes=30),
		target, preprocessing,
		UniverseFilters(AverageVolumeFilter(window_sessions=20, lower=Decimal("10"))),
		availability_policy=AvailabilityPolicy.STRICT,
		calendar_start=date(2024, 1, 2), calendar_end=date(2024, 6, 28), coverage=coverage, runs_root=runs_root,
	)


def _universe_data(request: UniverseSelectionRequest, gap_session: Any) -> UniverseSourceData:
	coverage = tuple(SourceSessionEvidence(session, CoverageState.COMPLETE, "Stage 12 complete fixture session", SOURCE_REVISION) for session in request.required_sessions)
	observations: list[UniverseObservation] = []
	for session in request.required_sessions:
		for ticker_id, _ in TICKERS:
			if ticker_id == 303 and session == gap_session:
				continue
			close = Decimal("100") + Decimal(ticker_id) / Decimal("100") + Decimal(session.sequence) / Decimal("10")
			volume = Decimal("1") if ticker_id == 404 else Decimal("100") + Decimal(ticker_id % 10)
			observations.append(UniverseObservation(ticker_id, session, RawObservationState.AVAILABLE, close, volume, 500, SOURCE_REVISION))
	return UniverseSourceData(tuple(TickerIdentity(*item) for item in TICKERS), coverage, tuple(observations), SOURCE_REVISION)


def _coverage(source: SourceIdentity, calendar: ExchangeCalendar, sessions: tuple[Any, ...]) -> SourceCoverageSnapshot:
	return SourceCoverageSnapshot.from_verified_dates(source, calendar.calendar_id, SOURCE_REVISION, tuple(session.session_date for session in sessions), snapshot_id="stage12-deterministic-snapshot")


def _boundary(source: SourceIdentity, calendar: ExchangeCalendar, plan: object, clock: FrozenClock) -> HistoryBoundary:
	first = getattr(plan, "warmup_sessions")[0]
	return HistoryBoundary(source, calendar.calendar_id, first.session_date, HistoryProvenance.EXPLICIT_BOUNDARY, "Stage 12 deterministic fixture", clock.now())


def run(output_dir: Path) -> dict[str, object]:
	output_dir.mkdir(parents=True, exist_ok=True)
	runs = RunDirectoryService(output_dir / "runs", project=Path.cwd())
	calendar = ExchangeCalendar("XNYS", date(2024, 1, 2), date(2024, 6, 28))
	sessions = _sessions(calendar)
	training_sessions = sessions[70:73]
	evaluation_sessions = sessions[77:87]
	training_start, training_end = training_sessions[0].session_date, training_sessions[-1].session_date
	evaluation_start, evaluation_end = evaluation_sessions[0].session_date, evaluation_sessions[-1].session_date
	source_identity = SourceIdentity("fixture", "daily-closes", "us-stocks", "stage12-deterministic", PriceAdjustment.ADJUSTED)
	source = SourceConfig(source_identity, output_dir / "fixture-data", output_dir / "operator-supplied-disposable-dbinfo.id")
	coverage = _coverage(source_identity, calendar, sessions)
	clock = FrozenClock(datetime(2024, 5, 7, 15, tzinfo=timezone.utc))
	closes = FixtureCloses(calendar, sessions)
	channels = FixtureChannels()
	gap_session = sessions[65]

	raw_job = _job(source, calendar, coverage, training_start, training_end, evaluation_start, evaluation_end, target=TargetSpecV2(kind=TargetKind.RAW_RETURN), preprocessing=PreprocessingSpec(NormalizationMethod.STANDARD, NormalizationMethod.MIN_MAX), objective=LossObjective.RMSE, channels=("relative_close", "price_zscore_20", "price_zscore_60", "price_delta_zscore_20", "price_delta_zscore_60", "raw_volume", "volume_zscore_20", "volume_zscore_60"), runs_root=output_dir / "runs")
	raw_preview = resolve_configured_training(raw_job, clock=clock)
	raw_request = UniverseSelectionRequest.from_dependency_plan(raw_preview.plan, raw_preview.calendar, filters=raw_job.filters)
	universe_data = _universe_data(raw_request, gap_session)
	preparation = FixturePreparation()
	prepare_configured_training(raw_job, preparation, clock=clock)
	raw_result = execute_configured_training(raw_job, output_dir / "raw-requested.sgmodel", coverage=coverage, universe_data=universe_data, boundary=_boundary(source_identity, calendar, raw_preview.plan, clock), repository=closes, channel_repository=channels, clock=clock, runs=runs, checkpoint=True)
	reloaded = load_artifact(raw_result.training.artifact)

	rank_job = _job(source, calendar, coverage, training_start, training_end, evaluation_start, evaluation_end, target=TargetSpecV2(kind=TargetKind.RETURN_ZSCORE_CROSS_SECTIONAL), preprocessing=PreprocessingSpec(), objective=LossObjective.PAIRWISE_RANKING, channels=("relative_close", "raw_volume"), runs_root=output_dir / "runs")
	rank_preview = resolve_configured_training(rank_job, clock=clock)
	rank_request = UniverseSelectionRequest.from_dependency_plan(rank_preview.plan, rank_preview.calendar, filters=rank_job.filters)
	rank_result = execute_configured_training(rank_job, output_dir / "ranking-requested.sgmodel", coverage=coverage, universe_data=_universe_data(rank_request, gap_session), boundary=_boundary(source_identity, calendar, rank_preview.plan, clock), repository=closes, channel_repository=channels, clock=clock, runs=runs)

	liquidity = FixtureLiquidity(calendar)
	readiness = FixtureReadiness()
	provider = FixtureProvider()
	features_v2 = raw_job.feature_spec(raw_result.universe.ordered_ticker_ids)
	recommendation_entry = sessions[88]
	job = RecommendationJob(
		source, raw_preview.plan.warmup_sessions[0].session_date, calendar.shift(recommendation_entry, -1).session_date, recommendation_entry.session_date, features_v2, raw_job.target,
		ModelSelection(ModelRefreshPolicy.CURRENT, raw_result.training.artifact.artifact_id), AllocationSpec(AllocationPolicy.ALTERNATE_SESSIONS, evaluation_sessions[0]), raw_job.publication,
		CorrectionPolicy(timedelta(days=30), timedelta(days=30), timedelta(days=365), "Stage 12 fixture corrections"),
		EligibilityPolicy(200), 5, raw_result.training.artifact, refresh_mode=RefreshMode.VALIDATE_ONLY,
	)
	services = RecommendationServices(cast(MarketDataProvider, provider), None, None, closes, liquidity, simplified_readiness=cast(Any, readiness), channels=channels)
	recommendation = recommend(job, _boundary(source_identity, calendar, raw_preview.plan, clock), calendar, services, FrozenClock(datetime(2024, 5, 8, 15, tzinfo=timezone.utc)), runs=runs)

	v1_features = FeatureSpec(SELECTED_TICKERS, 3)
	train_decisions = tuple(_timing(calendar, session) for session in training_sessions)
	signals = HistoricalSignals(_boundary(source_identity, calendar, raw_preview.plan, clock), calendar, raw_job.publication, closes, liquidity)
	request = SimulationRequest(source_identity, v1_features, TargetSpec(), AllocationSpec(AllocationPolicy.ALTERNATE_SESSIONS, evaluation_sessions[0]), evaluation_start, evaluation_end, Decimal("1000.00"), eligibility=EligibilityPolicy(200), top_k=1)
	prices = RepositoryExecutionPrices(_boundary(source_identity, calendar, raw_preview.plan, clock), closes, evaluation_sessions[-1].closes_at + timedelta(minutes=16))
	scores = TrainThenSimulateScores(PreEvaluationTraining(train_decisions, raw_job.configuration, output_dir / "simulation-preperiod"), signals)
	paired = persist_paired_simulation(runs, request, _boundary(source_identity, calendar, raw_preview.plan, clock), calendar, scores, prices)
	parent_summary = []
	for item in runs.list_runs(include_invalid=False):
		manifest = item.manifest
		if manifest is not None and manifest.operation is RunOperation.SIMULATION and manifest.results.get("paired_children"):
			parent_summary.append(item)
	if len(parent_summary) != 1 or parent_summary[0].manifest is None:
		raise AssertionError("Stage 12 paired simulation parent was not persisted")
	parent_id = parent_summary[0].run_id
	children = parent_summary[0].manifest.results["paired_children"]
	if not isinstance(children, list) or any(not isinstance(item, str) for item in children):
		raise AssertionError("Stage 12 paired simulation children are not recorded")
	child_ids = tuple(children)
	alternate_id = child_ids[0]
	replay = replay_saved_simulation(runs, alternate_id, request, _boundary(source_identity, calendar, raw_preview.plan, clock), calendar)
	if tuple(item.total_equity for item in replay.valuations) != tuple(item.total_equity for item in paired.alternate_sessions.valuations):
		raise AssertionError("Offline replay changed the original marked equity")
	if not paired.alternate_sessions.training_runs:
		raise AssertionError("Train-then-simulate did not retain its pre-period artifact")
	simulation_artifact = paired.alternate_sessions.training_runs[0][1].artifact
	rerun = rerun_pinned_simulation(runs, alternate_id, request, _boundary(source_identity, calendar, raw_preview.plan, clock), calendar, FixedModelScores(simulation_artifact, signals), prices, data_revision=SOURCE_REVISION)

	selection = raw_result.universe
	return {
		"schema": "stockguidance-stage12-acceptance-1",
		"fixture": {"calendar": calendar.calendar_id, "total_sessions": len(sessions), "warmup_sessions": len(raw_preview.plan.warmup_sessions), "training_sessions": [item.session_date.isoformat() for item in training_sessions], "simulation_sessions": len(evaluation_sessions), "interior_gap": gap_session.session_date.isoformat(), "selected_ticker_ids": list(selection.ordered_ticker_ids), "excluded": [item.to_mapping() for item in selection.exclusions]},
		"preparation": {"validated": preparation.validated, "prepared": preparation.prepared},
		"training": {"raw_run_id": raw_result.run_id, "raw_artifact_reloaded": reloaded.metadata.semantics.features == raw_job.feature_spec(raw_result.universe.ordered_ticker_ids), "raw_target": raw_job.target.version, "raw_preprocessing": raw_job.preprocessing.to_mapping(), "ranking_run_id": rank_result.run_id, "ranking_target": rank_job.target.version, "ranking_objective": rank_job.configuration.loss.objective.value},
		"recommendation": {"run_count": len(runs.list_runs(include_invalid=False)), "validate_only_calls": readiness.calls, "provider_calls": len(provider.calls), "eligible_count": recommendation.recommendation.eligible_count, "symbols": [item.symbol for item in recommendation.recommendation.ranking]},
		"simulation": {"parent_run_id": parent_id, "child_run_ids": list(child_ids), "alternate_successful": paired.alternate_sessions.successful, "staggered_successful": paired.staggered_halves.successful, "alternate_decisions": len(paired.alternate_sessions.forecasts), "staggered_decisions": len(paired.staggered_halves.forecasts), "replay_matches": True, "rerun_successful": rerun.successful},
	}


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Run the offline Stage 12 training/recommendation/simulation acceptance fixture")
	parser.add_argument("--output-dir", type=Path, required=True, help="Disposable output directory for artifacts, runs, and the summary")
	arguments = parser.parse_args(argv)
	try:
		result = run(arguments.output_dir)
		(arguments.output_dir / "stage12-summary.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
		print(json.dumps(result, sort_keys=True))
		return 0
	except Exception:
		print(json.dumps({"successful": False, "failure": "Stage 12 fixture workflow failed"}))
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
