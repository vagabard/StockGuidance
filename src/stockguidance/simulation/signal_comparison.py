from dataclasses import dataclass, replace
from datetime import date, datetime

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.closes import CloseRepository
from stockguidance.datasets.building import build_sample
from stockguidance.datasets.training import TrainingDataset, build_training_dataset
from stockguidance.features.construction import FeatureValues
from stockguidance.features.specification import CloseFeature, FeatureSpec, SignalMode
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.reporting.simulation import simulation_payload
from stockguidance.simulation.configuration import ScorePolicy, SimulationRequest
from stockguidance.simulation.engine import simulate
from stockguidance.simulation.prices import ExecutionPriceSource
from stockguidance.simulation.result import SimulationResult
from stockguidance.simulation.score_source import ChronologicalModelScores, DecisionForecast, DecisionUnavailable, HistoricalSignals, WalkForwardTraining
from stockguidance.training.result import TrainingResult
from stockguidance.training.service import train_model
from stockguidance.workflows.train_model import HistoryTrainingRequest


def signal_specifications(features: FeatureSpec) -> tuple[FeatureSpec, FeatureSpec]:
	order = tuple(feature for feature in features.feature_order if feature is not CloseFeature.ENTRY_OPEN_RETURN)
	previous = replace(features, signal_mode=SignalMode.PREVIOUS_CLOSE, feature_order=order)
	return previous, replace(previous, signal_mode=SignalMode.ENTRY_OPEN, feature_order=(*order, CloseFeature.ENTRY_OPEN_RETURN))


@dataclass(frozen=True)
class CohortExclusion:
	entry: date
	reason: str
	cutoff: datetime


class MatchedHistoryTrainer:
	def __init__(self, specifications: tuple[FeatureSpec, FeatureSpec]) -> None:
		self.specifications = specifications
		self.exclusions: list[CohortExclusion] = []

	def __call__(self, request: HistoryTrainingRequest, boundary: HistoryBoundary, calendar: SessionCalendar, publication: DailyPublicationPolicy, repository: CloseRepository) -> TrainingResult:
		datasets = tuple(build_training_dataset(request.decisions, request.training_cutoff, features, request.target, boundary, calendar, publication, repository) for features in self.specifications)
		common = set.intersection(*(set(sample.timing.entry_session for sample in dataset.eligible) for dataset in datasets))
		selected = datasets[self.specifications.index(request.features)]
		samples = []
		for sample in selected.samples:
			if sample.timing.entry_session not in common:
				self.exclusions.append(CohortExclusion(sample.timing.entry_session.session_date, "Unavailable features or immature outcome in one or both signal modes; excluded from both fits", request.training_cutoff))
				features = FeatureValues(sample.features.specification, sample.features.sessions, None, sample.features.issues)
				sample = replace(sample, features=features, feature_revision=content_id({"original": sample.feature_revision, "cohort": "paired_signal_intersection"}))
			samples.append(sample)
		return train_model(TrainingDataset(tuple(samples), request.training_cutoff), request.configuration, request.artifact_path)


class MatchedScores:
	def __init__(self, scores: ChronologicalModelScores, specifications: tuple[FeatureSpec, FeatureSpec]) -> None:
		self.scores, self.specifications = scores, specifications
		self.exclusions: list[CohortExclusion] = []

	@property
	def policy(self) -> ScorePolicy:
		return self.scores.policy

	@property
	def training_runs(self) -> tuple[tuple[DecisionTiming, TrainingResult], ...]:
		return self.scores.training_runs

	def validate(self, request: SimulationRequest, first: DecisionTiming, calendar: SessionCalendar) -> None:
		self.scores.validate(request, first, calendar)

	def forecast(self, request: SimulationRequest, timing: DecisionTiming, buy_index: int) -> DecisionForecast:
		signals = self.scores.signals
		for features in self.specifications:
			sample = build_sample(features, request.target, timing, signals.boundary, signals.calendar, signals.publication, signals.closes)
			if sample.features.values is None:
				reason = "A required close or full-universe opening is unavailable; both signal modes skip this anchored decision"
				self.exclusions.append(CohortExclusion(timing.entry_session.session_date, reason, timing.decision_cutoff))
				raise DecisionUnavailable(reason)
		return self.scores.forecast(request, timing, buy_index)


@dataclass(frozen=True)
class SignalComparison:
	previous_close: SimulationResult
	entry_open: SimulationResult
	excluded_training: tuple[CohortExclusion, ...]
	excluded_decisions: tuple[CohortExclusion, ...]
	seed: int
	epochs: int
	every_buy_decisions: int

	def payload(self) -> dict[str, object]:
		def exclusions(items: tuple[CohortExclusion, ...]) -> list[dict[str, str]]:
			return [{"entry_session": item.entry.isoformat(), "cutoff": item.cutoff.isoformat(), "reason": item.reason} for item in items]

		return {"schema": "stockguidance-signal-comparison-1", "successful": self.previous_close.successful and self.entry_open.successful, "cohort_policy": "intersection_at_each_training_and_decision_cutoff", "seed": self.seed, "epochs": self.epochs, "every_buy_decisions": self.every_buy_decisions, "excluded_training": exclusions(self.excluded_training), "excluded_decisions": exclusions(self.excluded_decisions), "previous_close": simulation_payload(self.previous_close), "entry_open": simulation_payload(self.entry_open)}


def compare_signal_modes(request: SimulationRequest, boundary: HistoryBoundary, calendar: SessionCalendar, signals: HistoricalSignals, prices: ExecutionPriceSource, plan: WalkForwardTraining) -> SignalComparison:
	if signals.boundary != boundary or signals.calendar is not calendar or request.source != boundary.source:
		raise ValueError("Comparison source, history and calendar must be identical")
	specifications = signal_specifications(request.features)
	results, trainers, score_sources = [], [], []
	for features in specifications:
		trainer = MatchedHistoryTrainer(specifications)
		mode_plan = replace(plan, artifact_directory=plan.artifact_directory / features.signal_mode.value)
		scores = MatchedScores(ChronologicalModelScores(mode_plan, signals, trainer=trainer), specifications)
		results.append(simulate(replace(request, features=features), boundary, calendar, scores, prices))
		trainers.append(trainer)
		score_sources.append(scores)
	training_exclusions = tuple(dict.fromkeys(item for trainer in trainers for item in trainer.exclusions))
	decision_exclusions = tuple(dict.fromkeys(item for scores in score_sources for item in scores.exclusions))
	return SignalComparison(results[0], results[1], training_exclusions, decision_exclusions, plan.configuration.seed, plan.configuration.epochs, plan.every_buy_decisions)
