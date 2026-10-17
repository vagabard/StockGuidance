from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import torch

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.sessions import TradingSession
from stockguidance.config.source import PriceAdjustment, SourceIdentity
from stockguidance.datasets.numpy_arrays import validate_arrays
from stockguidance.database.repositories.market_data import ImportReceipt, SessionSnapshot
from stockguidance.market_data.identity import encode_json
from stockguidance.market_data.legacy_derived import LegacyDerivedRow, legacy_rows
from stockguidance.market_data.manifests import decode_response, response_payload
from stockguidance.market_data.records import DailyBar, records_id
from stockguidance.market_data.providers.contracts import ProviderResponse, ResponseState
from stockguidance.models.mlp import mlp_type
from stockguidance.portfolio.marks import ClosingMark
from stockguidance.portfolio.planning import plan_orders
from stockguidance.portfolio.policy import AllocationPolicy, AllocationSpec
from stockguidance.portfolio.precision import ExecutionPolicy
from stockguidance.portfolio.schedule import scheduled_allocation
from stockguidance.portfolio.state import initial_portfolio
from stockguidance.portfolio.state import PortfolioState
from stockguidance.portfolio.ledger import release_proceeds
from stockguidance.portfolio.valuation import value_portfolio
from stockguidance.recommendations.result import RankedTicker
from stockguidance.simulation.events import EventKind, SimulationEvent
from stockguidance.simulation.execution import fill_orders, sell_positions
from stockguidance.simulation.prices import ExecutionFrame
from stockguidance.targets.specification import TargetSpec
from stockguidance.features.specification import FeatureSpec


@dataclass(frozen=True)
class SizeSpec:
	name: str
	sessions: int
	tickers: int


@dataclass(frozen=True)
class TimedOperation:
	name: str
	work_units: int
	cold_seconds: float
	warm_median_seconds: float
	cold_throughput: float
	warm_throughput: float
	cold_peak_python_bytes: int
	warm_peak_python_bytes: int
	cold_resident_delta_bytes: int | None
	warm_resident_delta_bytes: int | None


@dataclass(frozen=True)
class Fixture:
	spec: SizeSpec
	sessions: tuple[TradingSession, ...]
	ticker_ids: tuple[int, ...]
	bars: tuple[tuple[DailyBar, ...], ...]
	json_payloads: tuple[bytes, ...]
	raw_rows: tuple[tuple[object, ...], ...]
	derived_rows: tuple[tuple[object, ...], ...]
	feature_rows: tuple[tuple[float, ...], ...]
	target_rows: tuple[tuple[float, ...], ...]
	frames: tuple[ExecutionFrame, ...]
	fixture_identity: str


class FixtureCalendar:
	def __init__(self, sessions: tuple[TradingSession, ...]) -> None:
		self._sessions = sessions
		self._by_date = {session.session_date: session for session in sessions}

	def session(self, session_date: date) -> TradingSession:
		return self._by_date[session_date]

	@property
	def calendar_id(self) -> str:
		return "stage6-fixture-calendar"

	@property
	def version(self) -> str:
		return "stage6-fixture-v1"

	@property
	def timezone(self) -> ZoneInfo:
		return ZoneInfo("UTC")

	@property
	def start(self) -> date:
		return self._sessions[0].session_date

	@property
	def end(self) -> date:
		return self._sessions[-1].session_date

	def shift(self, session: TradingSession, offset: int) -> TradingSession:
		index = self._sessions.index(session) + offset
		return self._sessions[index]

	def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
		return tuple(session for session in self._sessions if start <= session.session_date <= end)


def _decimal(value: Decimal) -> Decimal:
	return value.quantize(Decimal("0.0001"))


def _make_sessions(count: int) -> tuple[TradingSession, ...]:
	sessions: list[TradingSession] = []
	current = date(2020, 1, 2)
	while len(sessions) < count:
		if current.weekday() < 5:
			opens = datetime(current.year, current.month, current.day, 14, 30, tzinfo=timezone.utc)
			closes = datetime(current.year, current.month, current.day, 21, tzinfo=timezone.utc)
			sessions.append(TradingSession("stage6-fixture-calendar", current, len(sessions), opens, closes))
		current += timedelta(days=1)
	return tuple(sessions)


