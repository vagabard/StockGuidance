"""Compare read-only per-case loading with the prepared array path.

Run from the project root. This reads existing data, never updates it, and
prints measurements; neither model fitting nor artifact export is performed.
"""
import argparse
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
import tracemalloc

import numpy as np

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.configuration import load_database_configuration
from stockguidance.database.prepared_history import load_prepared_history
from stockguidance.database.simplified_runtime import source_from_database
from stockguidance.database.table_names import TableRole, quote_identifier
from stockguidance.datasets.training import build_training_dataset
from stockguidance.datasets.numpy_arrays import training_arrays
from stockguidance.features.specification import FeatureSpec
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.market_data.history import HistoryBoundary, HistoryProvenance
from stockguidance.market_data.publication import DailyPublicationPolicy
from stockguidance.targets.specification import TargetSpec
from stockguidance.workflows.simplified_readiness import SimplifiedCloseRepository
from stockguidance.workflows.simplified_readiness import SimplifiedCoverageError


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--dbinfo",type=Path,default=Path("dbinfo.id"))
	parser.add_argument("--data-root",type=Path,default=Path("data"))
	parser.add_argument("--cases",type=int,default=20)
	parser.add_argument("--tickers",type=int,default=1000)
	parser.add_argument("--lookback",type=int,default=20)
	args = parser.parse_args()
	if min(args.cases,args.tickers,args.lookback)<1:
		parser.error("cases, tickers and lookback must be positive")
	connect = MySQLConnectionFactory()
	names = load_database_configuration(args.dbinfo).load_table_names()
	source,names = source_from_database(args.dbinfo,args.data_root,connect,names)
	connection = connect(args.dbinfo)
	cursor = connection.cursor()
	try:
		cursor.execute(f"SELECT MAX(transaction_date) FROM {quote_identifier(names.table(TableRole.SESSIONS))} WHERE raw_complete=1",())
		end = date.fromisoformat(str(cursor.fetchall()[0][0]))
		cursor.execute(f"SELECT id FROM {quote_identifier(names.table(TableRole.TICKERS))} ORDER BY id LIMIT %s",(args.tickers,))
		tickers = tuple(int(row[0]) for row in cursor.fetchall())
	finally:
		connection.rollback()
		cursor.close()
		connection.close()
	calendar = ExchangeCalendar(source.calendar_id,end-timedelta(days=(args.cases+args.lookback)*3+30),end+timedelta(days=14))
	last_entry = calendar.shift(calendar.session(end),-1)
	entries = calendar.sessions(calendar.shift(last_entry,-args.cases+1).session_date,last_entry.session_date)
	decisions = tuple(DecisionTiming(calendar.shift(s,-1),s,calendar.shift(s,1),s.closes_at-timedelta(minutes=30)) for s in entries)
	features = FeatureSpec(tickers,args.lookback,adjustment=source.identity.adjustment)
	tracemalloc.start()
	started = perf_counter()
	history = load_prepared_history(source,connect,calendar,decisions,features,TargetSpec(),table_names=names,max_array_bytes=256*1024*1024)
	loaded = perf_counter()-started
	dataset = history.dataset(decisions,calendar.session(end).closes_at)
	started = perf_counter()
	for index in range(len(dataset.eligible)):
		dataset.case_arrays(index)
	construction = perf_counter()-started
	_,array_peak = tracemalloc.get_traced_memory()
	tracemalloc.stop()
	boundary = HistoryBoundary(source.identity,calendar.calendar_id,history.sessions[0].session_date,HistoryProvenance.EXPLICIT_BOUNDARY,"Benchmark prepared source",calendar.session(end).closes_at)
	publication = DailyPublicationPolicy(timedelta(0),"Benchmark current prepared history")
	tracemalloc.start()
	started = perf_counter()
	reference_kind = "database_per_case"
	try:
		reference = build_training_dataset(decisions,dataset.training_cutoff,history.features,TargetSpec(),boundary,calendar,publication,SimplifiedCloseRepository(source,connect,names),AvailabilityPolicy.CURRENT_PREPARED)
	except SimplifiedCoverageError:
		# Never regenerate validation receipts as part of a read-only benchmark.
		# This fallback isolates the old object/window construction cost.
		reference_kind = "object_construction_from_loaded_snapshot_database_reference_blocked_by_validation_gate"
		tracemalloc.reset_peak()
		started = perf_counter()
		reference = build_training_dataset(decisions,dataset.training_cutoff,history.features,TargetSpec(),boundary,calendar,publication,history,AvailabilityPolicy.CURRENT_PREPARED)
	x,y = training_arrays(reference)
	reference_seconds = perf_counter()-started
	_,reference_peak = tracemalloc.get_traced_memory()
	tracemalloc.stop()
	for index in range(len(dataset.eligible)):
		a,b = dataset.case_arrays(index)
		np.testing.assert_allclose(a,x[index],atol=1e-14)
		np.testing.assert_allclose(b,y[index],atol=1e-14)
	import json
	print(json.dumps({"cases":len(dataset.eligible),"tickers":len(history.features.ticker_ids),"lookback":args.lookback,"bulk_load_seconds":loaded,"array_case_seconds":construction,"reference_seconds":reference_seconds,"reference_kind":reference_kind,"array_peak_bytes":array_peak,"reference_peak_bytes":reference_peak,"base_array_bytes":history.timings["array_bytes"],"bulk_load_queries":len([q for q in history.timings["queries"] if "load batch" in q["name"]]),"numeric_parity":True},sort_keys=True))


if __name__ == "__main__":
	main()
