from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import random
from typing import Callable, Sequence

from stockguidance.config.source import SourceConfig, VerificationMode
from stockguidance.database.repositories.mysql_sessions import Cursor
from stockguidance.database.table_names import TableNames, TableRole, quote_identifier
from stockguidance.market_data.identity import content_id
from stockguidance.market_data.records import DailyBar, bar_payload
from stockguidance.market_data.providers.contracts import MarketDataProvider
from stockguidance.calendar.sessions import TradingSession


class VerificationType(StrEnum):
	SOURCE_FAMILY = "source_family"
	ENDPOINT = "endpoint"


class VerificationStatus(StrEnum):
	PENDING = "pending"
	RUNNING = "running"
	PASSED = "passed"
	FAILED = "failed"


@dataclass(frozen=True)
class RecordComparison:
	baseline_digest: str
	candidate_digest: str
	mismatches: tuple[str, ...]

	@property
	def passed(self) -> bool:
		return not self.mismatches and self.baseline_digest == self.candidate_digest


@dataclass(frozen=True)
class SessionVerification:
	session: TradingSession
	comparison: RecordComparison


def verify_sessions(
	provider: MarketDataProvider, source: SourceConfig, sessions: Sequence[TradingSession],
	baseline_reader: Callable[[TradingSession], Sequence[DailyBar]], count: int, seed: int,
) -> tuple[SessionVerification, ...]:
	selected = tuple(sorted(random.Random(seed).sample(tuple(sessions), count), key=lambda session: session.session_date))
	if len(selected) != count:
		raise ValueError("Verification session range does not contain enough sessions")
	results: list[SessionVerification] = []
	for session in selected:
		response = provider.fetch(source, session)
		results.append(SessionVerification(session, compare_daily_bars(baseline_reader(session), response.records)))
	return tuple(results)


def sample_dates(values: Sequence[date], count: int, seed: int) -> tuple[date, ...]:
	if type(count) is not int or count <= 0 or type(seed) is not int:
		raise ValueError("Verification sample count and seed are invalid")
	unique = tuple(sorted(set(values)))
	if len(unique) < count:
		raise ValueError("Verification sample range does not contain enough distinct dates")
	return tuple(sorted(random.Random(seed).sample(unique, count)))


def compare_daily_bars(
	baseline: Sequence[DailyBar], candidate: Sequence[DailyBar],
	key: Callable[[DailyBar], object] | None = None,
) -> RecordComparison:
	key = key or (lambda bar: bar.ticker)
	baseline_rows = {key(bar): bar for bar in baseline}
	candidate_rows = {key(bar): bar for bar in candidate}
	if len(baseline_rows) != len(baseline) or len(candidate_rows) != len(candidate):
		raise ValueError("Verification records contain duplicate security keys")
	def digest(rows: Sequence[DailyBar]) -> str:
		return content_id([bar_payload(bar) for bar in sorted(rows, key=lambda bar: str(key(bar)))])
	mismatches: list[str] = []
	for security_key in sorted(set(baseline_rows) | set(candidate_rows), key=str):
		left, right = baseline_rows.get(security_key), candidate_rows.get(security_key)
		if left is None:
			mismatches.append(f"missing baseline security {security_key}")
		elif right is None:
			mismatches.append(f"missing candidate security {security_key}")
		elif bar_payload(left) != bar_payload(right):
			mismatches.append(f"record differs for security {security_key}")
	return RecordComparison(digest(baseline), digest(candidate), tuple(mismatches))


def register_verification(
	cursor: Cursor, table_names: TableNames, verification_type: VerificationType,
	baseline_key: str, candidate_key: str, requested_count: int, random_seed: int,
	registered_at: datetime,
) -> int:
	if not isinstance(verification_type, VerificationType) or len(baseline_key) != 64 or len(candidate_key) != 64:
		raise ValueError("Invalid verification registration")
	cursor.execute(
		f"INSERT INTO {quote_identifier(table_names.table(TableRole.VERIFICATIONS))} (comparison_type, baseline_key, candidate_key, status, requested_count, completed_count, random_seed, registered_at, completed_at, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
		(verification_type.value, baseline_key, candidate_key, VerificationStatus.PENDING.value, requested_count, 0, random_seed, registered_at.isoformat(), None, None),
	)
	cursor.execute(f"SELECT MAX(id) FROM {quote_identifier(table_names.table(TableRole.VERIFICATIONS))}", ())
	rows = cursor.fetchall()
	if len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not int:
		raise ValueError("Verification registration did not return an ID")
	return rows[0][0]


def write_sample(
	cursor: Cursor, table_names: TableNames, verification_id: int, sample_date: date,
	comparison: RecordComparison,
) -> None:
	status = VerificationStatus.PASSED.value if comparison.passed else VerificationStatus.FAILED.value
	detail = None if comparison.passed else "; ".join(comparison.mismatches)[:512]
	cursor.execute(
		f"INSERT INTO {quote_identifier(table_names.table(TableRole.VERIFICATION_SAMPLES))} (verification_id, sample_date, status, baseline_digest, candidate_digest, mismatch_count, detail) VALUES (%s, %s, %s, %s, %s, %s, %s)",
		(verification_id, sample_date, status, comparison.baseline_digest, comparison.candidate_digest, len(comparison.mismatches), detail),
	)


def verification_allows(status: VerificationStatus, mode: VerificationMode, write: bool) -> bool:
	if not isinstance(status, VerificationStatus) or not isinstance(mode, VerificationMode):
		raise ValueError("Invalid verification decision")
	if status is VerificationStatus.PASSED:
		return True
	if mode is VerificationMode.ADVISORY:
		return True
	if mode is VerificationMode.GATE_WRITES:
		return not write
	return False
