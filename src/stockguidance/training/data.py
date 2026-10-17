from stockguidance.artifacts.contracts import ModelSemantics
from stockguidance.datasets.training import TrainingDataset
from stockguidance.market_data.identity import content_id


def dataset_semantics(dataset: TrainingDataset) -> ModelSemantics:
	if not isinstance(dataset, TrainingDataset) or not dataset.eligible:
		raise ValueError("Training requires complete features and mature close-to-close outcomes")
	first = dataset.eligible[0]
	return ModelSemantics(first.source, first.features.specification, first.target_specification, first.timing.entry_session.calendar_id, first.calendar_version, availability_policy=first.availability_policy)


def dataset_revision(dataset: TrainingDataset) -> str:
	return content_id({"schema": "training-dataset-1", "cutoff": dataset.training_cutoff.isoformat(), "data_snapshot": None if dataset.data_snapshot is None else dataset.data_snapshot.isoformat(), "samples": [sample.cache_key for sample in dataset.samples]})


def validate_chronology(training: TrainingDataset, evaluation: TrainingDataset | None) -> ModelSemantics:
	semantics = dataset_semantics(training)
	for sample in training.samples:
		if sample.timing.decision_cutoff > training.training_cutoff or sample.timing.entry_session.calendar_id != semantics.calendar_id:
			raise ValueError("Training sample timing/calendar disagrees with fitting cutoff")
		if sample.training_eligible and sample.timing.exit_session.closes_at > training.training_cutoff:
			raise ValueError("Training outcomes must mature by the fitting cutoff")
	if evaluation is not None:
		if dataset_semantics(evaluation) != semantics:
			raise ValueError("Evaluation feature/target/ticker/source semantics differ")
		if semantics.availability_policy.retrospective and evaluation.data_snapshot != training.data_snapshot:
			raise ValueError("Training and evaluation must use one data snapshot")
		if evaluation.samples[0].timing.entry_session.sequence <= training.samples[-1].timing.entry_session.sequence:
			raise ValueError("Evaluation must follow the entire requested training period")
		if training.training_cutoff > min(sample.timing.decision_cutoff for sample in evaluation.samples):
			raise ValueError("Fitting cutoff must precede every held-out evaluation decision")
	return semantics