def _make_bar(session_index: int, ticker_index: int) -> DailyBar:
	base = Decimal("40.0000") + Decimal(ticker_index % 37) + Decimal(session_index) / Decimal("100")
	opening = _decimal(base)
	closing = _decimal(base + Decimal(((session_index + ticker_index) % 7) - 3) / Decimal("100"))
	high = _decimal(max(opening, closing) + Decimal("0.2500"))
	low = _decimal(min(opening, closing) - Decimal("0.2500"))
	vwap = _decimal((opening + closing) / Decimal("2"))
	return DailyBar(f"T{ticker_index + 1:06d}", opening, high, low, closing, 100_000 + session_index * 10 + ticker_index, vwap, 500 + ticker_index)


def _make_response_payloads(bars: tuple[tuple[DailyBar, ...], ...]) -> tuple[bytes, ...]:
	payloads = []
	for session_bars in bars:
		response = ProviderResponse(ResponseState.COMPLETE, session_bars, 200, "OK", (("resultsCount", len(session_bars)),))
		payloads.append(encode_json(response_payload(response)))
	return tuple(payloads)


def _make_fixture(spec: SizeSpec, seed: int, fixture_identity: str) -> Fixture:
	if seed != 20261016:
		raise ValueError("Stage 6 fixtures use the pinned seed")
	sessions = _make_sessions(spec.sessions)
	ticker_ids = tuple(range(1, spec.tickers + 1))
	bars = tuple(tuple(_make_bar(index, ticker) for ticker in range(spec.tickers)) for index in range(spec.sessions))
	raw_rows = tuple(
		(index + 1, bar.ticker, str(bar.open), str(bar.high), str(bar.low), str(bar.close), bar.volume, str(bar.vwap) if bar.vwap is not None else None, bar.transactions)
		for index, session_bars in enumerate(bars)
		for bar in session_bars
	)
	derived_rows: list[tuple[object, ...]] = []
	for index in range(1, spec.sessions - 1):
		for row in legacy_rows(bars[index - 1], bars[index], bars[index + 1]):
			derived_rows.append((index + 1, row.ticker, *row.values))
	feature_rows = tuple(
		tuple(float((session_index + 1) * (ticker_index + 1)) / 1000.0 for ticker_index in range(spec.tickers))
		for session_index in range(spec.sessions - 2)
	)
	target_rows = tuple(
		tuple(float(((session_index + ticker_index) % 19) - 9) / 100.0 for ticker_index in range(spec.tickers))
		for session_index in range(spec.sessions - 2)
	)
	source = SourceIdentity("stage6-fixture", "offline", "synthetic", "stage6", PriceAdjustment.ADJUSTED)
	frames = tuple(
		ExecutionFrame(
			source,
			session,
			ticker_ids,
			tuple(ClosingMark(ticker_id, session, _decimal(Decimal("40") + Decimal(ticker_id % 37) + Decimal(index) / Decimal("100")), f"fixture-{index}-{ticker_id}") for ticker_id in ticker_ids),
		)
		for index, session in enumerate(sessions)
	)
	return Fixture(spec, sessions, ticker_ids, bars, _make_response_payloads(bars), raw_rows, tuple(derived_rows), feature_rows, target_rows, frames, fixture_identity)


def _rss_bytes() -> int | None:
	try:
		import importlib
		psutil: Any = importlib.import_module("psutil")
	except ImportError:
		return None
	return int(psutil.Process(os.getpid()).memory_info().rss)


def _timed_call(operation: Callable[[], int]) -> tuple[float, int, int, int | None]:
	gc.collect()
	tracemalloc.start()
	before_rss = _rss_bytes()
	started = time.perf_counter()
	result = operation()
	elapsed = time.perf_counter() - started
	_, peak_python = tracemalloc.get_traced_memory()
	after_rss = _rss_bytes()
	tracemalloc.stop()
	resident_delta = None if before_rss is None or after_rss is None else max(0, after_rss - before_rss)
	return elapsed, result, peak_python, resident_delta


