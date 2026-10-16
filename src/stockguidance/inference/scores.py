from stockguidance.artifacts.ensembles import LoadedEnsemble
from stockguidance.artifacts.loading import LoadedModel
from stockguidance.datasets.sample import Sample
from stockguidance.inference.prediction import predict_sample
from stockguidance.recommendations.result import RankedTicker


def score_sample(artifact: LoadedModel | LoadedEnsemble, sample: Sample) -> tuple[RankedTicker, ...]:
	scores = predict_sample(artifact, sample)
	if len(scores) != len(sample.features.specification.ticker_ids):
		raise ValueError("Scores must match the exact ordered ticker universe")
	return tuple(RankedTicker(ticker_id, score) for ticker_id, score in zip(sample.features.specification.ticker_ids, scores))
