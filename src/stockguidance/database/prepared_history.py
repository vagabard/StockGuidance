"""Read-only, aggregate selection followed by bounded cursor-to-array loading."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from time import perf_counter
import sys
from typing import Any, Iterator


from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import SourceConfig
from stockguidance.config.history import HistoryLoadOptions
from stockguidance.database.operation_recovery import consistent_read_transaction, DatabaseReadinessGuard, ReadScope
from stockguidance.database.simplified_schema import require_simplified_schema
from stockguidance.database.table_names import TableNames, TableRole, load_table_names, quote_identifier
from stockguidance.database.write_connections import WriteConnectionFactory
from stockguidance.database.configuration import load_database_configuration
from stockguidance.datasets.prepared import PreparedHistory
from stockguidance.features.specification import FeatureSpecification, FeatureSpecV2, SignalMode, PriceZScore, PriceDeltaZScore, VolumeZScore, channel_key
from stockguidance.market_data.quality import QualityFlag
from stockguidance.targets.specification import TargetSpecification
from stockguidance.universe.endpoints import EndpointSelection, evenly_spaced, trim_candidates
from stockguidance.universe.selection import UniverseFilters
from stockguidance.market_data.identity import source_key
from stockguidance.zscores.storage import ZScoreSeriesDefinition
from stockguidance.zscores.specification import CALCULATION_VERSION, DELTA_DEFINITION, FLOAT32_PRECISION, ZScoreFamily
from stockguidance.database.preparation import VOLUME_CALCULATION_VERSION


def marks(values: Any) -> str:
	return ",".join("%s" for _ in values)


class PreparationRequired(ValueError):
	"""The updater must complete this scope before it can be consumed."""


class Timings:
	def __init__(self) -> None:
		self.values: dict[str, Any] = {"phases": {}, "queries": [], "rows": 0}

	@contextmanager
	def phase(self, name: str) -> Iterator[None]:
		started = perf_counter()
		try:
			yield
		finally:
			elapsed = perf_counter()-started
			self.values["phases"][name] = elapsed
			print(f"[data] {name}: {elapsed:.3f}s", file=sys.stderr, flush=True)

	def rows(self, cursor: Any, sql: str, parameters: tuple[object, ...], name: str, fetch_size: int) -> Iterator[Any]:
		record: dict[str, Any] = {"name": name, "rows": 0, "execute_seconds": 0.0, "fetch_seconds": 0.0, "fill_seconds": 0.0}
		self.values["queries"].append(record)
		started = perf_counter()
		cursor.execute(sql, parameters)
		record["execute_seconds"] = perf_counter()-started
		while True:
			started = perf_counter()
			rows = cursor.fetchmany(fetch_size)
			record["fetch_seconds"] += perf_counter()-started
			if not rows:
				break
			record["rows"] += len(rows)
			self.values["rows"] += len(rows)
			started = perf_counter()
			yield from rows
			record["fill_seconds"] += perf_counter()-started
		print(f"[data] {name}: {record['rows']} rows; execute {record['execute_seconds']:.3f}s; fetch {record['fetch_seconds']:.3f}s; consume {record['fill_seconds']:.3f}s", file=sys.stderr, flush=True)


def load_prepared_history(
	source: SourceConfig, connect: WriteConnectionFactory, calendar: SessionCalendar,
	decisions: tuple[DecisionTiming, ...], features: FeatureSpecification, target: TargetSpecification,
	*, label_decisions: tuple[DecisionTiming, ...] | None = None,
	extra_sessions: tuple[TradingSession, ...] = (), selection: EndpointSelection | None = None,
	filters: UniverseFilters = UniverseFilters(), filter_cutoff: TradingSession | None = None,
	transaction_sessions: tuple[TradingSession, ...] = (), table_names: TableNames | None = None,
	batch_size: int = 1000, fetch_size: int = 8192, max_array_bytes: int | None = None,
	options: HistoryLoadOptions | None = None,
) -> PreparedHistory:
	"""Select a fixed universe, then load only its required fields in one snapshot.

	No modification/receipt timestamps or repair history are read. Current
	completion flags and row states are authoritative; the updater owns repair.
	"""
	import numpy as np
	if options is not None:
		batch_size, fetch_size, max_array_bytes = options.ticker_batch_size, options.fetch_rows, options.max_array_bytes
	if type(batch_size) is not int or batch_size < 1 or type(fetch_size) is not int or fetch_size < 1:
		raise ValueError("History batch and fetch sizes must be positive integers")
	if not decisions:
		raise ValueError("Prepared history requires explicit decisions")
	timer = Timings()
	timer.values.update(ticker_batch_size=batch_size,fetch_rows=fetch_size,max_array_bytes=max_array_bytes)
	with timer.phase("total preparation"):
		with timer.phase("date resolution"):
			names = table_names or (load_database_configuration(source.database_config).load_table_names() if source.database_config.is_file() else load_table_names())
			settings = selection or EndpointSelection(ticker_ids=features.ticker_ids)
			labels = decisions if label_decisions is None else label_decisions
			transaction_sessions = tuple(sorted(set(transaction_sessions),key=lambda s:s.sequence))
			history_length = features.required_history_sessions if isinstance(features, FeatureSpecV2) else features.lookback_sessions
			shifted: dict[int, TradingSession] = {}
			def shift(session: TradingSession, offset: int) -> TradingSession:
				sequence = session.sequence+offset
				if sequence not in shifted:
					shifted[sequence] = calendar.shift(session,offset)
				return shifted[sequence]
			input_sessions = tuple(sorted({shift(d.entry_session, o) for d in decisions for o in range(-features.lookback_sessions, 0)}, key=lambda s:s.sequence))
			required = {shift(d.entry_session, o) for d in decisions for o in range(-history_length, 0)}
			required.update(extra_sessions)
			for d in labels:
				required.update(shift(d.entry_session, o) for o in (*target.baseline_offsets, 0, 1))
			if features.signal_mode is SignalMode.ENTRY_OPEN:
				required.update(d.entry_session for d in decisions)
			cutoff = filter_cutoff or max(input_sessions, key=lambda s:s.sequence)
			if filters.average_volume is not None:
				required.update(shift(cutoff, -o) for o in range(filters.average_volume.window_sessions))
			if filters.volatility is not None:
				required.update(shift(cutoff, -o) for o in range(filters.volatility.window_sessions+1))
			required.update(transaction_sessions)
			sessions = calendar.sessions(min(required,key=lambda s:s.sequence).session_date, max(required,key=lambda s:s.sequence).session_date)
			prices = quote_identifier(names.table(TableRole.PRICES))
			date_table = quote_identifier(names.table(TableRole.SESSIONS))
			ticker_table = quote_identifier(names.table(TableRole.TICKERS))
			fields: dict[str, tuple[str, str, str, tuple[TradingSession, ...]]] = {"close": (prices, "close", "state_id IN (1,4) AND close>0", sessions)}
			if isinstance(features, FeatureSpecV2):
				for channel in features.external_channels:
					key = channel_key(channel)
					if isinstance(channel, (PriceZScore, PriceDeltaZScore, VolumeZScore)):
						tables = names.resolve_zscore_tables((channel.window,))[channel.window]
						attr = "price" if isinstance(channel, PriceZScore) else "price_delta" if isinstance(channel, PriceDeltaZScore) else "volume"
						fields[key] = (quote_identifier(getattr(tables, attr)), "zscore", "status=0 AND zscore IS NOT NULL AND ABS(zscore)<=3.4028234663852886e38", input_sessions)
					else:
						fields[key] = (prices, "volume", "state_id IN (1,4) AND volume>=0", input_sessions)
			if features.signal_mode is SignalMode.ENTRY_OPEN:
				fields["open"] = (prices, "open", "state_id IN (1,4) AND open>0", tuple(sorted({d.entry_session for d in decisions},key=lambda s:s.sequence)))
			if transaction_sessions:
				fields["transactions"] = (prices, "transactions", "state_id IN (1,4) AND transactions>=0", transaction_sessions)
		with consistent_read_transaction(source, connect, streaming=True) as transaction:
			cursor: Any = transaction.cursor()
			def rows(sql: str, parameters: tuple[object, ...], name: str) -> Iterator[Any]:
				return timer.rows(cursor, sql, parameters, name, fetch_size)
			with timer.phase("coverage and ticker selection"):
				require_simplified_schema(cursor, names)
				days = tuple(s.session_date for s in sessions)
				session_rows = list(rows(f"SELECT id,transaction_date,raw_complete,raw_revision FROM {date_table} WHERE transaction_date IN ({marks(days)}) ORDER BY transaction_date", days, "session coverage"))
				if len(session_rows) != len(days) or any(not r[2] or not r[3] for r in session_rows):
					raise PreparationRequired("Raw sessions are incomplete; run update_market_data for the requested dates and warmup")
				date_ids = {str(r[1]): int(r[0]) for r in session_rows}
				ids = tuple(date_ids[str(s.session_date)] for s in sessions)
				if not DatabaseReadinessGuard(names).check(ReadScope(date_ids=ids), transaction).ready:
					raise PreparationRequired("An unfinished database operation overlaps this run; complete update_market_data first")
				identity_where = ""
				identity_parameters: tuple[object,...] = ()
				if settings.ticker_ids is not None:
					identity_where = f" WHERE id IN ({marks(settings.ticker_ids)})"
					identity_parameters = settings.ticker_ids
				elif settings.tickers is not None:
					identity_where = f" WHERE label IN ({marks(settings.tickers)})"
					identity_parameters = settings.tickers
				else:
					# Endpoint probes discard inactive symbols before full coverage scans.
					identity_where = f" WHERE EXISTS (SELECT 1 FROM {prices} p WHERE p.ticker_id=t.id AND p.date_id=%s AND p.state_id IN (1,4) AND p.close>0) AND EXISTS (SELECT 1 FROM {prices} p WHERE p.ticker_id=t.id AND p.date_id=%s AND p.state_id IN (1,4) AND p.close>0)"
					identity_parameters = (ids[0],ids[-1])
				identities = dict((int(r[0]),str(r[1])) for r in rows(f"SELECT id,label FROM {ticker_table} t{identity_where} ORDER BY id", identity_parameters, "ticker identities"))
				if settings.tickers is not None:
					by_label = {v:k for k,v in identities.items()}
					if any(t not in by_label for t in settings.tickers):
						raise ValueError("Explicit selection contains an unknown ticker symbol")
					candidates = tuple(by_label[t] for t in settings.tickers)
				else:
					candidates = settings.ticker_ids or tuple(identities)
				if any(t not in identities for t in candidates):
					raise ValueError("Explicit selection contains an unknown ticker ID")
				if not candidates:
					raise ValueError("Endpoint coverage produced an empty ticker universe")
				original = candidates
				exclusions: dict[str, list[int]] = {}
				# Each field's aggregate returns at most one row per candidate.
				for key, (table, column, predicate, scope) in fields.items():
					scope_ids = tuple(date_ids[str(s.session_date)] for s in scope)
					if table != prices:
						series = quote_identifier(names.table(TableRole.ZSCORE_SESSIONS))
						stored_channel = next(c for c in features.feature_order if channel_key(c)==key)
						assert isinstance(stored_channel,(PriceZScore,PriceDeltaZScore,VolumeZScore))
						family = "price" if isinstance(stored_channel,PriceZScore) else "price_delta" if isinstance(stored_channel,PriceDeltaZScore) else "volume"
						definition = ZScoreSeriesDefinition(source_key(source.identity),calendar.calendar_id,ZScoreFamily(family),stored_channel.window,VOLUME_CALCULATION_VERSION if family=="volume" else CALCULATION_VERSION,FLOAT32_PRECISION,DELTA_DEFINITION if family=="price_delta" else "not_applicable")
						complete = list(rows(f"SELECT COUNT(*) FROM {series} WHERE series_key=%s AND date_id IN ({marks(scope_ids)})", (definition.series_key, *scope_ids), f"{key} preparation"))
						if int(complete[0][0]) != len(scope_ids):
							raise PreparationRequired(f"{key} preparation is incomplete; run update_market_data with the required z-score windows")
					counts: dict[int,int] = {}
					for offset in range(0,len(candidates),batch_size):
						batch = candidates[offset:offset+batch_size]
						counts.update((int(r[0]),int(r[1])) for r in rows(f"SELECT ticker_id,COUNT(*) FROM {table} WHERE ticker_id IN ({marks(batch)}) AND date_id IN ({marks(scope_ids)}) AND {predicate} GROUP BY ticker_id", (*batch,*scope_ids), f"{key} coverage batch {offset//batch_size+1}"))
					exclusions[key] = [t for t in candidates if counts.get(t,0)!=len(scope_ids)]
					candidates = tuple(t for t in candidates if counts.get(t,0)==len(scope_ids))
					if not candidates:
						raise ValueError(f"No eligible tickers after {key} coverage; exclusions: {exclusions}")
			with timer.phase("optional ticker filters"):
				# SQL aggregates keep optional filters out of the history transfer.
				for field, policy in (("volume", filters.average_volume),):
					if policy is None:
						continue
					tail = tuple(date_ids[str(shift(cutoff,-o).session_date)] for o in range(policy.window_sessions))
					measurements: dict[int, Decimal] = {}
					for offset in range(0,len(candidates),batch_size):
						batch = candidates[offset:offset+batch_size]
						measurements.update((int(r[0]),Decimal(str(r[1]))) for r in rows(f"SELECT ticker_id,AVG({field}) FROM {prices} WHERE ticker_id IN ({marks(batch)}) AND date_id IN ({marks(tail)}) AND state_id IN (1,4) AND {field}>=0 GROUP BY ticker_id HAVING COUNT(*)=%s", (*batch,*tail,len(tail)), "average volume filter"))
					candidates = tuple(t for t in candidates if t in measurements and (policy.lower is None or measurements[t]>=policy.lower) and (policy.upper is None or measurements[t]<=policy.upper))
				if filters.volatility is not None:
					policy_v = filters.volatility
					tail_sessions = tuple(shift(cutoff,-o) for o in reversed(range(policy_v.window_sessions+1)))
					tail = tuple(date_ids[str(s.session_date)] for s in tail_sessions)
					measurements_v: dict[int, Decimal] = {}
					for offset in range(0,len(candidates),batch_size):
						batch = candidates[offset:offset+batch_size]
						base = f"SELECT p.ticker_id,1.0*p.close / LAG(p.close) OVER (PARTITION BY p.ticker_id ORDER BY d.transaction_date)-1 AS r FROM {prices} p JOIN {date_table} d ON d.id=p.date_id WHERE p.ticker_id IN ({marks(batch)}) AND p.date_id IN ({marks(tail)})"
						for t,mean,squared,count in rows(f"SELECT ticker_id,AVG(r),AVG(r*r),COUNT(r) FROM ({base}) returns_by_date GROUP BY ticker_id", (*batch,*tail), "volatility filter"):
							if int(count)==policy_v.window_sessions:
								measurements_v[int(t)] = max(Decimal(0),Decimal(str(squared))-Decimal(str(mean))**2).sqrt()
					candidates = tuple(t for t in candidates if t in measurements_v and (policy_v.lower is None or measurements_v[t]>=policy_v.lower) and (policy_v.upper is None or measurements_v[t]<=policy_v.upper))
				if filters.legacy_transactions is not None:
					kept: set[int] = set()
					for offset in range(0,len(candidates),batch_size):
						batch = candidates[offset:offset+batch_size]
						kept.update(int(r[0]) for r in rows(f"SELECT ticker_id FROM {prices} WHERE ticker_id IN ({marks(batch)}) AND date_id=%s AND transactions>=%s", (*batch,date_ids[str(cutoff.session_date)],filters.legacy_transactions.minimum), "transaction minimum filter"))
					candidates = tuple(t for t in candidates if t in kept)
				measurements_d: dict[int, tuple[Decimal,int]] = {}
				if settings.trim_percent:
					sampled = evenly_spaced(ids,settings.sample_sessions)
					for offset in range(0,len(candidates),batch_size):
						batch = candidates[offset:offset+batch_size]
						for t,total,count in rows(f"SELECT ticker_id,SUM(volume*close),COUNT(*) FROM {prices} WHERE ticker_id IN ({marks(batch)}) AND date_id IN ({marks(sampled)}) AND volume>=0 GROUP BY ticker_id HAVING COUNT(*)=%s", (*batch,*sampled,len(sampled)), "dollar volume filter"):
							measurements_d[int(t)] = (Decimal(str(total))/int(count),int(count))
					candidates,_,_ = trim_candidates(candidates,measurements_d,settings.trim_percent)
				if not candidates:
					raise ValueError("Optional filters produced an empty ticker universe")
			with timer.phase("array allocation"):
				shape = (len(sessions),len(candidates))
				array_bytes = shape[0]*shape[1]*(8*len(fields)+2)
				if max_array_bytes is not None and array_bytes > max_array_bytes:
					raise MemoryError(f"Prepared arrays require {array_bytes} bytes, exceeding the {max_array_bytes} byte budget")
				try:
					arrays = {key:np.full(shape,np.nan,dtype=np.float64) for key in fields}
					quality = np.zeros(shape,dtype=np.uint16)
				except MemoryError:
					raise MemoryError(f"Cannot allocate {array_bytes} bytes for prepared history; reduce the ticker list or date range") from None
				timer.values["array_bytes"] = array_bytes
				timer.values["selected_tickers"] = len(candidates)
				date_index = {value:i for i,value in enumerate(ids)}
				ticker_index = {value:i for i,value in enumerate(candidates)}
			with timer.phase("bulk loading"):
				for key,(table,column,predicate,scope) in fields.items():
					scope_ids = tuple(date_ids[str(s.session_date)] for s in scope)
					for offset in range(0,len(candidates),batch_size):
						batch = candidates[offset:offset+batch_size]
						batch_ids = set(batch)
						scope_set = set(scope_ids)
						seen = np.zeros((len(sessions),len(batch)),dtype=bool)
						batch_index = {t:i for i,t in enumerate(batch)}
						quality_column = "state_id" if table==prices else "quality_flags"
						for day,ticker,value,flag in rows(f"SELECT date_id,ticker_id,{column},{quality_column} FROM {table} WHERE ticker_id IN ({marks(batch)}) AND date_id IN ({marks(scope_ids)}) AND {predicate} ORDER BY date_id,ticker_id", (*batch,*scope_ids), f"{key} load batch {offset//batch_size+1}"):
							day,ticker = int(day),int(ticker)
							if day not in scope_set or ticker not in batch_ids:
								raise ValueError("Bulk query returned an unexpected session/ticker key")
							i,j = date_index[day],ticker_index[ticker]
							if seen[i,batch_index[ticker]]:
								raise ValueError("Bulk query returned a duplicate session/ticker key")
							seen[i,batch_index[ticker]] = True
							value = float(value)
							if not np.isfinite(value):
								raise ValueError("Bulk query returned a non-finite value")
							arrays[key][i,j] = value
							quality[i,j] |= int(QualityFlag.INTERPOLATED_SINGLE) if table==prices and int(flag)==4 else 0 if table==prices else int(flag)
						if int(seen.sum()) != len(scope_ids)*len(batch):
							raise ValueError("Bulk query omitted a selected session/ticker key")
		with timer.phase("history identity"):
			selected_features = replace(features,ticker_ids=candidates)
			selection_result = {"schema":"prepared-universe-1", "selection_mode":"explicit_subset" if settings.tickers or settings.ticker_ids else "stable_id", "ticker_ids":list(candidates), "ticker_labels":[identities[t] for t in candidates], "candidate_count":len(original), "selected_count":len(candidates), "exclusions":exclusions, "excluded_ids":[t for t in original if t not in set(candidates)], "excluded_labels":[identities[t] for t in original if t not in set(candidates)], "coverage_required_sessions":len(sessions)}
			history = PreparedHistory(source.identity,calendar,sessions,selected_features,target,arrays,quality,selection_result,timer.values)
			selection_result["data_version"] = getattr(history,"revision")
	return history