def _measure(name: str, units: int, operation: Callable[[], int], repeats: int) -> TimedOperation:
	cold_seconds, cold_result, cold_peak, cold_rss = _timed_call(operation)
	warm = [_timed_call(operation) for _ in range(repeats)]
	if any(result != cold_result for _, result, _, _ in warm):
		raise ValueError(f"Non-deterministic benchmark result for {name}")
	warm_seconds = statistics.median(item[0] for item in warm)
	warm_peak = max(item[2] for item in warm)
	warm_rss = max((item[3] for item in warm if item[3] is not None), default=None)
	return TimedOperation(
		name,
		units,
		cold_seconds,
		warm_seconds,
		units / cold_seconds if cold_seconds else 0.0,
		units / warm_seconds if warm_seconds else 0.0,
		cold_peak,
		warm_peak,
		cold_rss,
		warm_rss,
	)


def _json_decode_and_hash(fixture: Fixture) -> int:
	result = 0
	for payload in fixture.json_payloads:
		result ^= int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
		decoded = decode_response(json.loads(payload))
		result ^= len(decoded.records)
	if result < 0:
		raise ValueError("Unexpected fixture digest")
	return result


def _raw_derived_reconciliation(fixture: Fixture) -> int:
	decoded_raw: dict[int, list[DailyBar]] = {}
	for row in fixture.raw_rows:
		date_id, bar = _decode_raw_row(row)
		decoded_raw.setdefault(date_id, []).append(bar)
	decoded_derived: dict[int, list[LegacyDerivedRow]] = {}
	for row in fixture.derived_rows:
		date_id, decoded_derived_row = _decode_derived_row(row)
		decoded_derived.setdefault(date_id, []).append(decoded_derived_row)
	checks = 0
	for index in range(1, fixture.spec.sessions - 1):
		date_id = index + 1
		records = tuple(decoded_raw[date_id])
		derived_records = tuple(decoded_derived[date_id])
		receipt = ImportReceipt(records_id(records), fixture.sessions[index].closes_at + timedelta(minutes=15))
		snapshot = SessionSnapshot(fixture.sessions[index], date_id, records, derived_records, receipt)
		if not _derived_values_match(snapshot, tuple(decoded_raw[date_id - 1]), tuple(decoded_raw[date_id + 1])):
			raise ValueError("Synthetic derived reconciliation failed")
		checks += len(records)
	return checks


def _decode_raw_row(row: tuple[object, ...]) -> tuple[int, DailyBar]:
	from stockguidance.database.repositories.mysql_market_read import decode_raw_row
	return decode_raw_row(row)


def _decode_derived_row(row: tuple[object, ...]) -> tuple[int, LegacyDerivedRow]:
	from stockguidance.database.repositories.mysql_market_read import decode_derived_row
	return decode_derived_row(row)


def _derived_values_match(snapshot: SessionSnapshot, prior: tuple[DailyBar, ...], following: tuple[DailyBar, ...]) -> bool:
	from stockguidance.validation.derived import derived_values_match
	return derived_values_match(snapshot, prior, following)


def _tensor_construction(fixture: Fixture) -> int:
	specification = FeatureSpec(fixture.ticker_ids, 1)
	features = np.asarray(fixture.feature_rows, dtype=np.float64)
	targets = np.asarray(fixture.target_rows, dtype=np.float64)
	validate_arrays(features, targets, specification)
	inputs = torch.tensor(features, dtype=torch.float32)
	labels = torch.tensor(targets, dtype=torch.float32)
	if inputs.shape != (len(fixture.feature_rows), len(fixture.ticker_ids)) or labels.shape != targets.shape:
		raise ValueError("Synthetic tensor layout failed")
	return int(inputs.numel() + labels.numel())


