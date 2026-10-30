from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli import preview_date_ranges
from stockguidance.calendar.ranges import (
	AbsoluteDate,
	AnchorDuration,
	CalendarOffsetUnit,
	DateAnchor,
	DatePlanSpec,
	DateRangeResolver,
	DependencySpec,
	DurationUnit,
	ExplicitRange,
	RangeChronologyError,
	RangeConfigurationError,
	RangeOutOfCoverageError,
	RangeUnknownCoverageError,
	RelativeDate,
	RerunResolutionMode,
	SourceCoverageSnapshot,
	decode_resolved_plan,
	persist_resolution,
	resolve_rerun,
)
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.runs import RunConfig, RunDirectoryService, RunOperation


class FrozenClock:
	def __init__(self, value: datetime) -> None:
		self.value = value

	def now(self) -> datetime:
		return self.value


@pytest.fixture
def range_context():
	calendar = ExchangeCalendar("XNYS", date(2023, 12, 1), date(2026, 12, 31))
	source = SourceIdentity("fixture-provider", "fixture-endpoint", "us-stocks", "fixture", PriceAdjustment.ADJUSTED)
	verified = tuple(item.session_date for item in calendar.sessions(calendar.start, calendar.end))
	coverage = SourceCoverageSnapshot.from_verified_dates(source, calendar.calendar_id, "revision-a", verified, snapshot_id="snapshot-a")
	clock = FrozenClock(datetime(2026, 12, 31, 23, tzinfo=timezone.utc))
	return calendar, source, coverage, clock


def test_non_sessions_snap_forward_and_backward_and_holidays_are_not_calendar_days(range_context):
	calendar, source, coverage, clock = range_context
	resolved = DateRangeResolver(calendar, coverage, source, clock).resolve(
		ExplicitRange(AbsoluteDate(date(2024, 7, 4)), AbsoluteDate(date(2024, 7, 6)))
	)
	assert resolved.start == date(2024, 7, 5)
	assert resolved.end == date(2024, 7, 5)
	assert len(resolved.sessions) == 1


@pytest.mark.parametrize(
	"duration, expected",
	[
		(AnchorDuration(AbsoluteDate(date(2024, 7, 3)), 3, DurationUnit.EXCHANGE_SESSIONS), (date(2024, 7, 3), date(2024, 7, 8))),
		(AnchorDuration(AbsoluteDate(date(2024, 7, 8)), -3, DurationUnit.EXCHANGE_SESSIONS), (date(2024, 7, 3), date(2024, 7, 8))),
		(AnchorDuration(AbsoluteDate(date(2024, 7, 3)), 3, DurationUnit.CALENDAR_DAYS), (date(2024, 7, 3), date(2024, 7, 5))),
		(AnchorDuration(AbsoluteDate(date(2024, 7, 5)), -3, DurationUnit.CALENDAR_DAYS), (date(2024, 7, 2), date(2024, 7, 5))),
	],
)
def test_signed_durations_use_calendar_or_exchange_session_units(range_context, duration, expected):
	calendar, source, coverage, clock = range_context
	resolved = DateRangeResolver(calendar, coverage, source, clock).resolve(duration)
	assert (resolved.start, resolved.end) == expected
	if duration.duration_unit is DurationUnit.EXCHANGE_SESSIONS:
		assert len(resolved.sessions) == abs(duration.duration_value)


def test_calendar_years_clamp_february_29_and_anchor_evidence_preserves_original(range_context):
	calendar = ExchangeCalendar("XNYS", date(2019, 1, 1), date(2022, 12, 31))
	source = range_context[1]
	verified = tuple(item.session_date for item in calendar.sessions(calendar.start, calendar.end))
	coverage = SourceCoverageSnapshot.from_verified_dates(source, calendar.calendar_id, "leap", verified)
	clock = FrozenClock(datetime(2020, 2, 29, 23, tzinfo=timezone.utc))
	resolved = DateRangeResolver(calendar, coverage, source, clock).resolve(
		ExplicitRange(AbsoluteDate(date(2020, 2, 28)), RelativeDate(DateAnchor.TODAY, 1, CalendarOffsetUnit.CALENDAR_YEARS))
	)
	assert resolved.anchors[1].resolved_date == date(2021, 2, 28)
	assert resolved.end == date(2021, 2, 26)
	assert resolved.original.to_mapping()["end"] == {"anchor": "today", "offset": {"value": 1, "unit": "calendar_years"}}


