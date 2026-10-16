from dataclasses import asdict

from stockguidance.artifacts.contracts import ArtifactReference
from stockguidance.artifacts.loading import load_artifact


def artifact_payload(reference: ArtifactReference) -> dict[str, object]:
	loaded = load_artifact(reference)
	metadata = loaded.metadata
	training = metadata.training
	return {"schema": "stockguidance-artifact-inspection-1", "artifact_id": loaded.artifact_id, "source": asdict(metadata.semantics.source), "features": asdict(metadata.semantics.features), "target": {**asdict(metadata.semantics.target), "formula": metadata.semantics.target.raw_formula}, "model": asdict(metadata.model), "framework_version": metadata.framework_version, "training_cutoff": training.training_cutoff.isoformat(), "information_cutoff": training.information_cutoff.isoformat(), "last_training_exit": training.last_exit.isoformat(), "data_revision": training.data_revision, "parameters": [{"name": name, "shape": list(parameter.shape), "elements": parameter.numel()} for name, parameter in loaded.model.named_parameters()]}
