from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.cli.recommend import parse_args
from stockguidance.recommendations.resolution import resolve_start,resolve_period
from stockguidance.universe.endpoints import EndpointSelection,evenly_spaced,trim_candidates
from stockguidance.workflows.flexible_recommendation import load_setup


@pytest.fixture(scope="module")
def exchange():
	return ExchangeCalendar("XNYS",date(2023,1,1),date(2027,1,31))


def test_relative_start_is_anchored_to_end_and_leap_day_clamps():
	assert resolve_start("-1y",date(2024,2,29))==date(2023,2,28)
	assert resolve_start("-30d",date(2026,11,25))==date(2026,10,26)
	assert resolve_start("2025-10-01",date(2026,11,25))==date(2025,10,1)


def test_default_today_and_after_close_holiday_resolution(exchange):
	before = resolve_period("-1y",None,exchange,datetime(2026,11,25,15,tzinfo=timezone.utc))
	assert before.requested_start==date(2025,11,25)
	assert before.sessions[-1].session_date==date(2026,11,24)
	assert before.entry.session_date==date(2026,11,25) and not before.replay
	after = resolve_period("-1y",None,exchange,datetime(2026,11,25,22,tzinfo=timezone.utc))
	assert after.entry.session_date==date(2026,11,27) and not after.replay
	weekend = resolve_period("-30d",None,exchange,datetime(2026,11,29,15,tzinfo=timezone.utc))
	assert weekend.sessions[-1].session_date==date(2026,11,27)
	assert weekend.entry.session_date==date(2026,11,30)


def test_historical_end_replays_next_session_without_using_later_data(exchange):
	result=resolve_period("-1y","2026-10-03",exchange,datetime(2026,11,25,15,tzinfo=timezone.utc))
	assert result.requested_start==date(2025,10,3)
	assert result.sessions[0].session_date>=result.requested_start
	assert result.sessions[-1].session_date==date(2026,10,2)
	assert result.entry.session_date==date(2026,10,5) and result.replay
	assert result.decision_cutoff==result.entry.closes_at-timedelta(minutes=30)
	assert result.to_mapping()["history_policy"]=="current_revised_dataset"


def test_invalid_and_future_ranges_fail(exchange):
	for start,end in (("2026-12-01","2026-12-10"),("2026-11-24","2026-10-01"),("bad","2026-10-01")):
		with pytest.raises(ValueError):
			resolve_period(start,end,exchange,datetime(2026,11,25,15,tzinfo=timezone.utc))


@pytest.mark.parametrize("args",[["--start-date","-1y"],["--start-date=-1y"]])
def test_cli_accepts_negative_year_as_separate_or_equals_value(args):
	parsed = parse_args(args)
	assert parsed.start_date=="-1y" and parsed.policy=="retrain" and parsed.artifact is None


def test_percentile_uses_floor_stable_ties_and_does_not_invent_missing_measurements():
	ids=tuple(range(1,21))
	measurements={v:(Decimal(10),3) for v in ids}
	kept,removed,missing=trim_candidates(ids,measurements,Decimal(5))
	assert removed==(1,) and kept==ids[1:] and not missing
	assert trim_candidates(ids,{},Decimal(0))==(ids,(),())
	assert trim_candidates((1,2,3),{1:(Decimal(1),2),2:(Decimal(2),2)},Decimal(50))==((2,),(1,),(3,))
	assert trim_candidates((1,2),measurements,Decimal(5))[1]==()


@pytest.mark.parametrize("percentage",["-1","100","NaN","Infinity"])
def test_invalid_percentages_fail(percentage):
	with pytest.raises(ValueError):
		EndpointSelection(trim_percent=Decimal(percentage))


def test_sampling_is_explicit_deterministic_and_includes_endpoints():
	assert evenly_spaced(tuple(range(251)),40)==evenly_spaced(tuple(range(251)),40)
	assert len(evenly_spaced(tuple(range(251)),40))==40
	assert evenly_spaced(tuple(range(251)),40)[::39]==(0,250)
	assert evenly_spaced((1,2),40)==(1,2)
	with pytest.raises(ValueError):
		EndpointSelection(sample_sessions=1)


