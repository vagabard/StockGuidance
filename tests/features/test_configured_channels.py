from datetime import date, datetime, timedelta, timezone

import pytest

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.datasets.building import build_sample
from stockguidance.features.channels import FeatureChannelData, FeatureChannelObservation, FeatureChannelQuery, FeatureValueState
from stockguidance.features.construction import FeatureIssue, construct_configured_features
from stockguidance.features.history_window import WindowState, plan_feature_history
from stockguidance.features.specification import FeatureChannel, FeatureSpecV2, PriceDeltaZScore, PriceZScore, VolumeZScore
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.specification import TargetSpec


class EmptyCloseRepository:
	def read(self, query):
		raise AssertionError("volume-only configured features must not read close storage")


class ChannelFixture:
	def __init__(self, revision="channel-revision"):
		self.revision = revision

	def read(self, query: FeatureChannelQuery) -> FeatureChannelData:
		rows = []
		for session_index, session in enumerate(query.scored_sessions):
			for ticker_index, ticker_id in enumerate(query.specification.ticker_ids):
				for channel in query.specification.external_channels:
					value = float(session_index * 10 + ticker_index + len(rows) * 0.01)
					rows.append(FeatureChannelObservation(channel, session, ticker_id, value, revision=self.revision))
		return FeatureChannelData(query, tuple(rows), self.revision)


def _context():
	calendar = ExchangeCalendar("XNYS", date(2023, 12, 1), date(2025, 12, 31))
	source = SourceIdentity("fixture-provider", "grouped-daily", "us-stocks", "fixture", PriceAdjustment.ADJUSTED)
	boundary = HistoryBoundary(source, calendar.calendar_id, date(2024, 1, 2), HistoryProvenance.EXPLICIT_BOUNDARY, "fixture", datetime(2024, 1, 3, tzinfo=timezone.utc))
	publication = DailyPublicationPolicy(timedelta(minutes=15), "fixture publication")
	entry = calendar.session(date(2024, 5, 15))
	timing = DecisionTiming(calendar.shift(entry, -1), entry, calendar.shift(entry, 1), calendar.shift(entry, -1).closes_at + timedelta(minutes=30))
	return calendar, source, boundary, publication, timing


def test_configured_channels_preserve_shape_order_and_share_build_path():
	calendar, _source, boundary, publication, timing = _context()
	specification = FeatureSpecV2((17, 3), 3, (PriceZScore(3), VolumeZScore(3)))
	repository = ChannelFixture()
	sample = build_sample(specification, TargetSpec(), timing, boundary, calendar, publication, None, channel_repository=repository)

	assert sample.features.values is not None
	assert specification.shape == (3, 2, 2)
	assert len(sample.features.values) == specification.input_size
	assert tuple(session.sequence for session in sample.features.sessions) == tuple(range(timing.entry_session.sequence - 3, timing.entry_session.sequence))
	assert sample.features.values[:4] == pytest.approx((0.0, 0.01, 1.02, 1.03))
	assert sample.feature_cache_key


def test_dependency_planning_keeps_model_lookback_separate_from_rolling_window():
	calendar, _source, boundary, publication, timing = _context()
	price = FeatureSpecV2((1,), 3, (PriceZScore(20),))
	delta = FeatureSpecV2((1,), 3, (PriceDeltaZScore(60),))
	price_window = plan_feature_history(calendar, boundary, price, timing, publication)
	delta_window = plan_feature_history(calendar, boundary, delta, timing, publication)

	assert price.required_history_sessions == 22
	assert len(price_window.dependency_sessions) == 22
	assert delta.required_history_sessions == 63
	assert len(delta_window.dependency_sessions) == 63
	assert len(price_window.sessions) == len(delta_window.sessions) == 3


def test_serialized_rolling_channel_keys_round_trip_to_typed_channels():
	specification = FeatureSpecV2((17,), 3, ("price_zscore_20", "price_delta_zscore_60", "volume_zscore_20"))

	assert specification.channel_keys == ("price_zscore_20", "price_delta_zscore_60", "volume_zscore_20")
	assert specification.required_history_sessions == 63


