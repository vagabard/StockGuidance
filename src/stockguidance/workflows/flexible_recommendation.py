"""Resolve command settings into the existing typed recommendation workflow."""

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from typing import Any

from stockguidance.artifacts.decoding import decode_features, decode_target
from stockguidance.calendar.clock import SystemClock,Clock
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.database.table_names import TableNames
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.database.raw_rebuild import RawPayloadProvider
from stockguidance.recommendations.options import RecommendationOptions
from stockguidance.workflows.recommendation_result import RecommendationRunResult
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.training import decode_training_configuration
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.repositories.endpoint_universe import EndpointUniverseRepository
from stockguidance.database.operation_recovery import consistent_read_transaction, DatabaseReadinessGuard, ReadScope
from stockguidance.database.simplified_runtime import source_from_database, run_simplified_update
from stockguidance.database.table_names import TableRole, quote_identifier
from stockguidance.market_data.identity import decode_source
from stockguidance.market_data.corrections import CorrectionPolicy
from stockguidance.market_data.providers.configured_polygon import ConfiguredPolygonProvider
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.portfolio.policy import AllocationPolicy
from stockguidance.recommendations.configuration import RefreshMode
from stockguidance.recommendations.eligibility import EligibilityPolicy
from stockguidance.recommendations.jobs import RecommendationConfiguration, RetrainingConfiguration
from stockguidance.recommendations.resolution import resolve_start, resolve_period
from stockguidance.universe.endpoints import EndpointSelection
from stockguidance.targets.specification import TargetSpec


@dataclass(frozen=True)
class RecommendationSetup:
	start: str
	end: str | None = None
	selection: EndpointSelection = EndpointSelection()
	template: dict[str,Any] | None = None
	dbinfo: Path = Path("dbinfo.id")
	data_root: Path = Path("data")
	runs_root: Path | None = None
	minimum_transactions: int | None = None
	automatic_artifact: bool = True


def load_setup(path: Path | None, **overrides: Any) -> RecommendationSetup:
	base = overrides.get("template",{}) if path is None else json.loads(path.read_text(encoding="utf-8"))
	allowed = {"source","data_root","database_config","features","start_date","end_date","entry_date","allocation","publication","corrections","eligibility","top_k","retraining","availability_policy","target","runs_root","model","training","trim_dollar_volume_percent","volume_sample_sessions","tickers"}
	if not isinstance(base,dict) or set(base)-allowed:
		raise ValueError("Unsupported recommendation configuration fields")
	start = overrides.get("start") or base.get("start_date")
	if not isinstance(start,str):
		raise ValueError("Provide --start-date or a configured start_date")
	features = base.get("features",{})
	if not isinstance(features,dict):
		raise ValueError("Features must be a mapping")
	tickers = overrides.get("tickers")
	if tickers is None:
		tickers = base.get("tickers")
	ids = None if tickers is not None else features.get("ticker_ids")
	p = overrides.get("trim")
	if p is None:
		p = base.get("trim_dollar_volume_percent",0)
	sample = overrides.get("sample")
	if sample is None:
		sample = base.get("volume_sample_sessions")
	selection = EndpointSelection(None if tickers is None else tuple(tickers),None if ids is None else tuple(ids),Decimal(str(p)),sample)
	minimum = overrides.get("minimum_transactions")
	if minimum is not None and (type(minimum) is not int or minimum<0):
		raise ValueError("Minimum transactions must be a nonnegative integer")
	return RecommendationSetup(start,overrides.get("end") if overrides.get("end") is not None else base.get("end_date"),selection,base,
		Path(overrides.get("dbinfo") or base.get("database_config","dbinfo.id")),Path(overrides.get("data_root") or base.get("data_root","data")),
		None if not(overrides.get("runs_root") or base.get("runs_root")) else Path(overrides.get("runs_root") or base["runs_root"]),minimum,overrides.get("automatic_artifact",True))


def prepare_raw_history(source: SourceConfig, connect: WriteConnectionFactory, calendar: SessionCalendar, sessions: tuple[TradingSession,...], *, refresh: bool, provider: RawPayloadProvider, clock: Clock, names: TableNames) -> None:
	"""Refresh only missing coverage, using the maintained coordinator in batches."""
	dates = tuple(s.session_date for s in sessions)
	table = quote_identifier(names.table(TableRole.SESSIONS))
	with consistent_read_transaction(source,connect) as transaction:
		cursor = transaction.cursor()
		cursor.execute(f"SELECT id,transaction_date,raw_complete FROM {table} WHERE transaction_date IN ({','.join('%s' for _ in dates)})",dates)
		rows = cursor.fetchall()
		by_date = {str(row[1]):row for row in rows}
		ids = tuple(int(str(row[0])) for row in rows)
		if ids and not DatabaseReadinessGuard(names).check(ReadScope(date_ids=ids),transaction).ready:
			raise ValueError("Resume the unfinished maintenance operation before recommendation preparation")
	missing = tuple(s for s in sessions if str(s.session_date) not in by_date or not by_date[str(s.session_date)][2])
	if missing and not refresh:
		raise ValueError("Required raw coverage is incomplete; use refresh mode first")
	for offset in range(0,len(missing),5):
		batch = missing[offset:offset+5]
		print(f"Refreshing {batch[0].session_date} through {batch[-1].session_date}",file=sys.stderr,flush=True)
		run_simplified_update(source,connect,calendar,batch,provider=provider,windows=(),clock=clock,table_names=names)


