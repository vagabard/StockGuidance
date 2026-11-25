from datetime import date,datetime,timezone
from decimal import Decimal

import pytest
from support.ingestion import FakeDatabase

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig,SourceIdentity,PriceAdjustment
from stockguidance.database.simplified_schema import initialize_simplified_schema
from stockguidance.database.repositories.endpoint_universe import EndpointUniverseRepository
from stockguidance.universe.endpoints import EndpointSelection


@pytest.fixture
def store(tmp_path):
	source=SourceConfig(SourceIdentity("fixture","daily","us","all",PriceAdjustment.UNADJUSTED),tmp_path/"data",tmp_path/"db.id")
	db=FakeDatabase(tmp_path/"selection.sqlite")
	initialize_simplified_schema(source,db,windows=(2,))
	sessions=tuple(TradingSession("XNYS",date(2024,7,i),i,datetime(2024,7,i,13,30,tzinfo=timezone.utc),datetime(2024,7,i,20,tzinfo=timezone.utc)) for i in (1,2,3))
	for s in sessions:
		db.sql("INSERT INTO transaction_dates (id,transaction_date,raw_complete,raw_revision) VALUES (?,?,1,?)",(s.sequence,str(s.session_date),"a"*64))
	for ticker,label in enumerate(("AAA","BBB","CCC","DDD","EEE","FFF"),1):
		db.sql("INSERT INTO ticker_symbol (id,label) VALUES (?,?)",(ticker,label))
	for ticker,days,volume in ((1,(1,2,3),10),(2,(1,),20),(3,(3,),30),(4,(1,3),100),(5,(1,2,3),None),(6,(1,2,3),100)):
		for day in days:
			db.sql("INSERT INTO raw_stock_data (ticker_id,date_id,close,volume,state_id) VALUES (?,?,10,?,1)",(ticker,day,volume))
	return db,EndpointUniverseRepository(source,db),sessions


def test_full_coverage_excludes_middle_gaps_and_zero_skips_volume(store):
	db,repository,sessions=store
	result=repository.select(sessions,EndpointSelection())
	assert result["ticker_ids"]==[1,5,6]
	assert result["candidate_count"]==4 and result["start_population"]==5 and result["end_population"]==5
	assert result["coverage_excluded_ids"]==[4]
	assert result["coverage_missing_sessions"]=={"4":1}
	assert result["coverage_required_sessions"]==3
	assert result["measurements"]=={} and result["sampled_dates"]==[]
	assert not any("SUM(volume" in sql for sql,_ in db.statements)


def test_exact_aggregate_cache_and_revision_invalidation(store):
	db,repository,sessions=store
	settings=EndpointSelection(trim_percent=Decimal(50))
	result=repository.select(sessions,settings)
	assert result["ticker_ids"]==[6] and result["trimmed_ids"]==[1] and result["missing_measurement_ids"]==[5]
	assert result["measurements"]["6"]=={"average_dollar_volume":"1000","observed_sessions":3}
	assert not result["cache_hit"]
	db.statements.clear()
	assert repository.select(sessions,settings)["cache_hit"]
	assert not any("SUM(volume" in sql for sql,_ in db.statements)
	db.sql("UPDATE transaction_dates SET raw_revision=? WHERE id=2",("b"*64,))
	assert not repository.select(sessions,settings)["cache_hit"]


def test_sample_only_queries_chosen_dates_and_explicit_order_is_unchanged(store):
	db,repository,sessions=store
	result=repository.select(sessions,EndpointSelection(trim_percent=Decimal(25),sample_sessions=2))
	assert result["sampled_dates"]==["2024-07-01","2024-07-03"] and result["sampled"]
	assert result["coverage_excluded_ids"]==[4]
	assert repository.select(sessions,EndpointSelection(tickers=("FFF","AAA")))["ticker_ids"]==[6,1]
	assert repository.select(sessions,EndpointSelection(tickers=("DDD","AAA")))["ticker_ids"]==[1]
	with pytest.raises(ValueError,match="unknown"):
		repository.select(sessions,EndpointSelection(tickers=("unknown",)))


def test_incomplete_session_is_not_silently_ignored(store):
	db,repository,sessions=store
	db.sql("UPDATE transaction_dates SET raw_complete=0 WHERE id=2")
	with pytest.raises(ValueError,match="complete raw sessions"):
		repository.select(sessions,EndpointSelection())


def test_repair_and_algorithm_changes_invalidate_filter_cache(store,monkeypatch):
	db,repository,sessions=store
	settings=EndpointSelection(trim_percent=Decimal(50))
	repository.select(sessions,settings)
	assert repository.select(sessions,settings)["cache_hit"]
	db.sql("INSERT INTO stockguidance_repair_metadata (date_id,ticker_id,revision,state_id,original_values,invalid_fields,recorded_at) VALUES (2,1,?,2,'[]','[]',?)",("c"*64,"2026-11-25T00:00:00+00:00"))
	assert not repository.select(sessions,settings)["cache_hit"]
	assert repository.select(sessions,settings)["cache_hit"]
	monkeypatch.setattr("stockguidance.database.repositories.endpoint_universe.ALGORITHM_VERSION","next-design")
	assert not repository.select(sessions,settings)["cache_hit"]


def test_pending_updates_block_universe_selection(store):
	from stockguidance.database.repositories.simplified import PendingUpdateRepository
	from stockguidance.database.simplified_contract import ClearScope,OperationKind,PendingScope,WorkUnit
	from stockguidance.market_data.identity import data_configuration_key
	db,repository,sessions=store
	now=datetime(2026,11,25,tzinfo=timezone.utc)
	unit=WorkUnit("delta_session",2,1,sessions[1].session_date)
	scope=PendingScope(1,OperationKind.DERIVED_REPAIR,data_configuration_key(repository.source.data_configuration),"XNYS",now,now,(),ClearScope(deltas=(unit,)),(unit,),())
	PendingUpdateRepository(repository.source,db).register(scope,now)
	with pytest.raises(ValueError,match="unfinished operation"):
		repository.select(sessions,EndpointSelection())


@pytest.mark.parametrize("change",["DELETE FROM raw_stock_data WHERE ticker_id=1 AND date_id=2", "UPDATE raw_stock_data SET state_id=2 WHERE ticker_id=1 AND date_id=2", "UPDATE raw_stock_data SET close=0 WHERE ticker_id=1 AND date_id=2", "UPDATE raw_stock_data SET close=NULL WHERE ticker_id=1 AND date_id=2"])
def test_absent_invalid_and_unusable_days_are_excluded(store,change):
	db,repository,sessions=store
	db.sql(change)
	result=repository.select(sessions,EndpointSelection())
	assert 1 not in result["ticker_ids"]
	assert result["coverage_missing_sessions"]["1"]==1


def test_explicit_selection_with_no_complete_tickers_fails(store):
	_db,repository,sessions=store
	with pytest.raises(ValueError,match="coverage filtering produced an empty universe"):
		repository.select(sessions,EndpointSelection(tickers=("DDD",)))
