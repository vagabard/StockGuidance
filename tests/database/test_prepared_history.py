from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from support.ingestion import FakeDatabase
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.config.source import SourceConfig, SourceIdentity, PriceAdjustment
from stockguidance.database.prepared_history import load_prepared_history, PreparationRequired
from stockguidance.database.simplified_schema import initialize_simplified_schema
from stockguidance.features.specification import FeatureSpec, FeatureSpecV2, PriceZScore
from stockguidance.targets.specification import TargetSpec, TargetSpecV2, TargetKind
from stockguidance.preprocessing import PreprocessingSpec, fit_training_preprocessing, fit_preprocessing
from stockguidance.universe.endpoints import EndpointSelection


@pytest.fixture
def prepared_store(tmp_path):
	source = SourceConfig(SourceIdentity("fixture", "daily", "us", "all", PriceAdjustment.UNADJUSTED), tmp_path/"data", tmp_path/"db.id")
	db = FakeDatabase(tmp_path/"prepared.sqlite")
	initialize_simplified_schema(source, db, windows=(2,))
	calendar = ExchangeCalendar("XNYS", date(2024,1,1), date(2024,12,31))
	sessions = calendar.sessions(date(2024,7,1), date(2024,7,12))
	for s in sessions:
		db.sql("INSERT INTO transaction_dates (id,transaction_date,raw_complete,raw_revision) VALUES (?,?,1,?)", (s.sequence,str(s.session_date),"a"*64))
	for t in (3,17,29):
		db.sql("INSERT INTO ticker_symbol (id,label) VALUES (?,?)",(t,f"T{t}"))
		for i,s in enumerate(sessions):
			db.sql("INSERT INTO raw_stock_data (ticker_id,date_id,open,close,volume,transactions,state_id) VALUES (?,?,?,?,?,?,1)",(t,s.sequence,10+t+i,10+t+i*i,100+t,20+t))
	decisions = tuple(DecisionTiming(calendar.shift(s,-1),s,calendar.shift(s,1),s.closes_at-timedelta(minutes=30)) for s in sessions[3:6])
	features = FeatureSpec((17,3,29),2,adjustment=PriceAdjustment.UNADJUSTED)
	db.statements.clear()
	return source,db,calendar,sessions,decisions,features


def load(store, **kwargs):
	source,db,calendar,sessions,decisions,features = store
	return load_prepared_history(source,db,calendar,decisions,features,TargetSpec(),**kwargs)


def test_select_then_stream_is_read_only_and_batches_do_not_change_values(prepared_store):
	a = load(prepared_store,batch_size=1,fetch_size=2)
	b = load(prepared_store,batch_size=1000)
	assert a.features.ticker_ids == (17,3,29)
	assert getattr(a,"revision") == getattr(b,"revision")
	np.testing.assert_array_equal(a.fields["close"],b.fields["close"])
	assert not a.fields["close"].flags.writeable
	assert a.timings["array_bytes"] == sum(x.nbytes for x in a.fields.values()) + a.quality.nbytes
	assert all(sql.lstrip().split()[0] in {"SELECT","START"} for sql,_ in prepared_store[1].statements)
	assert not any("observed_at" in sql or "available_at" in sql or "recorded_at" in sql or "completed_at" in sql for sql,_ in prepared_store[1].statements)
	assert len([q for q in a.timings["queries"] if "load batch" in q["name"]]) == 3
	assert len([q for q in b.timings["queries"] if "load batch" in q["name"]]) == 1


def test_gaps_in_warmup_simulation_or_labels_exclude_ticker_and_repairs_count(prepared_store):
	source,db,calendar,sessions,decisions,features = prepared_store
	db.sql("DELETE FROM raw_stock_data WHERE ticker_id=29 AND date_id=?",(sessions[1].sequence,))
	db.sql("UPDATE raw_stock_data SET state_id=4 WHERE ticker_id=3 AND date_id=?",(sessions[2].sequence,))
	history = load(prepared_store)
	assert history.features.ticker_ids == (17,3)
	assert history.selection["exclusions"]["close"] == [29]
	assert history.quality.any()
	assert history.sample(decisions[0]).features.quality