def resolve_setup(setup: RecommendationSetup, options: RecommendationOptions, *, connect: WriteConnectionFactory | None=None, clock: Clock | None=None, provider: RawPayloadProvider | None=None) -> tuple[RecommendationConfiguration,RecommendationOptions]:
	connect = connect or MySQLConnectionFactory()
	clock = clock or SystemClock()
	base = setup.template or {}
	source,names = source_from_database(setup.dbinfo,setup.data_root,connect)
	if base.get("source") is not None and decode_source(base["source"]) != source.identity:
		raise ValueError("Configured source differs from the database-owned source")
	if base.get("availability_policy","strict")!="strict":
		raise ValueError("Flexible recommendations use current validated history with market-session cutoffs")
	publication = base.get("publication",{})
	delay = timedelta(minutes=publication.get("delay_minutes",0))
	if delay<timedelta(0):
		raise ValueError("Publication delay must be nonnegative")
	publication_policy = DailyPublicationPolicy(delay,publication.get("evidence","Current validated historical data; market-session chronology"))
	training = base.get("retraining") or {}
	configuration = decode_training_configuration(training.get("training",base.get("training",{})),training.get("model",base.get("model",{})))
	lead = timedelta(minutes=training.get("decision_minutes_before_close",30))
	custom_features = base.get("features",{})
	lookback = custom_features.get("lookback_sessions",20)
	if type(lookback) is not int or lookback<1:
		raise ValueError("Feature lookback must be a positive integer")
	now = clock.now()
	requested_end = date.fromisoformat(setup.end) if setup.end else now.astimezone(ZoneInfo("America/New_York")).date()
	requested_start = resolve_start(setup.start,requested_end)
	calendar = ExchangeCalendar(source.calendar_id,requested_start-timedelta(days=max(200,(lookback+2)*3)),max(requested_end,now.date())+timedelta(days=30))
	period = resolve_period(setup.start,setup.end,calendar,now,publication_delay=delay,decision_lead=lead)
	context = calendar.sessions(calendar.shift(period.sessions[0],-lookback).session_date,period.sessions[-1].session_date)
	prepare_raw_history(source,connect,calendar,context,refresh=options.refresh_mode is RefreshMode.REFRESH,
		provider=provider or ConfiguredPolygonProvider(options.max_retries),clock=clock,names=names)
	selection = EndpointUniverseRepository(source,connect,names).select(period.sessions,setup.selection)
	print(f"Full-period coverage: {selection['coverage_seconds']:.3f}s; excluded {len(selection['coverage_excluded_ids'])} tickers; "
		f"dollar-volume filtering: {selection['filter_seconds']:.3f}s; total selection: {selection['selection_seconds']:.3f}s; "
		f"{selection['selected_count']}/{selection['candidate_count']} tickers; cache_hit={selection['cache_hit']}",file=sys.stderr,flush=True)
	fields = {"ticker_ids":selection["ticker_ids"],"lookback_sessions":lookback,"signal_mode":"previous_close","interval":source.identity.interval.value,
		"missing_history":"unavailable","feature_order":["close_over_first_window_close_minus_one"],"adjustment":source.identity.adjustment.value}
	fields.update(custom_features)
	fields["ticker_ids"] = selection["ticker_ids"]
	if "channels" in fields:
		fields.pop("feature_order",None)
		fields.setdefault("schema","configured-features-v2")
		fields.setdefault("layout","session_oldest_first,ticker,feature:C")
	features = decode_features(fields)
	minimum = setup.minimum_transactions
	if minimum is None:
		minimum = base.get("eligibility",{}).get("minimum_transactions",0)
	allocation = base.get("allocation",{})
	anchor = date.fromisoformat(allocation["anchor_date"]) if allocation.get("anchor_date") else period.entry.session_date
	corrections = base.get("corrections",{})
	correction_policy = CorrectionPolicy(timedelta(days=corrections.get("recent_window_days",30)),timedelta(hours=corrections.get("recent_refresh_hours",24)),timedelta(days=corrections.get("archive_refresh_days",30)),corrections.get("evidence","Explicit correction reimport; refresh missing completed sessions"))
	plan = None
	if options.policy is ModelRefreshPolicy.RETRAIN:
		if len(period.sessions)<2:
			raise ValueError("At least two completed sessions are required for mature-outcome training")
		plan = RetrainingConfiguration(period.sessions[0].session_date,period.sessions[-2].session_date,lead,configuration)
	resolution = {**period.to_mapping(),"universe":selection,"automatic_artifact":setup.automatic_artifact}
	resolved = RecommendationConfiguration(source,features,context[0].session_date,period.sessions[-1].session_date,period.entry.session_date,
		AllocationPolicy(allocation.get("policy","alternate_sessions")),anchor,publication_policy,correction_policy,EligibilityPolicy(minimum),base.get("top_k",5),plan,
		target=decode_target(base["target"]) if "target" in base else TargetSpec(),
		runs_root=setup.runs_root,replay_cutoff=period.decision_cutoff if period.replay else None,resolution_metadata=resolution)
	# Source preparation uses actual operational time; replay scoring uses a
	# separate clock and a read-only coverage path after preparation.
	if period.replay:
		options = replace(options,refresh_mode=RefreshMode.VALIDATE_ONLY)
	return resolved,options


def assemble_flexible_recommendation(setup: RecommendationSetup, options: RecommendationOptions) -> RecommendationRunResult:
	from stockguidance.workflows.recommendation_runtime import assemble_and_recommend
	configuration,options = resolve_setup(setup,options)
	return assemble_and_recommend(configuration,options)