def _make_model(fixture: Fixture) -> torch.nn.Module:
	torch.manual_seed(20261016)
	model = mlp_type()(len(fixture.ticker_ids), len(fixture.ticker_ids), [min(64, max(8, len(fixture.ticker_ids) * 2))], num_layers=2, layer_normalization=False)
	model.eval()
	return model


def _single_sample_inference(fixture: Fixture, model: torch.nn.Module) -> int:
	result = 0.0
	with torch.inference_mode():
		for row in fixture.feature_rows:
			inputs = torch.tensor(row, dtype=torch.float32).reshape(1, -1)
			outputs = model(inputs)
			if outputs.shape != (1, len(fixture.ticker_ids)) or not torch.isfinite(outputs).all():
				raise ValueError("Synthetic inference output failed")
			result += float(outputs.sum())
	return int(abs(result) * 1_000_000)


def _batched_inference(fixture: Fixture, model: torch.nn.Module) -> int:
	inputs = torch.tensor(np.asarray(fixture.feature_rows, dtype=np.float64), dtype=torch.float32)
	with torch.inference_mode():
		outputs = model(inputs)
	if outputs.shape != (len(fixture.feature_rows), len(fixture.ticker_ids)) or not torch.isfinite(outputs).all():
		raise ValueError("Synthetic batched inference output failed")
	return int(outputs.numel())


def _portfolio_events(fixture: Fixture) -> int:
	calendar = FixtureCalendar(fixture.sessions)
	allocation = AllocationSpec(AllocationPolicy.STAGGERED_HALVES, fixture.sessions[0])
	state: PortfolioState = initial_portfolio(allocation, Decimal("100000.00"), fixture.sessions[0].closes_at)
	execution = ExecutionPolicy(Decimal("0.50"))
	target = TargetSpec()
	ranking = tuple(RankedTicker(ticker_id, float(len(fixture.ticker_ids) - index)) for index, ticker_id in enumerate(fixture.ticker_ids[: min(8, len(fixture.ticker_ids))]))
	event_count = 0
	trade_count = 0
	for index in range(1, len(fixture.sessions) - 1):
		session = fixture.sessions[index]
		previous = fixture.sessions[index - 1]
		next_session = fixture.sessions[index + 1]
		state = release_proceeds(state, session, session.opens_at)
		decision_cutoff = previous.closes_at + timedelta(minutes=1)
		state = replace(state, as_of=decision_cutoff)
		timing = DecisionTiming(previous, session, next_session, decision_cutoff)
		SimulationEvent(EventKind.DECISION, decision_cutoff, session, "Stage 6 benchmark decision", portfolio=state)
		plan = plan_orders(state, allocation, timing, ranking, execution)
		state, sale_events, trades = sell_positions(state, fixture.frames[index], calendar, execution, target)
		trade_count += len(trades)
		if plan.orders:
			state, buy_events = fill_orders(state, plan, fixture.frames[index])
			event_count += len(buy_events)
		event_count += len(sale_events)
		value = value_portfolio(state, session, {quote.ticker_id: quote for quote in fixture.frames[index].quotes})
		SimulationEvent(EventKind.MARK, session.closes_at, session, "Stage 6 benchmark mark", amount=value.total_equity, portfolio=state)
		event_count += 2
	return event_count + trade_count


def _operation_payload(operation: TimedOperation) -> dict[str, object]:
	return {
		"work_units": operation.work_units,
		"cold_seconds": operation.cold_seconds,
		"warm_median_seconds": operation.warm_median_seconds,
		"cold_throughput_per_second": operation.cold_throughput,
		"warm_throughput_per_second": operation.warm_throughput,
		"cold_peak_python_bytes": operation.cold_peak_python_bytes,
		"warm_peak_python_bytes": operation.warm_peak_python_bytes,
		"cold_resident_delta_bytes": operation.cold_resident_delta_bytes,
		"warm_resident_delta_bytes": operation.warm_resident_delta_bytes,
	}