def test_missing_global_preparation_fails_without_fetching_provider(prepared_store):
	prepared_store[1].sql("UPDATE transaction_dates SET raw_complete=0")
	with pytest.raises(PreparationRequired,match="update_market_data"):
		load(prepared_store)


def test_windows_and_targets_match_manual_layout_and_use_no_later_features(prepared_store):
	history = load(prepared_store)
	decisions = prepared_store[4]
	for d in decisions:
		end = history.position(d.previous_session)+1
		closes = history.fields["close"][end-2:end]
		np.testing.assert_allclose(history.feature_array(d), (closes/closes[0]-1).reshape(-1))
		raw,transformed = history.target_arrays(d)
		np.testing.assert_allclose(raw,history.fields["close"][end+1]/history.fields["close"][end]-1)
		np.testing.assert_array_equal(raw,transformed)
	assert history.dataset(decisions,decisions[-1].decision_cutoff).excluded[-1].target_state.value == "not_yet_available"


@pytest.mark.parametrize("method",["none","standard","min_max"])
def test_streamed_preprocessing_matches_materialized_reference(prepared_store,method):
	history = load(prepared_store)
	dataset = history.dataset(prepared_store[4],history.sessions[-1].closes_at)
	x,y = zip(*(dataset.case_arrays(i) for i in range(len(dataset.eligible))))
	spec = PreprocessingSpec(method,method)
	actual = fit_training_preprocessing(dataset,spec)
	expected = fit_preprocessing(spec,np.asarray(x),np.asarray(y),feature_specification=history.features,target_specification=history.target,data_revision=dataset.revision)
	np.testing.assert_allclose(actual.input.location,expected.input.location,atol=1e-14)
	np.testing.assert_allclose(actual.input.scale,expected.input.scale,atol=1e-14)
	np.testing.assert_allclose(actual.output.scale,expected.output.scale,atol=1e-14)


def test_budget_fails_before_allocation(prepared_store):
	with pytest.raises(MemoryError,match="budget"):
		load(prepared_store,max_array_bytes=1)


def test_array_dataset_trains_exports_and_reloads(prepared_store,tmp_path):
	from stockguidance.training.configuration import TrainingConfig, DeviceChoice
	from stockguidance.workflows.train_model import HistoryTrainingRequest, train_from_history
	from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
	from stockguidance.market_data.publication import DailyPublicationPolicy
	from stockguidance.market_data.availability import AvailabilityPolicy
	from stockguidance.artifacts.loading import load_artifact
	history = load(prepared_store)
	cutoff = history.sessions[-1].closes_at
	boundary = HistoryBoundary(history.source,history.calendar.calendar_id,history.sessions[0].session_date,HistoryProvenance.EXPLICIT_BOUNDARY,"fixture",cutoff)
	request = HistoryTrainingRequest(prepared_store[4],cutoff,history.features,history.target,TrainingConfig(epochs=1,device=DeviceChoice.CPU),tmp_path/"model.sgmodel",availability_policy=AvailabilityPolicy.CURRENT_PREPARED)
	result = train_from_history(request,boundary,history.calendar,DailyPublicationPolicy(timedelta(0),"fixture"),history)
	assert result.training_samples == 3
	assert load_artifact(result.artifact).metadata.semantics.availability_policy is AvailabilityPolicy.CURRENT_PREPARED
	assert (tmp_path/"model.data.json").is_file()


