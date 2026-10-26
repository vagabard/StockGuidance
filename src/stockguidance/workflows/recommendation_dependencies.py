from dataclasses import dataclass
from typing import Protocol

from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.database.repositories.closes import CloseRepository
from stockguidance.database.repositories.eligibility import EligibilityRepository
from stockguidance.database.repositories.market_data import MarketDataRepository
from stockguidance.database.repositories.validation import ValidationRepository
from stockguidance.market_data.history import HistoryBoundary
from stockguidance.market_data.opening_refresh import OpeningRefresh
from stockguidance.market_data.providers.contracts import MarketDataProvider
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.training.result import TrainingResult
from stockguidance.workflows.train_model import HistoryTrainingRequest
from stockguidance.workflows.simplified_readiness import SimplifiedReadinessWorkflow


@dataclass(frozen=True)
class RecommendationServices:
	provider: MarketDataProvider
	market_data: MarketDataRepository | None
	validation: ValidationRepository | None
	closes: CloseRepository
	eligibility: EligibilityRepository
	openings: OpeningRefresh | None = None
	simplified_readiness: SimplifiedReadinessWorkflow | None = None


class HistoryTrainer(Protocol):
	def __call__(self, request: HistoryTrainingRequest, boundary: HistoryBoundary, calendar: SessionCalendar, publication: DailyPublicationPolicy, repository: CloseRepository) -> TrainingResult: ...
