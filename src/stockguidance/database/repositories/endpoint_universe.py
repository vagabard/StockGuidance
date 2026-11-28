"""Bounded SQL endpoint selection and aggregate-only dollar-volume reads."""

from decimal import Decimal
import json
from pathlib import Path
from time import perf_counter
from collections.abc import Sequence
from typing import Any
from hashlib import sha256

from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.database.table_names import TableNames

from stockguidance.database.operation_recovery import consistent_read_transaction, DatabaseReadinessGuard, ReadScope
from stockguidance.database.table_names import TableRole, quote_identifier, load_table_names
from stockguidance.database.validation_gate import data_versions
from stockguidance.market_data.atomic import atomic_write
from stockguidance.market_data.identity import content_id, encode_json, data_configuration_key
from stockguidance.universe.endpoints import EndpointSelection, evenly_spaced, trim_candidates, filter_complete_history

ALGORITHM_VERSION = "complete-history-dollar-close-v2"


def marks(values: Sequence[object]) -> str:
	return ",".join("%s" for _ in values)


class EndpointUniverseRepository:
	def __init__(self, source: SourceConfig, connect: WriteConnectionFactory, table_names: TableNames | None=None, *, cache_root: Path | None = None) -> None:
		self.source,self.connect = source,connect
		self.names = table_names or load_table_names()
		self.cache_root = cache_root or source.data_root / ".universe-cache"

	def select(self, sessions: tuple[TradingSession,...], settings: EndpointSelection, *, require_transaction_observations: bool = False) -> dict[str,Any]:
		started = perf_counter()
		if not sessions:
			raise ValueError("Universe selection requires trading sessions")
		dates = tuple(s.session_date for s in sessions)
		prices = quote_identifier(self.names.table(TableRole.PRICES))
		session_table = quote_identifier(self.names.table(TableRole.SESSIONS))
		ticker_table = quote_identifier(self.names.table(TableRole.TICKERS))
		with consistent_read_transaction(self.source,self.connect) as transaction:
			cursor = transaction.cursor()
			cursor.execute(f"SELECT id,transaction_date,raw_complete,raw_revision FROM {session_table} WHERE transaction_date IN ({marks(dates)}) ORDER BY transaction_date",dates)
			rows = cursor.fetchall()
			if len(rows)!=len(dates) or any(not row[2] or not row[3] for row in rows):
				raise ValueError("Universe selection requires complete raw sessions throughout the requested range")
			ids = tuple(int(str(row[0])) for row in rows)
			if not DatabaseReadinessGuard(self.names).check(ReadScope(date_ids=ids),transaction).ready:
				raise ValueError("An unfinished operation overlaps universe selection")
			versions = data_versions(cursor,self.names,ids)
			populations = []
			for day_id in (ids[0],ids[-1]):
				cursor.execute(f"SELECT ticker_id FROM {prices} WHERE date_id=%s AND state_id=1 AND close>0 ORDER BY ticker_id",(day_id,))
				populations.append({int(str(row[0])) for row in cursor.fetchall()})
			if settings.tickers is not None:
				cursor.execute(f"SELECT id,label FROM {ticker_table} WHERE label IN ({marks(settings.tickers)})",settings.tickers)
				by_label = {str(row[1]):int(str(row[0])) for row in cursor.fetchall()}
				if any(label not in by_label for label in settings.tickers):
					raise ValueError("An explicitly requested ticker symbol is unknown (symbols are case sensitive)")
				candidates = tuple(by_label[label] for label in settings.tickers)
			elif settings.ticker_ids is not None:
				candidates = settings.ticker_ids
			else:
				candidates = tuple(sorted(populations[0] & populations[-1]))
			if not candidates:
				raise ValueError("Endpoint membership produced an empty universe")
			cursor.execute(f"SELECT id,label FROM {ticker_table} WHERE id IN ({marks(candidates)})",candidates)
			labels = {int(str(row[0])):str(row[1]) for row in cursor.fetchall()}
			if any(v not in labels for v in candidates):
				raise ValueError("An explicitly requested ticker ID is unknown")
			candidate_count = len(candidates)
			coverage_started = perf_counter()
			observed_sessions: dict[int,int] = {}
			# Count all requested sessions even when dollar-volume sampling is enabled.
			# The unique (ticker_id,date_id) key makes COUNT(*) a session count.
			for offset in range(0,len(candidates),500):
				batch = candidates[offset:offset+500]
				cursor.execute(f"SELECT ticker_id,COUNT(*) FROM {prices} WHERE ticker_id IN ({marks(batch)}) AND date_id IN ({marks(ids)}) AND state_id=1 AND close>0 GROUP BY ticker_id",(*batch,*ids))
				observed_sessions.update({int(str(row[0])):int(str(row[1])) for row in cursor.fetchall()})
			candidates,coverage_excluded = filter_complete_history(candidates,observed_sessions,len(ids))
			coverage_seconds = perf_counter()-coverage_started
			if not candidates:
				raise ValueError("Full-period history coverage filtering produced an empty universe")
			transaction_excluded: tuple[int,...] = ()
			if require_transaction_observations:
				transaction_ids: set[int] = set()
				for offset in range(0,len(candidates),500):
					batch = candidates[offset:offset+500]
					cursor.execute(f"SELECT ticker_id FROM {prices} WHERE date_id=%s AND ticker_id IN ({marks(batch)}) AND state_id=1 AND close>0 AND transactions IS NOT NULL AND transactions>=0",(ids[-1],*batch))
					transaction_ids.update(int(str(row[0])) for row in cursor.fetchall())
				transaction_excluded = tuple(v for v in candidates if v not in transaction_ids)
				candidates = tuple(v for v in candidates if v in transaction_ids)
			if not candidates:
				raise ValueError("Transaction-observation filtering produced an empty universe")
			sampled = evenly_spaced(ids,settings.sample_sessions) if settings.trim_percent else ()
			from stockguidance.universe import endpoints
			algorithm = content_id([ALGORITHM_VERSION,sha256(Path(__file__).read_bytes()).hexdigest(),sha256(Path(endpoints.__file__).read_bytes()).hexdigest()])
			key = content_id({"algorithm":algorithm,"source":data_configuration_key(self.source.data_configuration),
				"versions":versions,"candidates":candidates,"trim":str(settings.trim_percent),"sampled":sampled,
				"require_transaction_observations":require_transaction_observations})
			cache = self.cache_root / f"{key}.json"
			cache_hit = False
			filter_started = perf_counter()
			cached = None
			if settings.trim_percent and cache.is_file():
				try:
					cached = json.loads(cache.read_text(encoding="utf-8"))
					if cached.get("key")!=key or not isinstance(cached.get("measurements"),dict):
						cached = None
				except (OSError,ValueError,TypeError):
					cached = None
			if cached is not None:
				try:
					measurements = {int(k):(Decimal(v[0]),int(v[1])) for k,v in cached["measurements"].items()}
					if any(not v[0].is_finite() or v[0]<0 or v[1]<=0 for v in measurements.values()):
						raise ValueError("Invalid cache measurement")
					cache_hit = True
				except (ValueError,TypeError,IndexError,KeyError):
					cached = None
			if cached is None:
				measurements = {}
				if settings.trim_percent:
					# Each query returns at most 500 aggregates, never raw history.
					for offset in range(0,len(candidates),500):
						batch = candidates[offset:offset+500]
						cursor.execute(f"SELECT ticker_id,SUM(volume*close),COUNT(*) FROM {prices} WHERE ticker_id IN ({marks(batch)}) AND date_id IN ({marks(sampled)}) AND state_id=1 AND close>0 AND volume>=0 GROUP BY ticker_id",(*batch,*sampled))
						for ticker,total,count in cursor.fetchall():
							measurements[int(str(ticker))] = (Decimal(str(total))/int(str(count)),int(str(count)))
					atomic_write(cache,encode_json({"key":key,"measurements":{str(k):[str(v[0]),v[1]] for k,v in measurements.items()}}))
			selected,trimmed,missing = trim_candidates(candidates,measurements,settings.trim_percent)
			filter_seconds = perf_counter()-filter_started
			if not selected:
				raise ValueError("Dollar-volume filtering produced an empty universe")
			by_id = dict(zip(ids,dates))
			return {"schema":"endpoint-universe-1","selection_mode":"explicit" if settings.tickers is not None or settings.ticker_ids is not None else "endpoint_intersection",
				"start_population":len(populations[0]),"end_population":len(populations[-1]),"candidate_count":candidate_count,"selected_count":len(selected),
				"coverage_required_sessions":len(ids),"coverage_required_percent":100,"coverage_eligible_count":len(candidates),
				"coverage_excluded_ids":list(coverage_excluded),"coverage_excluded_labels":[labels[v] for v in coverage_excluded],
				"coverage_missing_sessions":{str(v):len(ids)-observed_sessions.get(v,0) for v in coverage_excluded},"coverage_seconds":coverage_seconds,
				"transaction_observation_required":require_transaction_observations,"transaction_observation_excluded_ids":list(transaction_excluded),
				"transaction_observation_excluded_labels":[labels[v] for v in transaction_excluded],"transaction_observation_eligible_count":len(candidates),
				"ticker_ids":list(selected),"ticker_labels":[labels[v] for v in selected],"trim_percent":str(settings.trim_percent),"trimmed_ids":list(trimmed),"missing_measurement_ids":list(missing),
				"metric":"mean_daily_shares_times_close","sampled_dates":[str(by_id[v]) for v in sampled],"sampled":bool(sampled and len(sampled)<len(ids)),
				"sample_sessions_requested":settings.sample_sessions,"required_measurement_sessions":len(sampled),"filter_disabled":not bool(settings.trim_percent),
				"measurements":{str(k):{"average_dollar_volume":str(v[0]),"observed_sessions":v[1]} for k,v in measurements.items()},
				"cache_hit":cache_hit,"data_version":key,"filter_seconds":filter_seconds,"selection_seconds":perf_counter()-started}