def seed_zscores(store):
	from stockguidance.market_data.identity import source_key
	from stockguidance.zscores.storage import ZScoreSeriesDefinition
	from stockguidance.zscores.specification import ZScoreFamily, CALCULATION_VERSION, FLOAT32_PRECISION
	from stockguidance.database.table_names import load_table_names
	source,db,calendar,sessions,_,_ = store
	definition = ZScoreSeriesDefinition(source_key(source.identity),calendar.calendar_id,ZScoreFamily.PRICE,2,CALCULATION_VERSION,FLOAT32_PRECISION,"not_applicable")
	values = definition.as_payload()
	columns = tuple(values)
	db.sql(f"INSERT INTO stockguidance_zscore_series (series_key,{','.join('`'+c+'`' for c in columns)},created_at) VALUES ({','.join('?' for _ in range(len(columns)+2))})",(definition.series_key,*values.values(),"2026-01-01"))
	table = load_table_names().resolve_zscore_tables((2,))[2].price
	for i,s in enumerate(sessions):
		# Late preparation and actual receipt availability are deliberately irrelevant.
		db.sql("INSERT INTO stockguidance_zscore_sessions (series_key,date_id,dependency_digest,availability_digest,actual_input_available,completed_at,row_count,ready_count,status_counts,content_digest) VALUES (?,?,'x','x',0,'2026-01-01',3,3,'{}','x')",(definition.series_key,s.sequence))
		for t in (3,17,29):
			db.sql(f"INSERT INTO {table} (date_id,ticker_id,zscore,status,quality_flags,gap_length,provenance) VALUES (?,?,?,0,0,0,'[]')",(s.sequence,t,float(i+t)))
	return table


def test_selected_channels_require_ready_values_and_completion_but_ignore_receipts(prepared_store):
	source,db,calendar,sessions,decisions,features = prepared_store
	table = seed_zscores(prepared_store)
	features = FeatureSpecV2(features.ticker_ids,2,(PriceZScore(2),"raw_volume"),adjustment=features.adjustment)
	db.sql(f"UPDATE {table} SET status=2,zscore=NULL WHERE ticker_id=29 AND date_id=?",(sessions[2].sequence,))
	history = load_prepared_history(source,db,calendar,decisions,features,TargetSpec())
	assert history.features.ticker_ids == (17,3)
	window = history.feature_array(decisions[0]).reshape(2,2,2)
	np.testing.assert_array_equal(window[:,:,1],[[117,103],[117,103]])
	assert history.selection["exclusions"]["price_zscore_2"] == [29]
	db.sql("DELETE FROM stockguidance_zscore_sessions WHERE date_id=?",(sessions[2].sequence,))
	with pytest.raises(PreparationRequired,match="price_zscore_2"):
		load_prepared_history(source,db,calendar,decisions,features,TargetSpec())


@pytest.mark.parametrize("kind",[TargetKind.RETURN_ZSCORE_PER_TICKER,TargetKind.RETURN_ZSCORE_CROSS_SECTIONAL])
def test_array_targets_match_existing_formulas(prepared_store,kind):
	from stockguidance.database.repositories.closes import CloseQuery
	from stockguidance.database.repositories.sessions import SessionQuery
	from stockguidance.market_data.history import HistoryBoundary,HistoryProvenance
	from stockguidance.targets.construction import construct_targets
	source,db,calendar,sessions,decisions,features = prepared_store
	if kind is TargetKind.RETURN_ZSCORE_PER_TICKER:
		warmup = calendar.sessions(calendar.shift(decisions[0].entry_session,-20).session_date,calendar.shift(sessions[0],-1).session_date)
		for i,s in enumerate(warmup):
			db.sql("INSERT INTO transaction_dates (id,transaction_date,raw_complete,raw_revision) VALUES (?,?,1,?)",(s.sequence,str(s.session_date),"a"*64))
			for t in features.ticker_ids:
				db.sql("INSERT INTO raw_stock_data (ticker_id,date_id,close,state_id) VALUES (?,?,?,1)",(t,s.sequence,10+t+i))
	target = TargetSpecV2(kind=kind,rolling_window_sessions=20 if kind is TargetKind.RETURN_ZSCORE_PER_TICKER else None)
	history = load_prepared_history(source,db,calendar,decisions,features,target)
	cutoff = sessions[-1].closes_at
	boundary = HistoryBoundary(source.identity,calendar.calendar_id,history.sessions[0].session_date,HistoryProvenance.EXPLICIT_BOUNDARY,"fixture",cutoff)
	data = history.read(CloseQuery(SessionQuery(boundary,history.sessions,cutoff),features.ticker_ids))
	for d in decisions:
		expected = construct_targets(target,features.ticker_ids,d.entry_session,d.exit_session,data)
		raw,transformed = history.target_arrays(d)
		np.testing.assert_allclose(raw,expected.raw)
		np.testing.assert_allclose(transformed,expected.transformed,atol=1e-14)


