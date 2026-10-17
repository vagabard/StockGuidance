from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from stage3.conftest import calendar, harness
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.database.repositories.closes import CloseData
from stockguidance.datasets.building import build_sample
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import ObservationTiming
from stockguidance.market_data.closes import CloseIssue, CloseObservation, PriceState
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.specification import TargetSpec


class FixtureCloseRepository:
	def __init__(self, observations):
		self.observations = {(item.timing.session, item.ticker_id): item for item in observations}
		self.calls = []
		self.missing = set()
		self.unverified = set()

	def read(self, query):
		self.calls.append(query)
		observations, issues = [], []
		for session in reversed(query.scope.sessions):
			for ticker_id in reversed(query.ticker_ids):
				key = session, ticker_id
				observation = self.observations.get(key)
				if key in self.unverified:
					issues.append(CloseIssue(ticker_id, session, PriceState.UNVERIFIED, "Fixture evidence is missing"))
				elif key in self.missing or observation is None:
					issues.append(CloseIssue(ticker_id, session, PriceState.MISSING, "Fixture price is missing"))
				elif observation.timing.available_at > query.scope.as_of:
					issues.append(CloseIssue(ticker_id, session, PriceState.NOT_YET_AVAILABLE, "Fixture receipt is after cutoff"))
				else:
					observations.append(observation)
		return CloseData(query, tuple(observations), tuple(issues))


@pytest.fixture
def samples(calendar):
	source = SourceIdentity("fixture", "daily-closes", "us-stocks", "fixture-full-universe", PriceAdjustment.ADJUSTED)
	boundary = HistoryBoundary(source, calendar.calendar_id, date(2024, 7, 1), HistoryProvenance.EXPLICIT_BOUNDARY, "Fixture-only supported history", datetime(2024, 7, 1, tzinfo=timezone.utc))
	publication = DailyPublicationPolicy(timedelta(minutes=15), "Fixture-only publication")
	prices = {1: (100, 50), 2: (110, 40), 3: (121, 60), 5: (200, 30), 8: (210, 27), 9: (220, 54), 10: (110, 108), 11: (330, 54)}
	observations = []
	for day, closes in prices.items():
		session = calendar.session(date(2024, 7, day))
		for ticker_id, close in zip((17, 3), closes):
			observations.append(CloseObservation(ticker_id, float(close), ObservationTiming(session, session.closes_at, session.closes_at + timedelta(minutes=15)), f"fixture-revision-{day}-{ticker_id}"))
	repository = FixtureCloseRepository(observations)

	class Samples:
		specification = FeatureSpec((17, 3), 3)
		target = TargetSpec()

		def decision(self, day=5):
			entry = calendar.session(date(2024, 7, day))
			return DecisionTiming(calendar.shift(entry, -1), entry, calendar.shift(entry, 1), entry.closes_at - timedelta(minutes=30))

		def build(self, day=5, *, label_cutoff=None, specification=None, publication_override=None, availability_policy=None, data_snapshot=None):
			kwargs = {} if availability_policy is None else {"availability_policy": availability_policy, "data_snapshot": data_snapshot}
			return build_sample(specification or self.specification, self.target, self.decision(day), boundary, calendar, publication_override or publication, repository, label_cutoff=label_cutoff, **kwargs)

		def revise(self, day, ticker_id, close):
			key = calendar.session(date(2024, 7, day)), ticker_id
			repository.observations[key] = replace(repository.observations[key], close=close, revision=f"fixture-corrected-{day}-{ticker_id}")

	result = Samples()
	result.boundary, result.calendar, result.publication, result.repository = boundary, calendar, publication, repository
	return result


@pytest.fixture
def chronological_storage(harness, calendar):
	settings = replace(harness.settings, corrections=replace(harness.settings.corrections, recent_recheck=timedelta(days=30)))
	for day, close in ((1, "100.0000"), (2, "110.0000"), (3, "121.0000"), (5, "200.0000"), (8, "210.0000")):
		session = calendar.session(date(2024, 7, day))
		harness.provider.responses[session.session_date] = harness.response(close=close)
		harness.clock.value = session.closes_at + timedelta(minutes=16)
		result = harness.run(replace(settings, end=session.session_date))
		assert result.successful, (result.coverage, result.failures)
	harness.clock.value = datetime(2024, 7, 9, 19, tzinfo=timezone.utc)
	return harness
