from dataclasses import dataclass

from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.recommendations.eligibility import EligibilityState
from stockguidance.simulation.prices import ExecutionFrame
from stockguidance.simulation.score_source import DecisionForecast
from stockguidance.targets.construction import close_to_close_return
from stockguidance.targets.specification import TargetSpec


@dataclass(frozen=True)
class BenchmarkEntry:
	timing: DecisionTiming
	eligible_tickers: tuple[int, ...]
	selected_tickers: tuple[int, ...]
	entry_prices: ExecutionFrame


@dataclass(frozen=True)
class BenchmarkResult:
	entry: BenchmarkEntry
	returns: tuple[tuple[int, float | None], ...]
	equal_weight_return: float | None
	selected_return: float | None
	selected_percentiles: tuple[tuple[int, float | None], ...]
	complete: bool


def benchmark_entry(forecast: DecisionForecast, frame: ExecutionFrame) -> BenchmarkEntry:
	return BenchmarkEntry(forecast.sample.timing, tuple(item.ticker_id for item in forecast.prediction.eligibility if item.state is EligibilityState.ELIGIBLE), tuple(item.ticker_id for item in forecast.prediction.ranking), frame)


def evaluate_benchmark(entry: BenchmarkEntry, exit_prices: ExecutionFrame | None, target: TargetSpec) -> BenchmarkResult:
	if exit_prices is not None and (exit_prices.session != entry.timing.exit_session or exit_prices.source != entry.entry_prices.source):
		raise ValueError("Benchmark requires the exact next-session close and source")
	entry_prices = {item.ticker_id: item.price for item in entry.entry_prices.quotes}
	exits = {} if exit_prices is None else {item.ticker_id: item.price for item in exit_prices.quotes}
	returns = tuple((ticker_id, close_to_close_return(float(entry_prices[ticker_id]), float(exits[ticker_id]), target) if ticker_id in entry_prices and ticker_id in exits else None) for ticker_id in entry.eligible_tickers)
	values = dict(returns)
	complete = bool(returns) and all(value is not None for _, value in returns)
	pool = tuple(value for _, value in returns if value is not None)
	selected = tuple(values[ticker_id] for ticker_id in entry.selected_tickers)
	selected_return = sum(value for value in selected if value is not None) / len(selected) if selected and all(value is not None for value in selected) else None
	percentiles = []
	for ticker_id in entry.selected_tickers:
		value = values[ticker_id]
		percentile = None if not complete or value is None else sum(candidate < value for candidate in pool) / len(pool)
		percentiles.append((ticker_id, percentile))
	return BenchmarkResult(entry, returns, sum(pool) / len(pool) if complete else None, selected_return, tuple(percentiles), complete)
