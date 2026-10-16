from dataclasses import dataclass

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
	scores = score_sample(artifact, sample)
	ranking = rank_scores(scores, eligibility, top_k)
	members = (artifact,) if isinstance(artifact, LoadedModel) else artifact.members
	weights = (1.0,) if isinstance(artifact, LoadedModel) else artifact.weights
	revision = content_id({"features": sample.feature_revision, "eligibility": eligibility_revision(eligibility)})
	return RankedPrediction(scores, eligibility, ranking, revision, tuple(member.artifact_id for member in members), weights)
