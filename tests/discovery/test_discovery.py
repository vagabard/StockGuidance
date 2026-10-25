from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.market_data.discovery import (
	DiscoveryRequest,
	HistoryOverride,
	HistoryOverrideConflict,
	discover_coverage,
)
from stockguidance.market_data.history import EvidenceQuality, HistoryProvenance, LocalCoverageInventory, LocalObservation, establish_history_boundary
from stockguidance.market_data.history_store import JsonHistoryStore
from stockguidance.market_data.manifests import ManifestStore
from stockguidance.market_data.providers.contracts import DownloadedSession, ProviderResponse, ResponseState
from stockguidance.market_data.records import DailyBar
from stockguidance.market_data.providers.contracts import CapabilityState, ProviderCapability
from stockguidance.market_data.publication import DailyPublicationPolicy


@dataclass
class FixtureCalendar:
	dates: tuple[date, ...]

	calendar_id = "fixture-calendar"
	version = "fixture-v1"
	timezone = ZoneInfo("UTC")

	@property
	def start(self) -> date:
		return self.dates[0]

	@property
	def end(self) -> date:
		return self.dates[-1]

	def session(self, session_date: date) -> TradingSession:
		if session_date not in self.dates:
			raise ValueError("not a fixture session")
		sequence = self.dates.index(session_date)
		return TradingSession("fixture-calendar", session_date, sequence, datetime.combine(session_date, datetime.min.time(), timezone.utc), datetime.combine(session_date, datetime.min.time(), timezone.utc) + timedelta(hours=8))

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		return tuple(self.session(item) for item in self.dates if start <= item <= end)

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		return self.session(self.dates[self.dates.index(session.session_date) + offset])


@dataclass
class FrozenClock:
	value: datetime

	def now(self) -> datetime:
		return self.value


@pytest.fixture
def source() -> SourceIdentity:
	return SourceIdentity("fixture", "grouped", "us-stocks", "all", PriceAdjustment.ADJUSTED)


@pytest.fixture
def calendar() -> FixtureCalendar:
	return FixtureCalendar((date(2021, 1, 4), date(2021, 1, 5), date(2021, 1, 6)))


def test_discovery_keeps_legacy_observations_unverified_and_finds_holes(source, calendar):
	inventory = LocalCoverageInventory(
		source,
		calendar.calendar_id,
		(
			LocalObservation(source, calendar.calendar_id, date(2021, 1, 4), EvidenceQuality.UNVERIFIED, "legacy rows", False),
			LocalObservation(source, calendar.calendar_id, date(2021, 1, 6), EvidenceQuality.VERIFIED, "source manifest", True),
		),
	)
	result = discover_coverage(source, calendar, FrozenClock(datetime(2021, 1, 7, tzinfo=timezone.utc)), DailyPublicationPolicy(timedelta(minutes=15), "fixture"), local_inventory=inventory)

	assert result.local_bounds.first_observed == date(2021, 1, 4)
	assert result.local_bounds.first_quality is EvidenceQuality.UNVERIFIED
	assert result.retained.verified_sessions == (date(2021, 1, 6),)
	assert result.retained.holes == (date(2021, 1, 4), date(2021, 1, 5))
	assert result.latest_expected_published_session.session_date == date(2021, 1, 6)
	assert result.provider.state is CapabilityState.UNKNOWN


def test_fresh_provider_capability_is_separate_from_retained_history(source, calendar):
	capability = ProviderCapability(source, CapabilityState.VERIFIED, datetime(2021, 1, 7, tzinfo=timezone.utc), "fixture account metadata", "bounded fixture entitlement", date(2021, 1, 5), date(2021, 1, 6), timedelta(days=2), date(2021, 1, 5))
	result = discover_coverage(
		source,
		calendar,
		FrozenClock(datetime(2021, 1, 8, tzinfo=timezone.utc)),
		DailyPublicationPolicy(timedelta(minutes=15), "fixture"),
		request=DiscoveryRequest(source),
		provider=type("CapabilityFixture", (), {"discover_capability": lambda self, requested_source, checked_at: capability})(),
	)

	assert result.provider_is_fresh
	assert result.fetchable_start == date(2021, 1, 5)
	assert result.fetchable_end == date(2021, 1, 6)
	assert result.retained.observed_sessions == ()


