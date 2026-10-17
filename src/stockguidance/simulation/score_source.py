from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from stockguidance.artifacts.contracts import ArtifactReference, ModelSemantics
from stockguidance.artifacts.ensembles import LoadedEnsemble
from stockguidance.artifacts.loading import LoadedModel
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.closes import CloseQuery, CloseRepository
from stockguidance.database.repositories.eligibility import EligibilityRepository
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.datasets.building import build_sample
from stockguidance.datasets.sample import Sample
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.recommendations.artifacts import load_recommendation_artifact
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.recommendations.eligibility import EligibilityState, assess_eligibility
from stockguidance.recommendations.scoring import RankedPrediction, predict_ranking
from stockguidance.simulation.configuration import EvaluationPolicy, ScorePolicy, SimulationRequest
from stockguidance.training.configuration import TrainingConfig
from stockguidance.training.result import TrainingResult
from stockguidance.workflows.recommendation_dependencies import HistoryTrainer
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


class DecisionUnavailable(ValueError):
	pass


@dataclass(frozen=True)
class DecisionForecast:
	artifact_id: str
	sample: Sample
	prediction: RankedPrediction
	training: TrainingResult | None = None


class ScoreSource(Protocol):
	@property
	def policy(self) -> ScorePolicy: ...
	@property
	def training_runs(self) -> tuple[tuple[DecisionTiming, TrainingResult], ...]: ...
	def validate(self, request: SimulationRequest, first: DecisionTiming, calendar: SessionCalendar) -> None: ...
	def forecast(self, request: SimulationRequest, timing: DecisionTiming, buy_index: int) -> DecisionForecast: ...


def validate_model(artifact: LoadedModel | LoadedEnsemble, request: SimulationRequest, timing: DecisionTiming, calendar: SessionCalendar, *, fixed: bool = False) -> None:
	semantics = artifact.metadata.semantics if isinstance(artifact, LoadedModel) else artifact.semantics
	if semantics != ModelSemantics(request.source, request.features, request.target, calendar.calendar_id, calendar.version, availability_policy=request.availability_policy):
		raise ValueError("Simulation artifact source/feature/target/ticker/calendar specifications differ")
	members = (artifact,) if isinstance(artifact, LoadedModel) else artifact.members
	if any(member.metadata.training.data_snapshot != request.data_snapshot for member in members) and request.availability_policy.retrospective:
		raise ValueError("Research artifacts and simulation must use the same data snapshot")
	if any(member.metadata.training.information_cutoff > timing.decision_cutoff for member in members):
		raise ValueError("Simulation model contains information after the decision cutoff")
	if fixed and any(member.metadata.training.information_cutoff >= timing.entry_session.opens_at or member.metadata.training.last_exit >= timing.entry_session.session_date for member in members):
		raise ValueError("Fixed models must be trained before the evaluation period")


class HistoricalSignals:
	def __init__(self, boundary: HistoryBoundary, calendar: SessionCalendar, publication: DailyPublicationPolicy, closes: CloseRepository, eligibility: EligibilityRepository, availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT, data_snapshot: datetime | None = None) -> None:
		if not isinstance(availability_policy, AvailabilityPolicy):
			raise ValueError("Historical signals require a typed availability policy")
		self.boundary, self.calendar, self.publication = boundary, calendar, publication
		self.availability_policy, self.data_snapshot = availability_policy, data_snapshot
		self.closes, self.eligibility = closes, eligibility

	def forecast(self, artifact: LoadedModel | LoadedEnsemble, request: SimulationRequest, timing: DecisionTiming, training: TrainingResult | None = None) -> DecisionForecast:
		validate_model(artifact, request, timing, self.calendar)
		if self.availability_policy is not request.availability_policy or self.data_snapshot != request.data_snapshot:
			raise ValueError("Historical signal policy and snapshot differ from the simulation request")
		sample = build_sample(request.features, request.target, timing, self.boundary, self.calendar, self.publication, self.closes, availability_policy=self.availability_policy, data_snapshot=self.data_snapshot)
		if sample.features.values is None:
			raise DecisionUnavailable("Required close history or full-universe opening inputs are unavailable at the historical decision")
		query = CloseQuery(SessionQuery(self.boundary, (timing.previous_session,), self.availability_policy.query_as_of(timing.decision_cutoff, self.data_snapshot)), request.features.ticker_ids)
		eligibility = assess_eligibility(request.features.ticker_ids, request.eligibility, timing, self.eligibility.read(query), self.availability_policy, self.publication)
		if any(item.state is EligibilityState.UNAVAILABLE for item in eligibility):
			raise DecisionUnavailable("Required previous-session liquidity is unavailable at the historical decision")
		prediction = predict_ranking(artifact, sample, eligibility, request.top_k)
		return DecisionForecast(artifact.artifact_id, sample, prediction, training)


