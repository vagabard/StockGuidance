from stockguidance.artifacts.contracts import ArtifactReference, ModelSemantics
from stockguidance.artifacts.ensembles import LoadedEnsemble, load_ensemble
from stockguidance.artifacts.loading import LoadedModel, load_artifact
from stockguidance.calendar.decisions import DecisionTiming
from stockguidance.calendar.protocols import SessionCalendar
from stockguidance.market_data.availability import AvailabilityPolicy
from stockguidance.recommendations.configuration import ArtifactKind, RecommendationJob


def load_recommendation_artifact(reference: ArtifactReference, kind: ArtifactKind) -> LoadedModel | LoadedEnsemble:
	return load_ensemble(reference) if kind is ArtifactKind.ENSEMBLE else load_artifact(reference)


def require_compatible_artifact(artifact: LoadedModel | LoadedEnsemble, job: RecommendationJob, timing: DecisionTiming, calendar: SessionCalendar) -> None:
	semantics = artifact.metadata.semantics if isinstance(artifact, LoadedModel) else artifact.semantics
	if semantics.availability_policy is not AvailabilityPolicy.STRICT:
		raise ValueError("Retrospective research artifacts are not supported for production recommendations")
	if semantics != ModelSemantics(job.source.identity, job.features, job.target, calendar.calendar_id, calendar.version, availability_policy=job.availability_policy):
		raise ValueError("Recommendation artifact source/feature/target/ticker/calendar specifications differ")
	members = (artifact,) if isinstance(artifact, LoadedModel) else artifact.members
	if any(member.metadata.training.information_cutoff > timing.decision_cutoff for member in members):
		raise ValueError("Artifact information cutoff follows the recommendation decision")