def test_default_batch_boundary_1001_tickers(prepared_store):
	import sqlite3
	source,db,calendar,sessions,decisions,features = prepared_store
	with sqlite3.connect(db.path) as connection:
		connection.executemany("INSERT INTO ticker_symbol (id,label) VALUES (?,?)",((t,f"T{t}") for t in range(100,1098)))
		connection.executemany("INSERT INTO raw_stock_data (ticker_id,date_id,close,state_id) VALUES (?,?,10,1)",((t,s.sequence) for t in range(100,1098) for s in sessions))
	history = load_prepared_history(source,db,calendar,decisions,features,TargetSpec(),selection=EndpointSelection())
	assert len(history.features.ticker_ids)==1001
	assert len([q for q in history.timings["queries"] if "close load batch" in q["name"]])==2


def test_target_simulation_reuses_bulk_history_and_never_updates(prepared_store,tmp_path,monkeypatch):
	from decimal import Decimal
	from stockguidance.database.table_names import load_table_names
	from stockguidance.market_data.publication import DailyPublicationPolicy
	from stockguidance.portfolio.policy import AllocationPolicy
	from stockguidance.portfolio.precision import ExecutionPolicy
	from stockguidance.recommendations.eligibility import EligibilityPolicy
	from stockguidance.simulation.jobs import SimulationConfiguration,WalkForwardConfiguration
	from stockguidance.simulation.configuration import EvaluationPolicy
	from stockguidance.simulation.options import SimulationOptions
	from stockguidance.training.configuration import TrainingConfig,DeviceChoice
	from stockguidance.workflows import simulation_runtime as module
	source,db,calendar,sessions,decisions,features = prepared_store
	monkeypatch.setattr(module,"source_from_database",lambda *args:(source,load_table_names()))
	configuration = SimulationConfiguration(source,features,sessions[5].session_date,sessions[7].session_date,AllocationPolicy.ALTERNATE_SESSIONS,sessions[5].session_date,Decimal("1000.00"),timedelta(minutes=30),DailyPublicationPolicy(timedelta(0),"fixture"),sessions[-1].closes_at,EligibilityPolicy(0),1,ExecutionPolicy(),WalkForwardConfiguration(sessions[3].session_date,sessions[3].session_date,timedelta(minutes=30),TrainingConfig(epochs=1,device=DeviceChoice.CPU),1))
	result = module._target_simulation(configuration,SimulationOptions(EvaluationPolicy.TRAIN_THEN_SIMULATE,artifacts_directory=tmp_path/"models"),db,load_table_names())
	assert result.successful
	assert result.forecasts and result.training_runs
	assert result.data_preparation["selection"]["ticker_ids"] == [17,3,29]
	assert all(sql.lstrip().split()[0] in {"SELECT","START"} for sql,_ in db.statements)
	assert len([sql for sql,_ in db.statements if sql.startswith("SELECT date_id,ticker_id,close,")]) == 1


