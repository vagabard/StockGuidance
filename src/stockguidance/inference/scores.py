from stockguidance.artifacts.ensembles import LoadedEnsemble
from stockguidance.artifacts.loading import LoadedModel
from stockguidance.datasets.sample import Sample
from stockguidance.inference.prediction import predict_sample
from stockguidance.recommendations.result import RankedTicker


def score_sample(artifact: LoadedModel | LoadedEnsemble, sample: Sample, symbols: tuple[str, ...] | None = None) -> tuple[RankedTicker, ...]:
	scores = predict_sample(artifact, sample)
	if len(scores) != len(sample.features.specification.ticker_ids):
		raise ValueError("Scores must match the exact ordered ticker universe")
	if symbols is not None and (len(symbols) != len(scores) or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols)):
		raise ValueError("Symbols must match the exact ordered ticker universe")
	return tuple(RankedTicker(ticker_id, score, None if symbols is None else symbol) for ticker_id, score, symbol in zip(sample.features.specification.ticker_ids, scores, (None,) * len(scores) if symbols is None else symbols))
