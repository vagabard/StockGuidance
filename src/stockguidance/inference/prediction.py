from stockguidance.artifacts.ensembles import LoadedEnsemble
from stockguidance.artifacts.loading import LoadedModel
from stockguidance.datasets.sample import Sample


def predict_sample(artifact: LoadedModel | LoadedEnsemble, sample: Sample) -> tuple[float, ...]:
	if not isinstance(artifact, (LoadedModel, LoadedEnsemble)) or not isinstance(sample, Sample):
		raise ValueError("Prediction requires a loaded artifact and a shared sample")
	semantics = artifact.metadata.semantics if isinstance(artifact, LoadedModel) else artifact.semantics
	members = (artifact,) if isinstance(artifact, LoadedModel) else artifact.members
	weights = (1.0,) if isinstance(artifact, LoadedModel) else artifact.weights
	if sample.source != semantics.source or sample.features.specification != semantics.features or sample.target_specification != semantics.target or sample.timing.entry_session.calendar_id != semantics.calendar_id or sample.calendar_version != semantics.calendar_version:
		raise ValueError("Sample and artifact feature/target/ticker/source/calendar semantics differ")
	if any(member.metadata.training.information_cutoff > sample.timing.decision_cutoff for member in members):
		raise ValueError("Model information cutoff follows this historical decision")
	if sample.features.values is None:
		raise ValueError("Sample features are unavailable")

	import torch

	import numpy as np

	values = np.asarray(sample.features.values, dtype=np.float64).reshape(1, -1)
	preprocessing = members[0].metadata.preprocessing if members[0].metadata.preprocessing is not None else None
	if preprocessing is not None:
		preprocessing.validate_for(sample.features.specification, sample.target_specification)
		values = preprocessing.transform_inputs(values)
	inputs = torch.tensor(values, dtype=torch.float32)
	if inputs.shape != (1, semantics.features.input_size) or not torch.isfinite(inputs).all():
		raise ValueError("Invalid inference input layout or values")
	scores = torch.zeros((1, len(semantics.features.ticker_ids)), dtype=torch.float32)
	with torch.inference_mode():
		for member, weight in zip(members, weights):
			member.model.eval()
			outputs = member.model(inputs)
			if not isinstance(outputs, torch.Tensor) or outputs.shape != scores.shape or not torch.isfinite(outputs).all():
				raise ValueError("Model emitted invalid inference outputs")
			scores += weight * outputs
	if not torch.isfinite(scores).all():
		raise ValueError("Ensemble emitted non-finite outputs")
	if preprocessing is not None:
		inverted = preprocessing.inverse_outputs(scores.cpu().numpy())
		return tuple(float(value) for value in inverted[0])
	return tuple(float(value) for value in scores[0])
