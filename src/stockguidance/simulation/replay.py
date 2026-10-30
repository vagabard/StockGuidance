"""Offline saved-score replay for completed Stage 10 simulations.

The replay adapters deliberately accept only the data written by the simulation
report.  They never open a database, refresh a provider, or load a model.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.sessions import TradingSession
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.config.source import SourceIdentity
from stockguidance.datasets.sample import Sample, TargetState
from stockguidance.features.construction import FeatureValues
from stockguidance.features.history_window import WindowState
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.closes import CloseIssue, PriceState
from stockguidance.market_data.identity import mapping
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.portfolio.marks import ClosingMark
from stockguidance.recommendations.eligibility import EligibilityState, LiquidityObservation, TickerEligibility
from stockguidance.recommendations.ranking import rank_scores
from stockguidance.recommendations.result import RankedTicker
from stockguidance.recommendations.scoring import RankedPrediction
from stockguidance.simulation.configuration import EvaluationPolicy, ScorePolicy, SimulationRequest
from stockguidance.simulation.prices import ExecutionFrame, ExecutionPriceSource
from stockguidance.simulation.score_source import DecisionForecast, DecisionUnavailable, ScoreSource
from stockguidance.training.result import TrainingResult
from stockguidance.targets.construction import TargetValues


class ReplayCoverageError(ValueError):
	"""Saved evidence does not cover the requested replay scope."""


def _text(value: object, name: str) -> str:
	if not isinstance(value, str) or not value:
		raise ValueError(f"Saved replay {name} must be nonempty text")
	return value


def _integer(value: object, name: str) -> int:
	if type(value) is not int:
		raise ValueError(f"Saved replay {name} must be an integer")
	return value


def _real(value: object, name: str) -> float:
	if not isinstance(value, (int, float)) or isinstance(value, bool):
		raise ValueError(f"Saved replay {name} must be numeric")
	return float(value)


def _rows(value: object, name: str) -> tuple[Mapping[str, object], ...]:
	if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
		raise ValueError(f"Saved replay {name} must be a list of objects")
	return tuple(item for item in value)


class SavedExecutionPrices(ExecutionPriceSource):
	"""Execution evidence loaded entirely from a saved simulation report."""

	def __init__(self, source: SourceIdentity, audit_cutoff: datetime, frames: tuple[ExecutionFrame, ...]) -> None:
		if not isinstance(source, SourceIdentity) or not isinstance(audit_cutoff, datetime) or audit_cutoff.tzinfo is None:
			raise ValueError("Saved execution prices require a typed source and aware audit cutoff")
		if not frames or any(frame.source != source for frame in frames):
			raise ValueError("Saved execution prices require source-matching frames")
		if len({frame.session for frame in frames}) != len(frames):
			raise ValueError("Saved execution evidence cannot contain duplicate sessions")
		self._source, self._audit_cutoff = source, audit_cutoff
		self._frames = {frame.session: frame for frame in frames}

	@property
	def source(self) -> SourceIdentity:
		return self._source

	@property
	def audit_cutoff(self) -> datetime:
		return self._audit_cutoff

	@property
	def frames(self) -> tuple[ExecutionFrame, ...]:
		return tuple(self._frames.values())

	def read(self, session: TradingSession, ticker_ids: tuple[int, ...]) -> ExecutionFrame:
		frame = self._frames.get(session)
		if frame is None:
			raise ReplayCoverageError(f"Saved execution quotes do not cover session {session.session_date.isoformat()}")
		if frame.ticker_ids != ticker_ids:
			missing = sorted(set(ticker_ids) - set(frame.ticker_ids))
			extra = sorted(set(frame.ticker_ids) - set(ticker_ids))
			raise ReplayCoverageError(f"Saved execution quote coverage differs for {session.session_date.isoformat()}; missing={missing}, extra={extra}")
		return frame

	@classmethod
	def from_report(cls, report: Mapping[str, object], source: SourceIdentity, calendar: SessionCalendar, ticker_ids: tuple[int, ...]) -> "SavedExecutionPrices":
		values = _rows(report.get("execution_evidence"), "execution_evidence")
		frames: list[ExecutionFrame] = []
		for value in values:
			session = calendar.session(datetime.fromisoformat(_text(value.get("session"), "execution session")).date())
			quotes: list[ClosingMark] = []
			for quote_value in _rows(value.get("quotes"), "quotes"):
				quotes.append(ClosingMark(_integer(quote_value.get("ticker_id"), "quote ticker ID"), session, Decimal(_text(quote_value.get("price"), "quote price")), _text(quote_value.get("revision"), "quote revision")))
			issues: list[CloseIssue] = []
			for issue_value in _rows(value.get("issues"), "issues"):
				issues.append(CloseIssue(_integer(issue_value.get("ticker_id"), "issue ticker ID"), session, PriceState(_text(issue_value.get("state"), "issue state")), "Saved execution issue"))
			frames.append(ExecutionFrame(source, session, ticker_ids, tuple(quotes), tuple(issues)))
		cutoff = datetime.fromisoformat(_text(report.get("execution_audit_cutoff"), "execution audit cutoff"))
		return cls(source, cutoff, tuple(frames))


class SavedScoreSource(ScoreSource):
	"""Score source backed by all ordered scores saved by a simulation."""

	policy = ScorePolicy(EvaluationPolicy.FIXED)

	def __init__(self, report: Mapping[str, object], request: SimulationRequest, calendar: SessionCalendar) -> None:
		if report.get("schema") not in ("stockguidance-simulation-1", "stockguidance-simulation-2"):
			raise ValueError("Unsupported saved simulation report")
		self.report, self.request, self.calendar = report, request, calendar
		self._decisions = {self._entry_date(item): item for item in _rows(report.get("decisions"), "decisions")}
		if len(self._decisions) != len(_rows(report.get("decisions"), "decisions")):
			raise ValueError("Saved simulation decisions contain duplicate entry sessions")

	@property
	def training_runs(self) -> tuple[tuple[DecisionTiming, TrainingResult], ...]:
		return ()

	@staticmethod
	def _entry_date(value: Mapping[str, object]) -> date:
		return datetime.fromisoformat(_text(value.get("entry_session"), "entry session")).date()

	def validate(self, request: SimulationRequest, first: DecisionTiming, calendar: SessionCalendar) -> None:
		if request.source != self.request.source or request.features != self.request.features or request.target != self.request.target:
			raise ValueError("Saved score specifications differ from the requested replay")
		if self.calendar.calendar_id != calendar.calendar_id or self.calendar.version != calendar.version:
			raise ValueError("Saved score calendar differs from the requested replay")
		from stockguidance.portfolio.schedule import scheduled_allocation
		stored_policy = mapping(self.report.get("allocation")).get("policy")
		required = tuple(timing.entry_session.session_date for timing in (DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - request.decision_lead) for session in calendar.sessions(request.start, request.end)) if scheduled_allocation(request.allocation, timing.entry_session) is not None)
		if stored_policy != request.allocation.policy.value:
			required = tuple(timing.entry_session.session_date for timing in (DecisionTiming(calendar.shift(session, -1), session, calendar.shift(session, 1), session.closes_at - request.decision_lead) for session in calendar.sessions(request.start, request.end)))
		if any(session not in self._decisions for session in required):
			raise ReplayCoverageError("Saved scores do not cover the requested scheduled decisions; changed schedule requires saved scores for every selected session")

	def _eligibility(self, value: object, timing: DecisionTiming) -> tuple[TickerEligibility, ...]:
		result: list[TickerEligibility] = []
		for item in _rows(value, "eligibility"):
			ticker_id = _integer(item.get("ticker_id"), "eligibility ticker ID")
			state = EligibilityState(_text(item.get("state"), "eligibility state"))
			observation = None
			if state is not EligibilityState.UNAVAILABLE:
				transactions = item.get("transactions")
				observation_date = item.get("observation_session")
				observation_session = timing.previous_session if observation_date is None else self.calendar.session(date.fromisoformat(_text(observation_date, "observation session")))
				observed_at = timing.previous_session.closes_at if item.get("observed_at") is None else datetime.fromisoformat(_text(item.get("observed_at"), "observed time"))
				available_at = observed_at if item.get("available_at") is None else datetime.fromisoformat(_text(item.get("available_at"), "available time"))
				observation = LiquidityObservation(ticker_id, 0 if transactions is None else _integer(transactions, "eligibility transactions"), ObservationTiming(observation_session, observed_at, available_at), _text(item.get("revision") or "saved-eligibility", "eligibility revision"))
			result.append(TickerEligibility(ticker_id, state, _text(item.get("detail"), "eligibility detail"), observation))
		return tuple(result)

	def forecast(self, request: SimulationRequest, timing: DecisionTiming, buy_index: int) -> DecisionForecast:
		value = self._decisions.get(timing.entry_session.session_date)
		if value is None:
			raise DecisionUnavailable(f"Saved scores do not cover evaluation session {timing.entry_session.session_date.isoformat()}")
		scores = tuple(RankedTicker(_integer(item.get("ticker_id"), "score ticker ID"), _real(item.get("score"), "score"), None if item.get("symbol") is None else str(item["symbol"])) for item in _rows(value.get("scores"), "scores"))
		eligibility = self._eligibility(value.get("eligibility"), timing)
		if tuple(item.ticker_id for item in scores) != request.features.ticker_ids or tuple(item.ticker_id for item in eligibility) != request.features.ticker_ids:
			missing = sorted(set(request.features.ticker_ids) - {item.ticker_id for item in scores})
			raise ReplayCoverageError(f"Saved scores do not cover the requested ordered universe; missing={missing}")
		ranking = rank_scores(scores, eligibility, request.top_k)
		sessions = tuple(self.calendar.shift(timing.entry_session, offset) for offset in range(-request.features.lookback_sessions, 0))
		features = FeatureValues(request.features, sessions, (0.0,) * request.features.input_size)
		targets = TargetValues((None,) * len(request.features.ticker_ids), (None,) * len(request.features.ticker_ids))
		placeholder = Sample(request.source, timing, features, WindowState.EXPECTED_AVAILABLE, request.target, targets, TargetState.NOT_REQUESTED, None, _text(value.get("feature_identity"), "feature identity"), _text(value.get("data_revision"), "data revision"), self.calendar.version, request.availability_policy, request.data_snapshot)
		return DecisionForecast(_text(value.get("artifact_id"), "artifact ID"), placeholder, RankedPrediction(scores, eligibility, ranking, _text(value.get("data_revision"), "prediction revision"), (), ()), None)


def load_saved_replay(report_path: Path) -> dict[str, object]:
	"""Read a saved report without importing a model or contacting a data source."""
	import json

	value = json.loads(report_path.read_text(encoding="utf-8"))
	return mapping(value)
