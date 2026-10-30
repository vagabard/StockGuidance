from dataclasses import dataclass
from statistics import median

from stockguidance.artifacts.ensembles import LoadedEnsemble
from stockguidance.artifacts.loading import LoadedModel
from stockguidance.datasets.sample import Sample
from stockguidance.inference.scores import score_sample
from stockguidance.market_data.identity import content_id
from stockguidance.recommendations.eligibility import TickerEligibility, eligibility_revision
from stockguidance.recommendations.ranking import rank_scores
from stockguidance.recommendations.result import RankedTicker


@dataclass(frozen=True)
class RankedPrediction:
	scores: tuple[RankedTicker, ...]
	eligibility: tuple[TickerEligibility, ...]
	ranking: tuple[RankedTicker, ...]
	data_revision: str
	member_artifact_ids: tuple[str, ...]
	member_weights: tuple[float, ...]


def predict_ranking(artifact: LoadedModel | LoadedEnsemble, sample: Sample, eligibility: tuple[TickerEligibility, ...], top_k: int) -> RankedPrediction:
	metadata = artifact.metadata if isinstance(artifact, LoadedModel) else artifact.members[0].metadata
	universe = metadata.universe
	if universe is None:
		if metadata.semantics.target.version != "raw_return-v1":
			raise ValueError("Versioned recommendation artifacts require frozen ticker labels")
		symbols = None
	else:
		if universe.ticker_ids != sample.features.specification.ticker_ids:
			raise ValueError("Artifact universe order differs from the inference sample")
		symbols = universe.ticker_labels
	# Preserve the v1 two-argument adapter for legacy artifacts; versioned
	# artifacts use the symbol-aware path so their stable labels are retained.
	scores = score_sample(artifact, sample) if symbols is None else score_sample(artifact, sample, symbols)
	ranking = rank_scores(scores, eligibility, top_k)
	members = (artifact,) if isinstance(artifact, LoadedModel) else artifact.members
	weights = (1.0,) if isinstance(artifact, LoadedModel) else artifact.weights
	revision = content_id({"features": sample.feature_revision, "eligibility": eligibility_revision(eligibility)})
	return RankedPrediction(scores, eligibility, ranking, revision, tuple(member.artifact_id for member in members), weights)


def recommendation_statistics(scores: tuple[RankedTicker, ...], eligibility: tuple[TickerEligibility, ...], ranking: tuple[RankedTicker, ...]) -> tuple[int, float | None, float | None]:
	"""Return eligible count and the two required target-space medians."""

	by_id = {item.ticker_id: item.score for item in scores}
	eligible_ids = {item.ticker_id for item in eligibility if item.state.value == "eligible"}
	eligible_scores = [by_id[ticker_id] for ticker_id in eligible_ids]
	returned_scores = [item.score for item in ranking]
	return len(eligible_scores), median(eligible_scores) if eligible_scores else None, median(returned_scores) if returned_scores else None
