"""Feed shared recommendation workflows into the existing investment ledger."""

from dataclasses import replace
from pathlib import Path
from typing import Any,Callable

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.recommendations.jobs import RecommendationConfiguration
from stockguidance.simulation.configuration import SimulationRequest
from stockguidance.training.result import TrainingResult
from stockguidance.workflows.flexible_recommendation import RecommendationSetup
from stockguidance.workflows.recommendation_result import RecommendationRunResult

from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.recommendations.options import RecommendationOptions
from stockguidance.recommendations.configuration import RefreshMode
from stockguidance.recommendations.scoring import RankedPrediction
from stockguidance.simulation.configuration import ScorePolicy, EvaluationPolicy
from stockguidance.simulation.score_source import DecisionForecast


class RecommendationReplayScores:
	"""Freeze membership per artifact and resolve it again only on retraining."""

	dynamic_universe = True

	def __init__(self, setup: RecommendationSetup, artifact_directory: Path, *, every_buy_decisions: int=1, resolver: Callable[[RecommendationSetup,RecommendationOptions],tuple[RecommendationConfiguration,RecommendationOptions]] | None=None, runner: Callable[[RecommendationConfiguration,RecommendationOptions],RecommendationRunResult] | None=None) -> None:
		from stockguidance.workflows.flexible_recommendation import resolve_setup
		from stockguidance.workflows.recommendation_runtime import assemble_and_recommend
		self.setup,self.artifact_directory = setup,artifact_directory
		self.policy = ScorePolicy(EvaluationPolicy.CHRONOLOGICAL_RETRAIN,every_buy_decisions)
		self.resolver,self.runner = resolver or resolve_setup,runner or assemble_and_recommend
		self.configuration: RecommendationConfiguration | None = None
		self.reference: ArtifactReference | None = None
		self._training_runs: list[tuple[DecisionTiming,TrainingResult]] = []
		self.selections: list[dict[str,Any] | None] = []

	@property
	def training_runs(self) -> tuple[tuple[DecisionTiming,TrainingResult],...]:
		return tuple(self._training_runs)

	def validate(self, request: SimulationRequest, first: DecisionTiming, calendar: SessionCalendar) -> None:
		if self.configuration is not None:
			raise ValueError("Use a fresh recommendation score source for each simulation")
		if first.decision_cutoff < first.entry_session.opens_at:
			raise ValueError("Simulation decisions must occur during the trading session")

	def forecast(self, request: SimulationRequest, timing: DecisionTiming, buy_index: int) -> DecisionForecast:
		if not isinstance(self.setup,RecommendationSetup):
			raise ValueError("Recommendation simulation requires typed setup settings")
		cadence = self.policy.retrain_every_buy_decisions
		assert cadence is not None
		training_due = self.reference is None or buy_index % cadence == 0
		path = self.artifact_directory / f"decision-{timing.entry_session.session_date}.sgmodel"
		if training_due:
			base = dict(self.setup.template or {})
			base["allocation"] = {"policy":request.allocation.policy.value,"anchor_date":str(request.allocation.anchor_session.session_date)}
			base["top_k"] = request.top_k
			retraining = dict(base.get("retraining") or {})
			retraining["decision_minutes_before_close"] = request.decision_lead.total_seconds()/60
			base["retraining"] = retraining
			setup = replace(self.setup,end=str(timing.previous_session.session_date),template=base,automatic_artifact=False,minimum_transactions=request.eligibility.minimum_transactions)
			options = RecommendationOptions(ModelRefreshPolicy.RETRAIN,path)
			configuration,options = self.resolver(setup,options)
			if configuration.entry_date != timing.entry_session.session_date:
				raise ValueError("Resolved historical endpoint differs from the simulation decision")
			configuration = replace(configuration,replay_cutoff=timing.decision_cutoff)
			options = replace(options,refresh_mode=RefreshMode.VALIDATE_ONLY)
		else:
			assert self.configuration is not None and self.reference is not None
			metadata = {**(self.configuration.resolution_metadata or {}),"entry_session":str(timing.entry_session.session_date),"decision_cutoff":timing.decision_cutoff.isoformat(),"feature_end_session":str(timing.previous_session.session_date)}
			configuration = replace(self.configuration,end=timing.previous_session.session_date,entry_date=timing.entry_session.session_date,retraining=None,replay_cutoff=timing.decision_cutoff,resolution_metadata=metadata)
			options = RecommendationOptions(ModelRefreshPolicy.CURRENT,self.reference.path,self.reference.artifact_id,refresh_mode=RefreshMode.VALIDATE_ONLY)
		run = self.runner(configuration,options)
		if run.sample.timing != timing:
			raise ValueError("Recommendation changed the simulation decision cutoff")
		if training_due:
			if run.training is None:
				raise ValueError("Retraining returned no training evidence")
			self.configuration,self.reference = configuration,run.training.artifact
			self._training_runs.append((timing,run.training))
			self.selections.append(configuration.resolution_metadata)
		prediction = RankedPrediction(run.scores,run.eligibility,run.recommendation.ranking,run.recommendation.data_revision,run.member_artifact_ids,run.member_weights)
		return DecisionForecast(run.recommendation.artifact_id,run.sample,prediction,run.training,run.resolution_metadata)
