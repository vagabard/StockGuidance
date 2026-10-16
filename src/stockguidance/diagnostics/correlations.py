import statistics
from dataclasses import asdict

from stockguidance.datasets.training import TrainingDataset
from stockguidance.market_data.identity import content_id


def target_correlations(dataset: TrainingDataset) -> dict[str, object]:
	if not dataset.samples:
		raise ValueError("Correlations require an explicit sample universe")
	tickers = dataset.samples[0].features.specification.ticker_ids
	values: list[tuple[float, ...]] = []
	for sample in dataset.eligible:
		if any(value is None for value in sample.targets.raw):
			raise ValueError("Correlation inputs require complete mature outcomes")
		values.append(tuple(value for value in sample.targets.raw if value is not None))
	rows: list[dict[str, object]] = []
	for left_index, left in enumerate(tickers):
		for right_index, right in enumerate(tickers):
			coefficient: float | None = None
			if len(values) >= 2:
				try:
					coefficient = statistics.correlation([row[left_index] for row in values], [row[right_index] for row in values])
				except statistics.StatisticsError:
					pass
			rows.append({"left_ticker_id": left, "right_ticker_id": right, "correlation": coefficient})
	return {"schema": "stockguidance-target-correlations-1", "source": asdict(dataset.samples[0].source), "features": asdict(dataset.samples[0].features.specification), "target_formula": "close(T+1) / close(T) - 1", "ticker_ids": list(tickers), "mature_samples": len(values), "excluded_samples": len(dataset.excluded), "training_cutoff": dataset.training_cutoff.isoformat(), "data_revision": content_id([sample.cache_key for sample in dataset.samples]), "undefined_policy": "null_for_insufficient_samples_or_zero_variance", "correlations": rows}