@pytest.mark.parametrize("state", [FeatureValueState.ZERO_VARIANCE, FeatureValueState.MISSING, FeatureValueState.UNVERIFIED, FeatureValueState.NOT_YET_AVAILABLE, FeatureValueState.STALE, FeatureValueState.PENDING])
def test_unready_prepared_values_are_not_zero_filled(state):
	calendar, _source, boundary, publication, timing = _context()
	specification = FeatureSpecV2((17,), 3, (VolumeZScore(3),))
	window = plan_feature_history(calendar, boundary, specification, timing, publication)
	query = FeatureChannelQuery(boundary.source, specification, window.dependency_sessions, window.sessions, timing.decision_cutoff)
	data = FeatureChannelData(query, tuple(FeatureChannelObservation(channel, session, 17, None, state=state) for session in window.sessions for channel in specification.external_channels), "unready")

	features = construct_configured_features(specification, timing, window.sessions, None, data)
	assert features.values is None
	assert features.issues and isinstance(features.issues[0], FeatureIssue)
	assert features.issues[0].state.value == state.value


def test_channel_revision_changes_sample_identity():
	calendar, _source, boundary, publication, timing = _context()
	specification = FeatureSpecV2((17, 3), 3, (VolumeZScore(3),))
	first = build_sample(specification, TargetSpec(), timing, boundary, calendar, publication, None, channel_repository=ChannelFixture("revision-a"))
	second = build_sample(specification, TargetSpec(), timing, boundary, calendar, publication, None, channel_repository=ChannelFixture("revision-b"))

	assert first.feature_cache_key != second.feature_cache_key



@pytest.mark.parametrize("missing_selected", [False, True])
def test_prepared_zscore_reader_accepts_filtered_subset_and_requires_its_rows(tmp_path, missing_selected):
	from stockguidance.config.source import SourceConfig
	from stockguidance.database.repositories.features import MySQLFeatureChannelRepository, _channel_definition, FeatureChannelReadError
	from stockguidance.zscores.results import ZScoreStatus
	calendar, identity, _boundary, _publication, timing = _context()
	source = SourceConfig(identity, tmp_path / "data", tmp_path / "db.id")
	channel = PriceDeltaZScore(20)
	session = timing.previous_session
	spec = FeatureSpecV2((17, 3), 1, (channel,))
	query = FeatureChannelQuery(identity, spec, (session,), (session,), timing.decision_cutoff)
	definition, _ = _channel_definition(source, query, channel)
	series = (definition.series_key, definition.schema_version, definition.source_scope, definition.calendar_id, definition.family.value, definition.window, definition.calculation_version, definition.delta_definition, definition.precision, definition.missing_value_policy)
	rows = [(1, 3, 0.25, int(ZScoreStatus.READY)), (1, 17, -0.5, int(ZScoreStatus.READY))]
	if missing_selected:
		rows.pop()
	class Cursor:
		def __init__(self):
			# Global metadata covers 100 stocks; unrelated unavailable inputs must
			# not invalidate this selected pair's completed, ready values.
			self.responses = iter(([series], [("dep", "availability", 0, 100, 98, "digest")], rows))
		def execute(self, sql, parameters):
			pass
		def fetchall(self):
			return next(self.responses)
	repository = MySQLFeatureChannelRepository(source, None)
	if missing_selected:
		with pytest.raises(FeatureChannelReadError, match="incomplete"):
			repository._read_zscore(Cursor(), query, channel, {session.session_date: 1})
	else:
		observations, evidence = repository._read_zscore(Cursor(), query, channel, {session.session_date: 1})
		assert [row.ticker_id for row in observations] == [17, 3]
		assert [row.value for row in observations] == [-0.5, 0.25]
		assert evidence[0].state.value == "ready"
