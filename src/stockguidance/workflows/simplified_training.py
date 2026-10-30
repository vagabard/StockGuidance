"""Training orchestration for the versioned target database."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from stockguidance.calendar.clock import Clock, SystemClock
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import SourceConfig
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.database.raw_rebuild import RawPayloadProvider
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.specification import TargetSpecification
from stockguidance.training.configuration import TrainingConfig
from stockguidance.training.result import TrainingResult
from stockguidance.workflows.simplified_readiness import (
	SimplifiedCloseRepository,
	SimplifiedReadinessWorkflow,
)
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


class SimplifiedTrainingJob(Protocol):
	@property
	def source(self) -> SourceConfig: ...
	@property
	def features(self) -> FeatureSpec: ...
	@property
	def configuration(self) -> TrainingConfig: ...
	@property
	def target(self) -> TargetSpecification: ...
	@property
	def start(self) -> date: ...
	@property
	def end(self) -> date: ...
	@property
	def cutoff(self) -> datetime: ...
	@property
	def publication(self) -> DailyPublicationPolicy: ...
	@property
	def decision_lead(self) -> timedelta: ...
	@property
	def evaluation_start(self) -> date | None: ...
	@property
	def evaluation_end(self) -> date | None: ...
	@property
	def evaluation_cutoff(self) -> datetime | None: ...
	@property
	def availability_policy(self) -> AvailabilityPolicy: ...
	@property
	def data_snapshot(self) -> datetime | None: ...


def _calendar(job: SimplifiedTrainingJob) -> ExchangeCalendar:
	first = min(job.start, job.evaluation_start or job.start)
	last = max(job.end, job.evaluation_end or job.end)
	padding = timedelta(days=max(30, (job.features.lookback_sessions + 2) * 3))
	return ExchangeCalendar(job.source.calendar_id, first - padding, last + padding)


def _decisions(calendar: ExchangeCalendar, start: date, end: date, lead: timedelta) -> tuple[DecisionTiming, ...]:
	return tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - lead) for session in calendar.sessions(start, end))


def execute_simplified_training(
	job: SimplifiedTrainingJob,
	artifact_path: Path,
	*,
	connect: WriteConnectionFactory,
	provider: RawPayloadProvider | None,
	clock: Clock | None = None,
	checkpoint_path: Path | None = None,
	resume_from: Path | None = None,
) -> TrainingResult:
	"""Ensure target coverage, then invoke the unchanged typed training path."""
	when = clock or SystemClock()
	calendar = _calendar(job)
	decisions = _decisions(calendar, job.start, job.end, job.decision_lead)
	evaluation_decisions = () if job.evaluation_start is None or job.evaluation_end is None else _decisions(calendar, job.evaluation_start, job.evaluation_end, job.decision_lead)
	all_decisions = tuple(sorted((*decisions, *evaluation_decisions), key=lambda item: item.entry_session.sequence))
	label_cutoff = job.evaluation_cutoff or job.cutoff
	readiness = SimplifiedReadinessWorkflow(job.source, connect, calendar, job.publication, provider, when)
	result = readiness.ensure(
		all_decisions,
		job.features,
		job.target,
		label_cutoff=label_cutoff,
		availability_policy=job.availability_policy,
		data_snapshot=job.data_snapshot,
	)
	repository = SimplifiedCloseRepository(job.source, connect)
	request = HistoryTrainingRequest(
		decisions,
		job.cutoff,
		job.features,
		job.target,
		job.configuration,
		artifact_path,
		evaluation_decisions,
		job.evaluation_cutoff,
		checkpoint_path,
		resume_from,
		job.availability_policy,
		job.data_snapshot,
	)
	training = train_from_history(request, result.boundary, calendar, job.publication, repository)
	# The dataset revision already includes every read row.  Keep the explicit
	# database revision available to callers that report readiness separately.
	if not result.successful:
		raise RuntimeError("Target database readiness did not complete")
	return training


__all__ = ["execute_simplified_training"]