def test_configured_training_uses_one_load_for_training_holdout_and_predictions(prepared_store,tmp_path):
	from stockguidance.calendar.ranges import DatePlanSpec,ExplicitRange,AbsoluteDate,DependencySpec,SourceCoverageSnapshot
	from stockguidance.features.specification import FeatureChannel
	from stockguidance.training.configured import ConfiguredTrainingJob,execute_configured_training
	from stockguidance.training.configuration import TrainingConfig,DeviceChoice
	from stockguidance.market_data.publication import DailyPublicationPolicy
	from stockguidance.runs.service import RunDirectoryService
	source,db,calendar,sessions,decisions,features = prepared_store
	coverage = SourceCoverageSnapshot.from_verified_dates(source.identity,calendar.calendar_id,"fixture",tuple(s.session_date for s in sessions),snapshot_id="fixture")
	job = ConfiguredTrainingJob(source,DatePlanSpec(ExplicitRange(AbsoluteDate(sessions[3].session_date),AbsoluteDate(sessions[4].session_date)),ExplicitRange(AbsoluteDate(sessions[6].session_date),AbsoluteDate(sessions[7].session_date)),DependencySpec(model_lookback_sessions=2,simulation_horizon_sessions=2)),2,(FeatureChannel.RELATIVE_CLOSE,),TrainingConfig(epochs=1,device=DeviceChoice.CPU),DailyPublicationPolicy(timedelta(0),"fixture"),timedelta(minutes=30))
	result = execute_configured_training(job,coverage=coverage,connect=db,runs=RunDirectoryService(tmp_path/"runs",project=tmp_path))
	assert result.training.training_samples==2
	assert result.training.evaluation_samples==1
	assert result.universe.ordered_ticker_ids==(3,17,29)
	assert len([sql for sql,_ in db.statements if sql.startswith("SELECT date_id,ticker_id,close,")])==1
	assert all(sql.lstrip().split()[0] in {"SELECT","START"} for sql,_ in db.statements)


def test_recommendation_simulation_freezes_stored_channel_universe(prepared_store,tmp_path):
	from decimal import Decimal
	from stockguidance.database.table_names import load_table_names
	from stockguidance.market_data.publication import DailyPublicationPolicy
	from stockguidance.portfolio.policy import AllocationPolicy
	from stockguidance.portfolio.precision import ExecutionPolicy
	from stockguidance.recommendations.eligibility import EligibilityPolicy
	from stockguidance.simulation.jobs import SimulationConfiguration,WalkForwardConfiguration
	from stockguidance.simulation.configuration import EvaluationPolicy
	from stockguidance.simulation.options import SimulationOptions
	from stockguidance.training.configuration import TrainingConfig,DeviceChoice
	from stockguidance.workflows.recommendation_simulation import simulate_recommendations
	source,db,calendar,sessions,decisions,features = prepared_store
	seed_zscores(prepared_store)
	db.statements.clear()
	configuration = SimulationConfiguration(source,features,sessions[5].session_date,sessions[7].session_date,AllocationPolicy.ALTERNATE_SESSIONS,sessions[5].session_date,Decimal("1000.00"),timedelta(minutes=30),DailyPublicationPolicy(timedelta(0),"fixture"),sessions[-1].closes_at,EligibilityPolicy(0),1,ExecutionPolicy(),WalkForwardConfiguration(sessions[3].session_date,sessions[4].session_date,timedelta(minutes=30),TrainingConfig(epochs=1,device=DeviceChoice.CPU),1),recommendation_setup={"tickers":["T17","T3"],"features":{"channels":["price_zscore_2"],"lookback_sessions":2}})
	result = simulate_recommendations(configuration,SimulationOptions(EvaluationPolicy.CHRONOLOGICAL_RETRAIN,artifacts_directory=tmp_path/"models"),source,db,load_table_names())
	assert result.successful
	assert len(result.training_runs)==2
	assert all(f.sample.features.specification.ticker_ids==(17,3) for f in result.forecasts)
	assert all(sql.lstrip().split()[0] in {"SELECT","START"} for sql,_ in db.statements)
	assert len([sql for sql,_ in db.statements if "SELECT date_id,ticker_id,zscore," in sql])==1