class FixedModelScores:
	policy = ScorePolicy(EvaluationPolicy.FIXED)
	training_runs: tuple[tuple[DecisionTiming, TrainingResult], ...] = ()

	def __init__(self, reference: ArtifactReference, signals: HistoricalSignals, kind: ArtifactKind = ArtifactKind.MODEL) -> None:
		self.artifact = load_recommendation_artifact(reference, kind)
		self.signals = signals

	def validate(self, request: SimulationRequest, first: DecisionTiming, calendar: SessionCalendar) -> None:
		if self.signals.boundary.source != request.source or self.signals.calendar is not calendar:
			raise ValueError("Historical signals must use the requested source and calendar")
		if self.signals.availability_policy is not request.availability_policy or self.signals.data_snapshot != request.data_snapshot:
			raise ValueError("Historical signal policy and snapshot differ from the simulation request")
		validate_model(self.artifact, request, first, calendar, fixed=True)

	def forecast(self, request: SimulationRequest, timing: DecisionTiming, buy_index: int) -> DecisionForecast:
		return self.signals.forecast(self.artifact, request, timing)


@dataclass(frozen=True)
class WalkForwardTraining:
	decisions: tuple[DecisionTiming, ...]
	configuration: TrainingConfig
	artifact_directory: Path
	every_buy_decisions: int = 1

	def __post_init__(self) -> None:
		ScorePolicy(EvaluationPolicy.CHRONOLOGICAL_RETRAIN, self.every_buy_decisions)
		if not isinstance(self.decisions, tuple) or not self.decisions or any(not isinstance(item, DecisionTiming) for item in self.decisions) or any(right.entry_session.sequence <= left.entry_session.sequence for left, right in zip(self.decisions, self.decisions[1:])):
			raise ValueError("Walk-forward training requires unique chronological historical decisions")
		if not isinstance(self.configuration, TrainingConfig) or not isinstance(self.artifact_directory, Path):
			raise ValueError("Walk-forward training requires typed settings and artifact destinations")


class ChronologicalModelScores:
	def __init__(self, plan: WalkForwardTraining, signals: HistoricalSignals, *, trainer: HistoryTrainer | None = None) -> None:
		self.plan, self.signals, self.trainer = plan, signals, trainer or train_from_history
		self.policy = ScorePolicy(EvaluationPolicy.CHRONOLOGICAL_RETRAIN, plan.every_buy_decisions)
		self.artifact: LoadedModel | LoadedEnsemble | None = None
		self._training_runs: list[tuple[DecisionTiming, TrainingResult]] = []

	@property
	def training_runs(self) -> tuple[tuple[DecisionTiming, TrainingResult], ...]:
		return tuple(self._training_runs)

	def validate(self, request: SimulationRequest, first: DecisionTiming, calendar: SessionCalendar) -> None:
		if self.artifact is not None or self.training_runs:
			raise ValueError("Use a fresh chronological score source for each evaluation")
		if self.signals.boundary.source != request.source or self.signals.calendar is not calendar:
			raise ValueError("Historical signals must use the requested source and calendar")
		if self.signals.availability_policy is not request.availability_policy or self.signals.data_snapshot != request.data_snapshot:
			raise ValueError("Historical signal policy and snapshot differ from the simulation request")
		for timing in self.plan.decisions:
			if timing.entry_session.session_date < self.signals.boundary.history_start or any(session != calendar.session(session.session_date) for session in (timing.previous_session, timing.entry_session, timing.exit_session)):
				raise ValueError("Training decisions cross supported history or disagree with the calendar")

	def forecast(self, request: SimulationRequest, timing: DecisionTiming, buy_index: int) -> DecisionForecast:
		training = None
		if self.artifact is None or buy_index % self.plan.every_buy_decisions == 0:
			self.artifact = None
			decisions = tuple(item for item in self.plan.decisions if item.entry_session.sequence < timing.entry_session.sequence and item.decision_cutoff <= timing.decision_cutoff)
			if not decisions:
				raise ValueError("No historical training decisions precede the evaluation decision")
			path = self.plan.artifact_directory / f"decision-{timing.entry_session.session_date.isoformat()}.sgmodel"
			training_request = HistoryTrainingRequest(decisions, timing.decision_cutoff, request.features, request.target, self.plan.configuration, path, availability_policy=request.availability_policy, data_snapshot=request.data_snapshot)
			training = self.trainer(training_request, self.signals.boundary, self.signals.calendar, self.signals.publication, self.signals.closes)
			artifact = load_recommendation_artifact(training.artifact, ArtifactKind.MODEL)
			validate_model(artifact, request, timing, self.signals.calendar)
			self.artifact = artifact
			self._training_runs.append((timing, training))
		return self.signals.forecast(self.artifact, request, timing, training)
