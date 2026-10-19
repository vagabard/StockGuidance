from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path

from stockguidance.artifacts.decoding import decode_features, exact_mapping
from stockguidance.calendar.clock import SystemClock
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.training import decode_training_configuration, number
from stockguidance.config.source import SourceConfig
from stockguidance.config.validation import require_aware_datetime
from stockguidance.database.repositories.mysql_validation import MySQLValidationRepository
from stockguidance.database.repositories.verified_closes import VerifiedCloseRepository
from stockguidance.database.repositories.closes import CloseRepository
from stockguidance.database.repositories.signals import SignalRepository
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.history import HistoryBoundary, UnknownHistoryBoundary
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.identity import decode_source, mapping, text_field
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.market_data.opening_store import OpeningStore
from stockguidance.targets.specification import TargetSpec
from stockguidance.training.configuration import TrainingConfig
from stockguidance.training.result import TrainingResult
from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history


@dataclass(frozen=True)
class TrainingJob:
	source: SourceConfig
	features: FeatureSpec
	configuration: TrainingConfig
	start: date
	end: date
	cutoff: datetime
	publication: DailyPublicationPolicy
	decision_lead: timedelta
	evaluation_start: date | None
	evaluation_end: date | None
	evaluation_cutoff: datetime | None
	availability_policy: AvailabilityPolicy = AvailabilityPolicy.STRICT
	data_snapshot: datetime | None = None


def load_job(path: Path) -> TrainingJob:
	fields = mapping(json.loads(path.read_text(encoding="utf-8")))
	required = ("source", "data_root", "database_config", "features", "model", "training", "start_date", "end_date", "training_cutoff", "publication_delay_minutes", "publication_evidence", "decision_minutes_before_close", "evaluation")
	if set(fields) - {*required, "availability_policy", "data_snapshot"} or any(name not in fields for name in required):
		raise ValueError("Incomplete or unsupported training job fields")
	identity = decode_source(exact_mapping(fields["source"], ("provider", "endpoint", "market", "universe", "adjustment", "interval")))
	source = SourceConfig(identity, Path(text_field(fields, "data_root")), Path(text_field(fields, "database_config")))
	features = decode_features(fields["features"])
	if features.adjustment is not identity.adjustment or features.interval is not identity.interval:
		raise ValueError("Training source and feature specifications disagree")
	configuration = decode_training_configuration(fields["training"], fields["model"])
	cutoff = datetime.fromisoformat(text_field(fields, "training_cutoff"))
	require_aware_datetime(cutoff, "training cutoff")
	start, end = date.fromisoformat(text_field(fields, "start_date")), date.fromisoformat(text_field(fields, "end_date"))
	lead = timedelta(minutes=number(fields["decision_minutes_before_close"]))
	if start > end or cutoff > SystemClock().now() or not timedelta(0) < lead < timedelta(days=1):
		raise ValueError("Invalid training period, cutoff or pre-close decision lead")
	publication = DailyPublicationPolicy(timedelta(minutes=number(fields["publication_delay_minutes"])), text_field(fields, "publication_evidence"))
	evaluation = None if fields["evaluation"] is None else exact_mapping(fields["evaluation"], ("start_date", "end_date", "label_cutoff"))
	evaluation_start = None if evaluation is None else date.fromisoformat(text_field(evaluation, "start_date"))
	evaluation_end = None if evaluation is None else date.fromisoformat(text_field(evaluation, "end_date"))
	evaluation_cutoff = None if evaluation is None else datetime.fromisoformat(text_field(evaluation, "label_cutoff"))
	if evaluation_cutoff is not None:
		require_aware_datetime(evaluation_cutoff, "evaluation cutoff")
		if evaluation_start is None or evaluation_end is None or not end < evaluation_start <= evaluation_end or evaluation_cutoff < cutoff or evaluation_cutoff > SystemClock().now():
				raise ValueError("Evaluation must follow training with an available explicit label cutoff")
	availability_policy = AvailabilityPolicy(str(fields.get("availability_policy", AvailabilityPolicy.STRICT.value)))
	data_snapshot = None if fields.get("data_snapshot") is None else datetime.fromisoformat(text_field(fields, "data_snapshot"))
	if data_snapshot is not None:
		require_aware_datetime(data_snapshot, "data snapshot")
	if availability_policy.retrospective and data_snapshot is None:
		raise ValueError("Retrospective training requires data_snapshot")
	if data_snapshot is not None and data_snapshot > SystemClock().now():
		raise ValueError("Data snapshot cannot be in the future")
	if availability_policy.retrospective and data_snapshot is not None and data_snapshot < cutoff:
		raise ValueError("Retrospective data_snapshot must include the training cutoff")
	return TrainingJob(source, features, configuration, start, end, cutoff, publication, lead, evaluation_start, evaluation_end, evaluation_cutoff, availability_policy, data_snapshot)


@dataclass(frozen=True)
class JobHistory:
	boundary: HistoryBoundary
	calendar: SessionCalendar
	repository: CloseRepository
	closes: VerifiedCloseRepository
	decisions: tuple[DecisionTiming, ...]
	evaluation_decisions: tuple[DecisionTiming, ...]


def open_job_history(job: TrainingJob, *, connect: WriteConnectionFactory | None = None) -> JobHistory:
	boundary = JsonHistoryStore(job.source.data_root / "history").load(job.source.identity)
	if boundary is None:
		raise UnknownHistoryBoundary("Training requires evidenced, persisted source history")
	end = max(job.end, job.evaluation_end or job.end)
	calendar = ExchangeCalendar(boundary.calendar_id, boundary.history_start - timedelta(days=14), end + timedelta(days=14))
	if job.start < boundary.history_start:
		raise ValueError("Training period precedes supported source history")

	def decisions(start: date, end: date) -> tuple[DecisionTiming, ...]:
		return tuple(DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - job.decision_lead) for session in calendar.sessions(start, end))

	evaluation_decisions = () if job.evaluation_start is None or job.evaluation_end is None else decisions(job.evaluation_start, job.evaluation_end)
	table_names = load_database_configuration(job.source.database_config).load_table_names()
	repository = VerifiedCloseRepository(job.source, MySQLValidationRepository(job.source, connect or MySQLConnectionFactory(), table_names))
	return JobHistory(boundary, calendar, SignalRepository(repository, OpeningStore(job.source.data_root)), repository, decisions(job.start, job.end), evaluation_decisions)


def execute_job(job: TrainingJob, artifact_path: Path, *, checkpoint_path: Path | None = None, resume_from: Path | None = None) -> TrainingResult:
	history = open_job_history(job)
	request = HistoryTrainingRequest(history.decisions, job.cutoff, job.features, TargetSpec(), job.configuration, artifact_path, history.evaluation_decisions, job.evaluation_cutoff, checkpoint_path, resume_from, job.availability_policy, job.data_snapshot)
	return train_from_history(request, history.boundary, history.calendar, job.publication, history.repository)