def test_first_and_last_data_anchors_use_verified_coverage_not_database_ids(range_context):
	calendar = ExchangeCalendar("XNYS", date(2010, 1, 1), date(2035, 12, 31))
	source = range_context[1]
	verified = tuple(item.session_date for item in calendar.sessions(calendar.start, calendar.end))
	coverage = SourceCoverageSnapshot.from_verified_dates(source, calendar.calendar_id, "bounded", verified)
	clock = FrozenClock(datetime(2035, 12, 31, 23, tzinfo=timezone.utc))
	resolved = DateRangeResolver(calendar, coverage, source, clock).resolve(
		ExplicitRange(
			RelativeDate(DateAnchor.FIRST_DATA, 3, CalendarOffsetUnit.CALENDAR_YEARS),
			RelativeDate(DateAnchor.LAST_DATA, -10, CalendarOffsetUnit.CALENDAR_YEARS),
		)
	)
	assert resolved.start >= date(2013, 1, 1)
	assert resolved.end <= date(2025, 12, 31)
	ago = DateRangeResolver(calendar, coverage, source, clock).resolve(
		ExplicitRange(
			RelativeDate(DateAnchor.LAST_DATA, -5, CalendarOffsetUnit.CALENDAR_YEARS),
			RelativeDate(DateAnchor.LAST_DATA, -2, CalendarOffsetUnit.CALENDAR_YEARS),
		)
	)
	assert ago.start >= date(2030, 1, 1) and ago.end <= date(2033, 12, 31)
	forward = DateRangeResolver(calendar, coverage, source, clock).resolve(
		AnchorDuration(RelativeDate(DateAnchor.FIRST_DATA, 10, CalendarOffsetUnit.CALENDAR_YEARS), 5, DurationUnit.CALENDAR_YEARS)
	)
	assert forward.start >= date(2020, 1, 1) and forward.end <= date(2025, 12, 31)
	negative_end = DateRangeResolver(calendar, coverage, source, clock).resolve(
		AnchorDuration(AbsoluteDate(date(2025, 7, 10)), -5, DurationUnit.CALENDAR_DAYS)
	)
	assert negative_end.end == date(2025, 7, 10) and negative_end.start == date(2025, 7, 7)


def test_dependency_plan_keeps_lookback_zscore_gap_horizon_and_labels_distinct(range_context):
	calendar, source, coverage, clock = range_context
	specification = DatePlanSpec(
		ExplicitRange(AbsoluteDate(date(2024, 7, 8)), AbsoluteDate(date(2024, 7, 10))),
		dependencies=DependencySpec(model_lookback_sessions=2, zscore_window_sessions=20, price_delta_predecessor_sessions=1, target_baseline_sessions=3, training_end_gap_sessions=1, simulation_horizon_sessions=3),
	)
	plan = DateRangeResolver(calendar, coverage, source, clock).resolve_plan(specification)
	assert plan.specification.dependencies.feature_warmup_sessions == 22
	assert len(plan.feature_warmup_sessions) == 22
	assert len(plan.target_baseline_sessions) == 3
	assert tuple(item.session_date for item in plan.training_label_exit_sessions) == (date(2024, 7, 9), date(2024, 7, 10), date(2024, 7, 11))
	assert plan.training_information_cutoff.session_date == date(2024, 7, 11)
	assert tuple(item.session_date for item in plan.simulation_sessions) == (date(2024, 7, 15), date(2024, 7, 16), date(2024, 7, 17))
	assert tuple(item.session_date for item in plan.evaluation_label_exit_sessions) == (date(2024, 7, 16), date(2024, 7, 17))


def test_interior_unknown_and_known_absent_coverage_are_not_silently_skipped(range_context):
	calendar, source, _, clock = range_context
	all_days = tuple(item.session_date for item in calendar.sessions(date(2024, 7, 1), date(2024, 7, 10)))
	unknown = SourceCoverageSnapshot.from_verified_dates(source, calendar.calendar_id, "unknown", tuple(day for day in all_days if day != date(2024, 7, 2)))
	with pytest.raises(RangeUnknownCoverageError, match="2024-07-02"):
		DateRangeResolver(calendar, unknown, source, clock).resolve(ExplicitRange(AbsoluteDate(date(2024, 7, 1)), AbsoluteDate(date(2024, 7, 3))))
	absent = SourceCoverageSnapshot.from_verified_dates(source, calendar.calendar_id, "absent", tuple(day for day in all_days if day != date(2024, 7, 2)), absent_sessions=(date(2024, 7, 2),))
	with pytest.raises(RangeOutOfCoverageError, match="known absent"):
		DateRangeResolver(calendar, absent, source, clock).resolve(ExplicitRange(AbsoluteDate(date(2024, 7, 1)), AbsoluteDate(date(2024, 7, 3))))
	incomplete = SourceCoverageSnapshot.from_verified_dates(source, calendar.calendar_id, "incomplete", tuple(day for day in all_days if day != date(2024, 7, 2)), incomplete_sessions=(date(2024, 7, 2),))
	with pytest.raises(RangeUnknownCoverageError, match="incomplete"):
		DateRangeResolver(calendar, incomplete, source, clock).resolve(ExplicitRange(AbsoluteDate(date(2024, 7, 1)), AbsoluteDate(date(2024, 7, 3))))