def test_recommendation_runtime_uses_preloaded_history_without_provider(prepared_store,tmp_path,monkeypatch):
	from stockguidance.database.table_names import load_table_names
	from stockguidance.market_data.publication import DailyPublicationPolicy
	from stockguidance.market_data.corrections import CorrectionPolicy
	from stockguidance.models.policy import ModelRefreshPolicy
	from stockguidance.portfolio.policy import AllocationPolicy
	from stockguidance.recommendations.eligibility import EligibilityPolicy
	from stockguidance.recommendations.jobs import RecommendationConfiguration,RetrainingConfiguration
	from stockguidance.recommendations.options import RecommendationOptions
	from stockguidance.training.configuration import TrainingConfig,DeviceChoice
	from stockguidance.workflows import recommendation_runtime as module
	source,db,calendar,sessions,decisions,features = prepared_store
	entry = sessions[7]
	inference = DecisionTiming(calendar.shift(entry,-1),entry,calendar.shift(entry,1),entry.closes_at-timedelta(minutes=30))
	history = load_prepared_history(source,db,calendar,(*decisions,inference),features,TargetSpec(),label_decisions=decisions,transaction_sessions=(inference.previous_session,))
	db.statements.clear()
	monkeypatch.setattr(module,"source_from_database",lambda *args:(source,load_table_names()))
	configuration = RecommendationConfiguration(source,history.features,history.sessions[0].session_date,sessions[6].session_date,entry.session_date,AllocationPolicy.ALTERNATE_SESSIONS,entry.session_date,DailyPublicationPolicy(timedelta(0),"fixture"),CorrectionPolicy(timedelta(days=30),timedelta(hours=1),timedelta(days=7),"fixture"),EligibilityPolicy(0),1,RetrainingConfiguration(decisions[0].entry_session.session_date,decisions[-1].entry_session.session_date,timedelta(minutes=30),TrainingConfig(epochs=1,device=DeviceChoice.CPU)),runs_root=tmp_path/"runs",replay_cutoff=inference.decision_cutoff,prepared_history=history)
	result = module._target_assemble(configuration,RecommendationOptions(ModelRefreshPolicy.RETRAIN,tmp_path/"model.sgmodel"),db,load_table_names())
	assert result.training.training_samples==3
	assert result.sample.features.values is not None
	assert not db.statements


@pytest.mark.parametrize("change",["duplicate","missing","unexpected"])
def test_cursor_key_corruption_is_rejected(prepared_store,monkeypatch,change):
	from support.ingestion import FakeCursor
	original = FakeCursor.fetchmany
	changed = False
	def corrupt(cursor,size=8192):
		nonlocal changed
		rows = original(cursor,size)
		if rows and not changed and cursor.database.statements[-1][0].startswith("SELECT date_id,ticker_id,close,"):
			changed=True
			if change=="duplicate":
				return [*rows,rows[0]]
			if change=="missing":
				return rows[1:]
			return [(rows[0][0],999999,rows[0][2],rows[0][3]),*rows[1:]]
		return rows
	monkeypatch.setattr(FakeCursor,"fetchmany",corrupt)
	with pytest.raises(ValueError,match="duplicate|omitted|unexpected"):
		load(prepared_store)


def test_updater_uses_largest_registered_window_for_its_context(prepared_store,monkeypatch):
	from stockguidance.database import simplified_runtime as module
	from stockguidance.zscores.storage import ZScoreSeriesDefinition
	from stockguidance.zscores.specification import ZScoreFamily,CALCULATION_VERSION,FLOAT32_PRECISION
	from stockguidance.market_data.identity import source_key
	source,db,calendar,sessions,decisions,features = prepared_store
	definition = ZScoreSeriesDefinition(source_key(source.identity),calendar.calendar_id,ZScoreFamily.PRICE,20,CALCULATION_VERSION,FLOAT32_PRECISION,"not_applicable")
	values = definition.as_payload()
	db.sql(f"INSERT INTO stockguidance_zscore_series (series_key,{','.join('`'+c+'`' for c in values)},created_at) VALUES ({','.join('?' for _ in range(len(values)+2))})",(definition.series_key,*values.values(),"2026-01-01"))
	def context(calendar,requested,max_window):
		assert max_window==20
		raise RuntimeError("context checked before any write")
	monkeypatch.setattr(module,"_session_context",context)
	with pytest.raises(RuntimeError,match="context checked"):
		module.run_simplified_update(source,db,calendar,(sessions[-1],),provider=None,clock=object())
	assert all(sql.lstrip().split()[0] in {"SELECT","START"} for sql,_ in db.statements)
