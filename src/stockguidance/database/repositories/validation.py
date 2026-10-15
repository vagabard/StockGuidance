from dataclasses import dataclass
from datetime import date
from typing import Protocol

from stockguidance.database.repositories.market_data import SessionSnapshot
from stockguidance.database.repositories.sessions import SessionQuery
from stockguidance.validation.findings import ValidationFinding


@dataclass(frozen=True)
class SessionRecordCounts:
	session_date: date
	raw_count: int
	derived_count: int


@dataclass(frozen=True)
class DatabaseInspection:
	snapshots: tuple[SessionSnapshot, ...]
	findings: tuple[ValidationFinding, ...] = ()
	record_counts: tuple[SessionRecordCounts, ...] = ()
	ticker_symbols: tuple[tuple[int, str], ...] = ()


class ValidationRepository(Protocol):
	def inspect(self, query: SessionQuery) -> DatabaseInspection: ...