def test_explicit_lists_preserve_order_and_reject_trimming():
	settings=EndpointSelection(tickers=("MSFT","AAPL"))
	assert settings.tickers==("MSFT","AAPL")
	with pytest.raises(ValueError,match="Explicit ticker"):
		EndpointSelection(tickers=("AAPL",),trim_percent=Decimal(5))
	assert load_setup(None,start="-1y").selection.trim_percent==0


def test_publication_gap_does_not_silently_turn_default_live_run_into_replay(exchange):
	with pytest.raises(ValueError,match="not published"):
		resolve_period("-1y",None,exchange,datetime(2026,11,25,21,5,tzinfo=timezone.utc),publication_delay=timedelta(minutes=15))


def test_current_mode_reports_universe_mismatch_instead_of_reshaping_model(recommender,job_json,tmp_path):
	import json
	from dataclasses import replace
	from stockguidance.artifacts.loading import load_artifact
	from stockguidance.recommendations.jobs import load_configuration
	from stockguidance.workflows.recommendation_runtime import _artifact_owned_specification
	from stockguidance.workflows.failures import StageExecutionError
	path=tmp_path/"explicit.json"
	path.write_text(json.dumps(job_json),encoding="utf-8")
	configuration=load_configuration(path)
	artifact=load_artifact(recommender.job.current_artifact)
	ids=artifact.metadata.semantics.features.ticker_ids
	configuration=replace(configuration,features=replace(artifact.metadata.semantics.features,ticker_ids=ids+(max(ids)+1,)),resolution_metadata={"history_policy":"current_revised_dataset"})
	with pytest.raises(StageExecutionError,match="retrain for the new selection"):
		_artifact_owned_specification(configuration,artifact,current=True)


def test_flexible_resolution_preserves_requested_zscore_input_and_target(tmp_path, monkeypatch):
	from stockguidance.config.source import SourceConfig, SourceIdentity, PriceAdjustment
	from stockguidance.database.table_names import load_table_names
	from stockguidance.models.policy import ModelRefreshPolicy
	from stockguidance.recommendations.options import RecommendationOptions
	from stockguidance.targets.specification import TargetSpecV2, TargetKind
	from stockguidance.features.specification import FeatureSpecV2
	from stockguidance.workflows import flexible_recommendation as module
	source = SourceConfig(SourceIdentity("fixture", "daily", "us", "all", PriceAdjustment.UNADJUSTED), tmp_path / "data", tmp_path / "db.id")
	monkeypatch.setattr(module, "source_from_database", lambda *args: (source, load_table_names()))
	monkeypatch.setattr(module, "prepare_raw_history", lambda *args, **kwargs: None)
	selection = {"ticker_ids": [1, 2], "ticker_labels": ["AAA", "BBB"], "coverage_seconds": 0, "coverage_excluded_ids": [], "filter_seconds": 0, "selection_seconds": 0, "selected_count": 2, "candidate_count": 2, "cache_hit": False}
	monkeypatch.setattr(module.EndpointUniverseRepository, "select", lambda *args: selection)
	target = TargetSpecV2(kind=TargetKind.RETURN_ZSCORE_PER_TICKER, rolling_window_sessions=20)
	setup = load_setup(None, start="-1y", template={"features": {"channels": ["price_delta_zscore_20"]}, "target": target.to_mapping()})
	clock = type("Clock", (), {"now": lambda self: datetime(2026, 11, 25, 15, tzinfo=timezone.utc)})()
	configuration, _ = module.resolve_setup(setup, RecommendationOptions(ModelRefreshPolicy.RETRAIN, tmp_path / "model.sgmodel"), connect=object(), clock=clock)
	assert configuration.target == target
	assert isinstance(configuration.features, FeatureSpecV2)
	assert configuration.features.channel_keys == ("price_delta_zscore_20",)
	assert configuration.target.raw_formula == "close(T+1) / close(T) - 1"
