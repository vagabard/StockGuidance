"""Time exact, sampled, and cached universe filtering without training."""

import argparse
import ctypes
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4

from stockguidance.calendar.exchange import ExchangeCalendar
from stockguidance.database.connections import MySQLConnectionFactory
from stockguidance.database.repositories.endpoint_universe import EndpointUniverseRepository
from stockguidance.database.simplified_runtime import source_from_database
from stockguidance.universe.endpoints import EndpointSelection


def peak_memory() -> int:
	if os.name != "nt":
		import resource
		return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
	class Counters(ctypes.Structure):
		_fields_ = [("cb",ctypes.c_ulong),("faults",ctypes.c_ulong),*((name,ctypes.c_size_t) for name in ("peak","working","peak_pool_paged","pool_paged","peak_pool_nonpaged","pool_nonpaged","pagefile","peak_pagefile"))]
	counters = Counters()
	counters.cb = ctypes.sizeof(counters)
	function = ctypes.WinDLL("psapi").GetProcessMemoryInfo
	function.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong]
	if not function(ctypes.c_void_p(-1),ctypes.byref(counters),ctypes.sizeof(counters)):
		raise OSError("Cannot read process memory counters")
	return counters.peak


def main():
	parser=argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--start-date",type=date.fromisoformat,required=True)
	parser.add_argument("--end-date",type=date.fromisoformat,required=True)
	parser.add_argument("--dbinfo-file",type=Path,default=Path("dbinfo.id"))
	parser.add_argument("--output",type=Path,default=Path("output/dynamic-universe-benchmark.json"))
	args=parser.parse_args()
	connect=MySQLConnectionFactory()
	source,names=source_from_database(args.dbinfo_file,Path("data"),connect)
	calendar=ExchangeCalendar(source.calendar_id,args.start_date,args.end_date)
	sessions=calendar.sessions(args.start_date,args.end_date)
	cache=args.output.parent / f"universe-benchmark-cache-{uuid4().hex}"
	repository=EndpointUniverseRepository(source,connect,names,cache_root=cache)
	results=[]
	for label,sample in (("exact",None),("sample40",40),("cached_exact",None)):
		r=repository.select(sessions,EndpointSelection(trim_percent=Decimal(5),sample_sessions=sample))
		summary={k:r[k] for k in ("candidate_count","selected_count","cache_hit","filter_seconds","selection_seconds","sampled")}
		summary.update(case=label,sessions=len(sessions),filter_sessions=len(r["sampled_dates"]),peak_working_set_bytes=peak_memory())
		results.append(summary)
		print(json.dumps(summary),flush=True)
	args.output.parent.mkdir(parents=True,exist_ok=True)
	args.output.write_text(json.dumps(results,indent=2),encoding="utf-8")


if __name__=="__main__":
	main()