def _size_report(fixture: Fixture, repeats: int) -> dict[str, object]:
	model = _make_model(fixture)
	rows = fixture.spec.sessions * fixture.spec.tickers
	checks = (fixture.spec.sessions - 2) * fixture.spec.tickers
	samples = fixture.spec.sessions - 2
	operations = (
		_measure("json_decode_and_hash", rows, lambda: _json_decode_and_hash(fixture), repeats),
		_measure("raw_derived_reconciliation", checks, lambda: _raw_derived_reconciliation(fixture), repeats),
		_measure("tensor_construction", samples * fixture.spec.tickers * 2, lambda: _tensor_construction(fixture), repeats),
		_measure("single_sample_inference", samples, lambda: _single_sample_inference(fixture, model), repeats),
		_measure("batched_inference_candidate", samples, lambda: _batched_inference(fixture, model), repeats),
		_measure("portfolio_event_processing", samples, lambda: _portfolio_events(fixture), repeats),
	)
	return {
		"name": fixture.spec.name,
		"sessions": fixture.spec.sessions,
		"tickers": fixture.spec.tickers,
		"rows": rows,
		"json_transfer_bytes": sum(len(payload) for payload in fixture.json_payloads),
		"results": {operation.name: _operation_payload(operation) for operation in operations},
	}


def _load_fixture_config(path: Path) -> tuple[int, tuple[SizeSpec, ...], int]:
	payload = json.loads(path.read_bytes())
	if payload.get("schema_version") != 1 or payload.get("seed") != 20261016:
		raise ValueError("Unsupported Stage 6 fixture specification")
	sizes = tuple(SizeSpec(item["name"], int(item["sessions"]), int(item["tickers"])) for item in payload["sizes"])
	if not sizes or any(item.sessions < 3 or item.tickers < 1 for item in sizes):
		raise ValueError("Stage 6 fixture sizes must have sessions and tickers")
	return int(payload["seed"]), sizes, int(payload.get("warm_repeats", 5))


def run(fixture_path: Path, repeats_override: int | None = None) -> dict[str, object]:
	seed, sizes, configured_repeats = _load_fixture_config(fixture_path)
	repeats = configured_repeats if repeats_override is None else repeats_override
	if repeats < 2:
		raise ValueError("At least two warm benchmark repeats are required")
	fixture_identity = hashlib.sha256(encode_json(json.loads(fixture_path.read_bytes()))).hexdigest()
	torch.set_num_threads(1)
	reports = []
	for size in sizes:
		reports.append(_size_report(_make_fixture(size, seed, fixture_identity), repeats))
	return {
		"benchmark": "stockguidance-stage6-offline-performance-v1",
		"fixture": {"path": fixture_path.as_posix(), "identity": fixture_identity, "seed": seed, "warm_repeats": repeats},
		"environment": {
			"python": sys.version,
			"python_executable": sys.executable,
			"numpy": np.__version__,
			"torch": torch.__version__,
			"torch_threads": torch.get_num_threads(),
			"platform": platform.platform(),
			"processor": platform.processor(),
			"cpu_count": os.cpu_count(),
		},
		"conditions": {"cold": "first timed call in this process after fixture construction", "warm": "median of repeated calls in the same process", "memory": "tracemalloc peak Python allocations; resident deltas are optional psutil samples"},
		"database": {"status": "absent", "reason": "No disposable SQL server was available; no production database was contacted", "query_count": None, "transfer_bytes": None},
		"sizes": reports,
	}


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Run the offline Stage 6 performance study")
	parser.add_argument("--fixture", type=Path, default=Path("benchmarks/fixtures/stage6.json"))
	parser.add_argument("--output", type=Path)
	parser.add_argument("--warm-repeats", type=int)
	arguments = parser.parse_args(argv)
	report = run(arguments.fixture, arguments.warm_repeats)
	serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
	if arguments.output is not None:
		arguments.output.parent.mkdir(parents=True, exist_ok=True)
		arguments.output.write_text(serialized, encoding="utf-8")
	print(serialized, end="")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
