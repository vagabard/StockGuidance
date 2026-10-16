import json
from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.artifacts.decoding import exact_mapping
from stockguidance.market_data.identity import mapping, text_field
from stockguidance.models.policy import ModelRefreshPolicy
from stockguidance.recommendations.configuration import ArtifactKind
from stockguidance.recommendations.jobs import load_configuration as load_recommendation
from stockguidance.recommendations.options import RecommendationOptions
from stockguidance.simulation.configuration import EvaluationPolicy
from stockguidance.simulation.jobs import load_configuration as load_simulation
from stockguidance.simulation.options import SimulationOptions
from stockguidance.training.jobs import load_job
from stockguidance.workflows.batch import BatchPlan, BatchStep
from stockguidance.workflows.batch_tasks import BatchTask, ProducedArtifact, RecommendationTask, SimulationTask, TrainingTask


def artifact_selection(value: object) -> ArtifactReference | ProducedArtifact:
	fields = mapping(value)
	if set(fields) == {"from_step"}:
		return ProducedArtifact(text_field(fields, "from_step"))
	fields = exact_mapping(fields, ("path", "artifact_id"))
	return ArtifactReference(Path(text_field(fields, "path")), text_field(fields, "artifact_id"))


def load_batch(path: Path) -> BatchPlan:
	fields = exact_mapping(json.loads(path.read_text(encoding="utf-8")), ("schema", "steps"))
	if fields["schema"] != "stockguidance-batch-1" or not isinstance(fields["steps"], list):
		raise ValueError("Unsupported batch schema")
	steps: list[BatchStep] = []
	for value in fields["steps"]:
		step = exact_mapping(value, ("name", "kind", "config", "report", "options"))
		configuration = Path(text_field(step, "config"))
		kind = text_field(step, "kind")
		options = mapping(step["options"])
		task: BatchTask
		if kind == "train":
			options = exact_mapping(options, ("artifact", "checkpoint", "resume"))
			task = TrainingTask(load_job(configuration), Path(text_field(options, "artifact")), None if options["checkpoint"] is None else Path(text_field(options, "checkpoint")), None if options["resume"] is None else Path(text_field(options, "resume")))
		elif kind == "recommend":
			options = exact_mapping(options, ("policy", "artifact", "artifact_kind", "portfolio_state", "capital", "fee_per_fill"))
			policy = ModelRefreshPolicy(text_field(options, "policy"))
			selection = Path(text_field(options, "artifact")) if policy is ModelRefreshPolicy.RETRAIN else artifact_selection(options["artifact"])
			portfolio = None if options["portfolio_state"] is None else Path(text_field(options, "portfolio_state"))
			capital = None if options["capital"] is None else text_field(options, "capital")
			artifact_kind = ArtifactKind(text_field(options, "artifact_kind"))
			job = load_recommendation(configuration)
			if (job.retraining is not None) != (policy is ModelRefreshPolicy.RETRAIN):
				raise ValueError("Recommendation policy and configuration differ")
			if not isinstance(selection, ProducedArtifact):
				RecommendationOptions(policy, selection.path if isinstance(selection, ArtifactReference) else selection, selection.artifact_id if isinstance(selection, ArtifactReference) else None, artifact_kind, portfolio_state=portfolio, capital=capital, fee_per_fill=text_field(options, "fee_per_fill"))
			task = RecommendationTask(job, policy, selection, artifact_kind, portfolio, capital, text_field(options, "fee_per_fill"))
		elif kind == "simulate":
			options = exact_mapping(options, ("model_policy", "artifact", "artifact_kind"))
			model_policy = EvaluationPolicy(text_field(options, "model_policy"))
			selected = Path(text_field(options, "artifact")) if model_policy is EvaluationPolicy.CHRONOLOGICAL_RETRAIN else artifact_selection(options["artifact"])
			simulation = load_simulation(configuration)
			if (simulation.retraining is not None) != (model_policy is EvaluationPolicy.CHRONOLOGICAL_RETRAIN):
				raise ValueError("Evaluation policy and configuration differ")
			artifact_kind = ArtifactKind(text_field(options, "artifact_kind"))
			if not isinstance(selected, ProducedArtifact):
				SimulationOptions(model_policy, selected.path if isinstance(selected, ArtifactReference) else None, selected.artifact_id if isinstance(selected, ArtifactReference) else None, artifact_kind, selected if isinstance(selected, Path) else None)
			task = SimulationTask(simulation, model_policy, selected, artifact_kind)
		else:
			raise ValueError("Unsupported batch step")
		steps.append(BatchStep(text_field(step, "name"), task, Path(text_field(step, "report"))))
	return BatchPlan(tuple(steps))