def test_older_retained_history_is_not_limited_by_current_provider_access(source):
	calendar = FixtureCalendar((date(2020, 12, 31), date(2021, 1, 4), date(2021, 1, 5), date(2021, 1, 6)))
	inventory = LocalCoverageInventory(source, calendar.calendar_id, (LocalObservation(source, calendar.calendar_id, date(2020, 12, 31), EvidenceQuality.VERIFIED, "retained manifest", True),))
	capability = ProviderCapability(source, CapabilityState.VERIFIED, datetime(2021, 1, 7, tzinfo=timezone.utc), "fixture account metadata", "provider begins in 2021", date(2021, 1, 4), date(2021, 1, 6), timedelta(days=2))
	result = discover_coverage(source, calendar, FrozenClock(datetime(2021, 1, 8, tzinfo=timezone.utc)), DailyPublicationPolicy(timedelta(minutes=15), "fixture"), local_inventory=inventory, provider_capability=capability)

	assert result.effective_start == date(2020, 12, 31)
	assert result.fetchable_start == date(2021, 1, 4)
	assert result.local_bounds.first_observed == date(2020, 12, 31)


def test_stale_capability_is_not_used_as_fetch_scope(source, calendar):
	capability = ProviderCapability(source, CapabilityState.VERIFIED, datetime(2021, 1, 1, tzinfo=timezone.utc), "fixture account metadata", "old fixture entitlement", date(2021, 1, 4), date(2021, 1, 6), timedelta(hours=1))
	result = discover_coverage(source, calendar, FrozenClock(datetime(2021, 1, 7, tzinfo=timezone.utc)), DailyPublicationPolicy(timedelta(minutes=15), "fixture"), provider=type("CapabilityFixture", (), {"discover_capability": lambda self, requested_source, checked_at: capability})())

	assert result.provider.state is CapabilityState.STALE
	assert result.fetchable_start is None and result.fetchable_end is None


def test_conflicting_override_is_reported_without_writing(source, calendar, tmp_path: Path):
	boundary = establish_history_boundary(source, calendar, date(2021, 1, 4), FrozenClock(datetime(2021, 1, 7, tzinfo=timezone.utc)), provenance=HistoryProvenance.SOURCE_HISTORY, evidence="retained fixture")
	with pytest.raises(HistoryOverrideConflict, match="conflicts") as error:
		discover_coverage(source, calendar, FrozenClock(datetime(2021, 1, 7, tzinfo=timezone.utc)), DailyPublicationPolicy(timedelta(minutes=15), "fixture"), history_boundary=boundary, history_override=HistoryOverride(source, date(2021, 1, 5), "override fixture"))
	assert error.value.diagnostics[0].code == "history_override_conflict"
	assert not list(tmp_path.iterdir())


def test_source_manifest_inventory_and_versioned_discovery_metadata_are_readable(source, calendar, tmp_path: Path):
	store = ManifestStore(tmp_path)
	session = calendar.session(date(2021, 1, 6))
	bar = DailyBar("AAA", Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"), 100, Decimal("10"), 2)
	response = ProviderResponse(ResponseState.COMPLETE, (bar,), 200, "OK", (("resultsCount", 1),), source_timestamps=(("AAA", int(session.closes_at.timestamp() * 1000)),))
	store.save_download(source, DownloadedSession(session, response, session.closes_at + timedelta(minutes=15)))
	inventory = store.inventory(source, calendar)
	assert inventory.verified_dates == ()
	assert tuple(item.session_date for item in inventory.observations) == (date(2021, 1, 6),)

	clock = FrozenClock(datetime(2021, 1, 7, tzinfo=timezone.utc))
	result = discover_coverage(source, calendar, clock, DailyPublicationPolicy(timedelta(minutes=15), "fixture"), local_inventory=inventory)
	metadata_store = JsonHistoryStore(tmp_path / "history")
	metadata_store.save_discovery_metadata(result.metadata())
	assert metadata_store.load_discovery_metadata(source) == result.metadata()