def test_reversed_empty_zero_and_out_of_calendar_ranges_fail(range_context):
	calendar, source, coverage, clock = range_context
	resolver = DateRangeResolver(calendar, coverage, source, clock)
	with pytest.raises(RangeChronologyError, match="reversed"):
		resolver.resolve(ExplicitRange(AbsoluteDate(date(2024, 7, 10)), AbsoluteDate(date(2024, 7, 1))))
	with pytest.raises(RangeConfigurationError, match="nonzero"):
		resolver.resolve(AnchorDuration(AbsoluteDate(date(2024, 7, 8)), 0, DurationUnit.EXCHANGE_SESSIONS))
	with pytest.raises(RangeOutOfCoverageError, match="outside"):
		resolver.resolve(ExplicitRange(AbsoluteDate(date(2022, 1, 1)), AbsoluteDate(date(2022, 1, 5))))


def test_pinned_rerun_does_not_re_resolve_but_explicit_mode_does(range_context):
	calendar, source, coverage, clock = range_context
	first = DateRangeResolver(calendar, coverage, source, clock).resolve(ExplicitRange(RelativeDate(DateAnchor.LAST_DATA, -2), RelativeDate(DateAnchor.LAST_DATA, 0)))
	new_coverage = SourceCoverageSnapshot.from_verified_dates(source, calendar.calendar_id, "revision-b", tuple(item.session_date for item in calendar.sessions(date(2024, 1, 2), date(2026, 12, 31))), snapshot_id="snapshot-b")
	new = DateRangeResolver(calendar, new_coverage, source, clock)
	pinned = resolve_rerun(first, new, RerunResolutionMode.PINNED)
	re_resolved = resolve_rerun(first, new, RerunResolutionMode.RERESOLVE)
	assert pinned is first
	assert re_resolved.source_revision == "revision-b"
	assert re_resolved.source_snapshot_id == "snapshot-b"


def test_saved_plan_round_trip_preserves_typed_original_expressions(range_context):
	calendar, source, coverage, clock = range_context
	specification = DatePlanSpec(ExplicitRange(RelativeDate(DateAnchor.FIRST_DATA, 20, CalendarOffsetUnit.CALENDAR_DAYS), RelativeDate(DateAnchor.FIRST_DATA, 25, CalendarOffsetUnit.CALENDAR_DAYS)))
	plan = DateRangeResolver(calendar, coverage, source, clock).resolve_plan(specification)
	assert decode_resolved_plan(plan.to_mapping()) == specification


def test_resolution_is_persisted_through_stage1_resolution_section(tmp_path: Path, range_context):
	calendar, source, coverage, clock = range_context
	resolved = DateRangeResolver(calendar, coverage, source, clock).resolve(ExplicitRange(AbsoluteDate(date(2024, 7, 8)), AbsoluteDate(date(2024, 7, 9))))
	service = RunDirectoryService(tmp_path / "runs")
	handle = service.create(RunConfig(RunOperation.TRAINING, {"date_range": resolved.original.to_mapping()}), provenance={})
	persist_resolution(service, handle.run_id, resolved)
	assert service.read(handle.run_id).resolution["source_revision"] == "revision-a"


def test_preview_cli_is_read_only_and_reports_dependencies_without_a_model(tmp_path: Path, range_context, capsys):
	calendar, source, coverage, _ = range_context
	config = {
		"source": {"provider": source.provider, "endpoint": source.endpoint, "market": source.market, "universe": source.universe, "adjustment": source.adjustment.value, "interval": source.interval.value},
		"calendar": {"name": "XNYS", "start": calendar.start.isoformat(), "end": calendar.end.isoformat()},
		"clock_now": "2026-12-31T23:00:00+00:00",
		"coverage": {"revision": coverage.revision, "snapshot_id": coverage.snapshot_id, "verified_sessions": [item.isoformat() for item in coverage.verified_sessions]},
		"date_range": {"start": "2024-07-08", "end": "2024-07-09"},
		"dependencies": {"model_lookback_sessions": 1, "simulation_horizon_sessions": 2},
	}
	path = tmp_path / "preview.json"
	path.write_text(json.dumps(config), encoding="utf-8")
	assert preview_date_ranges.main(["--config", str(path)]) == 0
	payload = json.loads(capsys.readouterr().out)
	assert payload["successful"] is True
	assert payload["resolution"]["evaluation"]["effective_sessions"][0]["date"] == "2024-07-11"
	assert not tuple(tmp_path.glob("*.sgmodel"))

