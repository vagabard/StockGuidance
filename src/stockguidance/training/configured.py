"""Configured training orchestration for the Stage 8 contract.

The resolver and runner are dependency-injected.  They never invent source
coverage or ticker identities: callers provide the verified coverage snapshot
and either universe rows or a typed universe repository.  Database readiness
is explicit when a connection is supplied; the pure fixture path accepts an
already evidenced close repository and boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from stockguidance.artifacts.contracts import ArtifactUniverse
from stockguidance.artifacts.loading import load_artifact
from stockguidance.calendar.clock import Clock, SystemClock
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.calendar.ranges import (
	DatePlanSpec, DateRangeResolver, DependencyPlan, DependencySpec, SourceCoverageSnapshot,
	coverage_snapshot_from_mapping, parse_date_range,
)
from stockguidance.config.source import SourceConfig
from stockguidance.config.training import decode_preprocessing, decode_target, decode_training_configuration
from stockguidance.market_data.identity import decode_source, mapping, text_field
from stockguidance.datasets.training import build_training_dataset
from stockguidance.features.channels import FeatureChannelRepository
from stockguidance.features.specification import (
	FeatureChannelValue, FeatureSpecV2, MissingHistoryPolicy, SignalMode,
)
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.preprocessing import PreprocessingSpec
from stockguidance.reporting.storage import write_report
from stockguidance.reporting.training import training_payload
from stockguidance.runs.contracts import RunConfig, RunOperation
from stockguidance.runs.service import RunDirectoryService, RunHandle
from stockguidance.targets.specification import TargetSpec, TargetSpecification
from stockguidance.training.configuration import TrainingConfig
from stockguidance.training.result import TrainingResult
from stockguidance.universe.selection import (
	AverageVolumeFilter, LegacyTransactionFilter, UniverseDataRepository, UniverseFilters, UniverseSelectionRequest,
	VolatilityFilter,
	UniverseSelectionResult, UniverseSourceData, select_universe,
)
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history

if TYPE_CHECKING:
	from stockguidance.database.raw_rebuild import RawPayloadProvider
	from stockguidance.database.repositories.closes import CloseRepository
	from stockguidance.database.write_connections import WriteConnectionFactory


@dataclass(frozen=True)
class ConfiguredTrainingJob:
	"""Sanitized v2 settings before source-dependent resolution."""

	source: SourceConfig
	date_plan: DatePlanSpec
	lookback_sessions: int
	channels: tuple[FeatureChannelValue | str, ...]
	configuration: TrainingConfig
	publication: DailyPublicationPolicy
	decision_lead: timedelta
	target: TargetSpecification = TargetSpec()
	preprocessing: PreprocessingSpec = PreprocessingSpec()
	filters: UniverseFilters = UniverseFilters()
	ordered_ticker_ids: tuple[int, ...] | None = None
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT
	data_snapshot: datetime | None = None
	calendar_start: date | None = None
	calendar_end: date | None = None
	coverage: SourceCoverageSnapshot | None = None
	runs_root: Path | None = None

	def __post_init__(self) -> None:
		if not isinstance(self.source, SourceConfig) or not isinstance(self.date_plan, DatePlanSpec):
			raise ValueError("Configured training requires a typed source and date plan")
		if type(self.lookback_sessions) is not int or self.lookback_sessions <= 0 or not self.channels:
			raise ValueError("Configured training requires a positive lookback and channels")
		if not isinstance(self.configuration, TrainingConfig) or not isinstance(self.target, TargetSpec) or not isinstance(self.preprocessing, PreprocessingSpec):
			raise ValueError("Configured training requires typed target, preprocessing and model settings")
		if not isinstance(self.filters, UniverseFilters) or self.ordered_ticker_ids is not None and (not self.ordered_ticker_ids or len(set(self.ordered_ticker_ids)) != len(self.ordered_ticker_ids)):
			raise ValueError("Configured universe settings are invalid")
		if self.data_snapshot is not None and (self.data_snapshot.tzinfo is None or self.data_snapshot.utcoffset() is None):
			raise ValueError("Configured data snapshot must be timezone aware")
		if self.availability_policy.retrospective and self.data_snapshot is None:
			raise ValueError("Retrospective configured training requires data_snapshot")

	@property
	def contract_version(self) -> str:
		return "training-job-v2"

	def feature_spec(self, ticker_ids: tuple[int, ...]) -> FeatureSpecV2:
		return FeatureSpecV2(ticker_ids, self.lookback_sessions, self.channels, SignalMode.PREVIOUS_CLOSE, self.source.identity.interval, MissingHistoryPolicy.UNAVAILABLE, self.source.identity.adjustment)

	def to_mapping(self) -> dict[str, object]:
		return {
			"contract_version": self.contract_version,
			"source": {
				"provider": self.source.identity.provider, "endpoint": self.source.identity.endpoint,
				"market": self.source.identity.market, "universe": self.source.identity.universe,
				"adjustment": self.source.identity.adjustment.value, "interval": self.source.identity.interval.value,
			},
			"data_root": str(self.source.data_root), "database_config": str(self.source.database_config),
			"date_range": self.date_plan.training.to_mapping(),
			"evaluation_range": None if self.date_plan.evaluation is None else self.date_plan.evaluation.to_mapping(),
			"dependencies": self.date_plan.dependencies.to_mapping(),
			"features": {"lookback_sessions": self.lookback_sessions, "channels": [str(value) for value in self.channels], "signal_mode": SignalMode.PREVIOUS_CLOSE.value},
			"target": self.target.to_mapping(), "preprocessing": self.preprocessing.to_mapping(),
			"model": self.configuration.model.to_mapping(), "loss": self.configuration.loss.to_mapping(),
			"membership": {"filters": self.filters.to_mapping(), "ordered_ticker_ids": self.ordered_ticker_ids},
			"publication": {"delay_minutes": None if self.publication.after_close is None else self.publication.after_close.total_seconds() / 60, "evidence": self.publication.provenance},
			"decision_minutes_before_close": self.decision_lead.total_seconds() / 60,
			"availability_policy": self.availability_policy.value,
			"data_snapshot": None if self.data_snapshot is None else self.data_snapshot.isoformat(),
			"calendar": {"name": self.source.calendar_id, "start": None if self.calendar_start is None else self.calendar_start.isoformat(), "end": None if self.calendar_end is None else self.calendar_end.isoformat()},
			"coverage": None if self.coverage is None else self.coverage.to_mapping(),
			"runs_root": None if self.runs_root is None else str(self.runs_root),
		}


def _filter(value: object, name: str) -> tuple[int, Decimal | None, Decimal | None]:
	fields = mapping(value)
	if set(fields) != {"window_sessions", "lower", "upper"}:
		raise ValueError(f"{name} filter fields are incomplete")
	window = fields["window_sessions"]
	if type(window) is not int:
		raise ValueError(f"{name} filter window must be an integer")
	return window, None if fields["lower"] is None else Decimal(str(fields["lower"])), None if fields["upper"] is None else Decimal(str(fields["upper"]))


def _filters(value: object) -> UniverseFilters:
	fields = mapping(value)
	if set(fields) - {"average_volume", "volatility", "legacy_transactions"}:
		raise ValueError("Unknown universe filter fields")
	average = None if fields.get("average_volume") is None else AverageVolumeFilter(*_filter(fields["average_volume"], "average_volume"))
	volatility = None if fields.get("volatility") is None else VolatilityFilter(*_filter(fields["volatility"], "volatility"))
	legacy_value = fields.get("legacy_transactions")
	minimum = 200 if legacy_value is None else mapping(legacy_value).get("minimum", 200)
	if type(minimum) is not int:
		raise ValueError("legacy transaction minimum must be an integer")
	legacy = None if legacy_value is None else LegacyTransactionFilter(minimum)
	return UniverseFilters(average, volatility, legacy)


def _number(value: object, name: str) -> float:
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		raise ValueError(f"{name} must be numeric")
	return float(value)


def load_configured_job(path: Path) -> ConfiguredTrainingJob:
	"""Decode a non-secret v2 job without resolving dates or touching a database."""
	fields = mapping(json.loads(path.read_text(encoding="utf-8")))
	allowed = {"contract_version", "source", "data_root", "database_config", "date_range", "evaluation_range", "dependencies", "membership", "features", "target", "preprocessing", "model", "loss", "training", "publication", "publication_delay_minutes", "publication_evidence", "decision_minutes_before_close", "availability_policy", "data_snapshot", "calendar", "coverage", "runs_root"}
	if fields.get("contract_version") != "training-job-v2" or set(fields) - allowed:
		raise ValueError("Incomplete or unsupported configured training job fields")
	identity = decode_source(mapping(fields["source"]))
	source = SourceConfig(identity, Path(text_field(fields, "data_root")), Path(text_field(fields, "database_config")))
	feature_fields = mapping(fields["features"])
	if set(feature_fields) - {"lookback_sessions", "channels", "signal_mode"} or "lookback_sessions" not in feature_fields or "channels" not in feature_fields:
		raise ValueError("Configured features require lookback_sessions and channels")
	lookback = feature_fields["lookback_sessions"]
	channels = feature_fields["channels"]
	if type(lookback) is not int or lookback <= 0 or not isinstance(channels, list) or not channels or any(not isinstance(item, str) for item in channels):
		raise ValueError("Configured features have invalid channels or lookback")
	if feature_fields.get("signal_mode", SignalMode.PREVIOUS_CLOSE.value) != SignalMode.PREVIOUS_CLOSE.value:
		raise ValueError("Stage 8 configured training currently supports previous_close only")
	target = decode_target(fields.get("target"))
	preprocessing = decode_preprocessing(fields.get("preprocessing"))
	training_fields = mapping(fields["training"])
	if "loss" in fields:
		if "loss" in training_fields:
			raise ValueError("Loss must be specified either at job level or in training")
		training_fields["loss"] = fields["loss"]
	configuration = decode_training_configuration(training_fields, fields["model"])
	range_value = parse_date_range(fields["date_range"])
	evaluation = None if fields.get("evaluation_range") is None else parse_date_range(fields["evaluation_range"])
	dependency_fields = mapping(fields.get("dependencies", {}))
	windows: list[int] = []
	for channel in channels:
		if channel.startswith(("price_zscore_", "price_delta_zscore_", "volume_zscore_")):
			try:
				windows.append(int(channel.rsplit("_", 1)[1]))
			except ValueError:
				raise ValueError("Configured z-score channels require numeric windows") from None
	training_gap = dependency_fields.get("training_end_gap_sessions", 0)
	simulation_horizon = dependency_fields.get("simulation_horizon_sessions", 10)
	if type(training_gap) is not int or type(simulation_horizon) is not int:
		raise ValueError("Configured dependency session counts must be integers")
	dependencies = DependencySpec(
		model_lookback_sessions=lookback,
		training_end_gap_sessions=training_gap,
		zscore_window_sessions=max(windows) if windows else None,
		price_delta_predecessor_sessions=1 if any(channel.startswith("price_delta_zscore_") for channel in channels) else 0,
		target_baseline_sessions=target.rolling_window_sessions or 0,
		label_horizon_sessions=1,
		simulation_horizon_sessions=simulation_horizon,
	)
	plan = DatePlanSpec(range_value, evaluation, dependencies)
	membership = mapping(fields.get("membership", {}))
	filters = _filters(membership.get("filters", {}))
	ordered = membership.get("ordered_ticker_ids")
	if ordered is not None:
		if not isinstance(ordered, list) or any(type(item) is not int for item in ordered):
			raise ValueError("ordered_ticker_ids must be an integer array")
		ordered_ids = tuple(ordered)
	else:
		ordered_ids = None
	publication_fields = mapping(fields.get("publication", {}))
	publication = DailyPublicationPolicy(timedelta(minutes=_number(publication_fields.get("delay_minutes", fields.get("publication_delay_minutes", 15)), "publication delay")), str(publication_fields.get("evidence", fields.get("publication_evidence", "Configured source publication policy"))))
	lead = timedelta(minutes=_number(fields.get("decision_minutes_before_close", 30), "decision lead"))
	if not timedelta(0) < lead < timedelta(days=1):
		raise ValueError("Configured decision lead must be between zero and one day")
	calendar_fields = mapping(fields.get("calendar", {}))
	calendar_start = None if calendar_fields.get("start") is None else date.fromisoformat(str(calendar_fields["start"]))
	calendar_end = None if calendar_fields.get("end") is None else date.fromisoformat(str(calendar_fields["end"]))
	coverage = None if fields.get("coverage") is None else coverage_snapshot_from_mapping(identity, str(calendar_fields.get("name", source.calendar_id)), mapping(fields["coverage"]))
	snapshot = None if fields.get("data_snapshot") is None else datetime.fromisoformat(text_field(fields, "data_snapshot"))
	return ConfiguredTrainingJob(source, DatePlanSpec(range_value, evaluation, dependencies), lookback, tuple(channels), configuration, publication, lead, target, preprocessing, filters, ordered_ids, AvailabilityPolicy(str(fields.get("availability_policy", AvailabilityPolicy.STRICT.value))), snapshot, calendar_start, calendar_end, coverage, None if fields.get("runs_root") is None else Path(text_field(fields, "runs_root")))


class PreparationService(Protocol):
	def validate(self, job: ConfiguredTrainingJob, plan: DependencyPlan) -> object: ...
	def prepare(self, job: ConfiguredTrainingJob, plan: DependencyPlan) -> object: ...


@dataclass(frozen=True)
class ConfiguredTrainingPreview:
	job: ConfiguredTrainingJob
	plan: DependencyPlan
	calendar: ExchangeCalendar

	def to_mapping(self) -> dict[str, object]:
		return {"schema": "stockguidance-training-preview-1", "contract_version": self.job.contract_version, "plan": self.plan.to_mapping()}


@dataclass(frozen=True)
class ConfiguredTrainingResult:
	training: TrainingResult
	plan: DependencyPlan
	universe: UniverseSelectionResult
	run_id: str | None


def _calendar(job: ConfiguredTrainingJob, coverage: SourceCoverageSnapshot) -> ExchangeCalendar:
	start = job.calendar_start or coverage.first_verified
	end = job.calendar_end or coverage.last_verified
	if start is None or end is None:
		raise ValueError("Configured training requires verified source coverage")
	return ExchangeCalendar(job.source.calendar_id, start - timedelta(days=30), end + timedelta(days=30))


def resolve_configured_training(job: ConfiguredTrainingJob, coverage: SourceCoverageSnapshot | None = None, *, clock: Clock | None = None) -> ConfiguredTrainingPreview:
	if not isinstance(job, ConfiguredTrainingJob):
		raise ValueError("Configured training resolution requires a typed job")
	snapshot = coverage or job.coverage
	if snapshot is None:
		raise ValueError("Configured training requires an explicit source coverage snapshot")
	calendar = _calendar(job, snapshot)
	plan = DateRangeResolver(calendar, snapshot, job.source.identity, clock or SystemClock()).resolve_plan(job.date_plan)
	return ConfiguredTrainingPreview(job, plan, calendar)


def validate_configured_preparation(
	job: ConfiguredTrainingJob, preparation: PreparationService,
	coverage: SourceCoverageSnapshot | None = None, *, clock: Clock | None = None,
) -> ConfiguredTrainingPreview:
	"""Resolve and validate source preparation without fitting or exporting."""
	preview = resolve_configured_training(job, coverage, clock=clock)
	preparation.validate(job, preview.plan)
	return preview


def prepare_configured_training(
	job: ConfiguredTrainingJob, preparation: PreparationService,
	coverage: SourceCoverageSnapshot | None = None, *, clock: Clock | None = None,
) -> ConfiguredTrainingPreview:
	"""Run the injected bounded preparation after independent validation."""
	preview = validate_configured_preparation(job, preparation, coverage, clock=clock)
	preparation.prepare(job, preview.plan)
	return preview


def _decisions(calendar: ExchangeCalendar, sessions: tuple[TradingSession, ...], lead: timedelta) -> tuple[DecisionTiming, ...]:
	return tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - lead) for session in sessions)


def _cutoff(session: TradingSession, publication: DailyPublicationPolicy) -> datetime:
	expected = publication.expected_at(session)
	return max(session.closes_at, expected or session.closes_at)


def _boundary(job: ConfiguredTrainingJob, plan: DependencyPlan, clock: Clock) -> HistoryBoundary:
	first = plan.warmup_sessions[0] if plan.warmup_sessions else plan.training.entry_sessions[0]
	return HistoryBoundary(job.source.identity, plan.training.calendar_id, first.session_date, HistoryProvenance.EXPLICIT_BOUNDARY, "Stage 8 verified source coverage snapshot", clock.now())


def _universe_data(repository: UniverseDataRepository | None, request: UniverseSelectionRequest, data: UniverseSourceData | None) -> UniverseSourceData:
	if data is not None:
		return data
	if repository is None:
		raise ValueError("Configured training requires universe source data or a typed universe repository")
	return repository.read(request.read_request())


def _prediction_rows(training: TrainingResult, repository: CloseRepository, job: ConfiguredTrainingJob, feature_spec: FeatureSpecV2, target: TargetSpecification, boundary: HistoryBoundary, calendar: ExchangeCalendar, plan: DependencyPlan, channel_repository: FeatureChannelRepository | None = None) -> dict[str, object]:
	from stockguidance.inference.prediction import predict_sample

	loaded = load_artifact(training.artifact)
	publication = job.publication
	evaluation_decisions = _decisions(calendar, plan.evaluation.entry_sessions, job.decision_lead)
	label_exits = plan.evaluation_label_exit_sessions
	evaluation_cutoff = max(
		_cutoff(label_exits[-1] if label_exits else plan.evaluation.start_session, publication),
		evaluation_decisions[-1].decision_cutoff,
	)
	dataset = build_training_dataset(evaluation_decisions, evaluation_cutoff, feature_spec, target, boundary, calendar, publication, repository, job.availability_policy, job.data_snapshot, channel_repository)
	rows: list[dict[str, object]] = []
	for sample in dataset.samples:
		if sample.features.values is None:
			continue
		rows.append({"entry_session": sample.timing.entry_session.session_date.isoformat(), "ticker_ids": list(feature_spec.ticker_ids), "scores": list(predict_sample(loaded, sample)), "score_units": "fractional_return" if target.version.startswith("raw_return") else "target_score", "eligible": sample.training_eligible})
	return {"schema": "training-predictions-1", "scope": "evaluation_entries", "rows": rows}


def execute_configured_training(
	job: ConfiguredTrainingJob,
	artifact_path: Path | None = None,
	*,
	coverage: SourceCoverageSnapshot | None = None,
	universe_data: UniverseSourceData | None = None,
	universe_repository: UniverseDataRepository | None = None,
	preparation: PreparationService | None = None,
	channel_repository: FeatureChannelRepository | None = None,
	boundary: HistoryBoundary | None = None,
	repository: CloseRepository | None = None,
	connect: WriteConnectionFactory | None = None,
	provider: RawPayloadProvider | None = None,
	clock: Clock | None = None,
	runs: RunDirectoryService | None = None,
	checkpoint: bool = False,
	resume_from: Path | None = None,
) -> ConfiguredTrainingResult:
	"""Resolve, select, explicitly validate/prepare, fit, export and record."""
	when = clock or SystemClock()
	run_service = runs or RunDirectoryService(job.runs_root)
	run_handle: RunHandle | None = run_service.create(RunConfig(RunOperation.TRAINING, job.to_mapping()))
	if run_handle is not None:
		run_service.start(run_handle.run_id)
	failure_stage = "resolution"
	try:
		preview = resolve_configured_training(job, coverage, clock=when)
		active_coverage = coverage or job.coverage
		assert active_coverage is not None
		if run_handle is not None:
			run_service.update_sections(run_handle.run_id, resolved_configuration=preview.plan.to_mapping(), sections={"resolution": preview.plan.to_mapping(), "identity": {"source": job.source.identity, "source_revision": active_coverage.revision, "calendar": {"id": preview.calendar.calendar_id, "version": preview.calendar.version}}, "provenance": {"data_revision": active_coverage.revision}})
		failure_stage = "preparation"
		if preparation is not None:
			preparation.validate(job, preview.plan)
			preparation.prepare(job, preview.plan)
		failure_stage = "universe"
		training_request = UniverseSelectionRequest.from_dependency_plan(preview.plan, preview.calendar, filters=job.filters, ordered_ticker_ids=job.ordered_ticker_ids, as_of=job.data_snapshot)
		active_universe_repository = universe_repository
		if active_universe_repository is None and connect is not None:
			from stockguidance.database.repositories.universe import MySQLUniverseRepository

			active_universe_repository = MySQLUniverseRepository(job.source, connect)
		selected = select_universe(training_request, _universe_data(active_universe_repository, training_request, universe_data))
		feature_spec = job.feature_spec(selected.ordered_ticker_ids)
		if run_handle is not None:
			run_service.update_sections(run_handle.run_id, sections={"universe": selected.to_mapping(), "specifications": {"features": feature_spec.to_mapping(), "target": job.target.to_mapping(), "preprocessing": job.preprocessing.to_mapping(), "model": job.configuration.model.to_mapping(), "loss": job.configuration.loss.to_mapping(), "universe": ArtifactUniverse(selected.ordered_ticker_ids, selected.ordered_ticker_labels, selected.request.selection_mode, selected.source_revision).to_mapping()}, "results": {"exclusions": [item.to_mapping() for item in selected.exclusions]}})
		training_decisions = _decisions(preview.calendar, preview.plan.training.entry_sessions, job.decision_lead)
		evaluation_decisions = _decisions(preview.calendar, preview.plan.evaluation.entry_sessions, job.decision_lead)
		training_cutoff = _cutoff(preview.plan.training_information_cutoff, job.publication)
		evaluation_labels = preview.plan.evaluation_label_exit_sessions
		evaluation_cutoff = max(_cutoff(evaluation_labels[-1] if evaluation_labels else preview.plan.evaluation.start_session, job.publication), evaluation_decisions[-1].decision_cutoff)
		active_boundary = boundary or _boundary(job, preview.plan, when)
		active_repository = repository
		if active_repository is None:
			if connect is None:
				raise ValueError("Configured training requires a close repository or a database connection")
			from stockguidance.workflows.simplified_readiness import SimplifiedCloseRepository, SimplifiedReadinessWorkflow

			readiness = SimplifiedReadinessWorkflow(job.source, connect, preview.calendar, job.publication, provider, when)
			readiness.ensure(tuple((*training_decisions, *evaluation_decisions[:-1])), feature_spec, job.target, label_cutoff=evaluation_cutoff, availability_policy=job.availability_policy, data_snapshot=job.data_snapshot)
			active_repository = SimplifiedCloseRepository(job.source, connect)
		assert active_repository is not None
		output = artifact_path
		checkpoint_path = None
		if run_handle is not None:
			output = run_handle.artifacts_directory / "model.sgmodel"
			checkpoint_path = run_handle.checkpoints_directory / "training.pt" if checkpoint else None
		if output is None:
			raise ValueError("Configured training requires an artifact path")
		failure_stage = "training"
		universe = ArtifactUniverse(selected.ordered_ticker_ids, selected.ordered_ticker_labels, selected.request.selection_mode, selected.source_revision)
		request = HistoryTrainingRequest(training_decisions, training_cutoff, feature_spec, job.target, job.configuration, output, evaluation_decisions, evaluation_cutoff, checkpoint_path, resume_from, job.availability_policy, job.data_snapshot, job.preprocessing, channel_repository, universe)
		training = train_from_history(request, active_boundary, preview.calendar, job.publication, active_repository)
		failure_stage = "export"
		if artifact_path is not None and artifact_path.resolve() != output.resolve():
			atomic_write(artifact_path, output.read_bytes())
		if run_handle is not None:
			run_service.register_file(run_handle.run_id, name="model", relative_path="artifacts/model.sgmodel")
			if checkpoint_path is not None:
				run_service.register_file(run_handle.run_id, name="checkpoint", relative_path="checkpoints/training.pt", kind="checkpoint")
			write_report(run_handle.reports_directory / "training.json", training_payload(training))
			loaded_predictions = _prediction_rows(training, active_repository, job, feature_spec, job.target, active_boundary, preview.calendar, preview.plan, channel_repository)
			run_service.update_sections(run_handle.run_id, sections={"results": {"predictions": loaded_predictions, "metrics": {"training": training.training_mae, "evaluation": training.evaluation_mae, "objective": training.training_objective_loss, "objective_units": training.objective_loss_units, "target_units": training.target_metric_units, "excluded_training": [item.__dict__ for item in training.excluded_training], "excluded_evaluation": [item.__dict__ for item in training.excluded_evaluation]}, "artifacts": {"artifact_id": training.artifact.artifact_id}, "reports": {"path": "reports/training.json", "schema": "stockguidance-training-1"}}})
			run_service.complete(run_handle.run_id)
		return ConfiguredTrainingResult(training, preview.plan, selected, None if run_handle is None else run_handle.run_id)
	except Exception as error:
		if run_handle is not None:
			run_service.fail(run_handle.run_id, stage=failure_stage, code=type(error).__name__, message=str(error))
		raise


__all__ = ["ConfiguredTrainingJob", "ConfiguredTrainingPreview", "ConfiguredTrainingResult", "PreparationService", "execute_configured_training", "prepare_configured_training", "resolve_configured_training", "validate_configured_preparation"]
