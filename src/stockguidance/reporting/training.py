from dataclasses import asdict

from stockguidance.training.result import TrainingResult


def training_payload(result: TrainingResult) -> dict[str, object]:
	return {
		"schema": "stockguidance-training-1", "successful": True,
		"artifact": {"path": str(result.artifact.path), "artifact_id": result.artifact.artifact_id},
		"completed_epochs": result.completed_epochs, "training_samples": result.training_samples,
		"training_mae": result.training_mae, "training_metric_scope": "in_sample",
		"evaluation_kind": result.evaluation_kind.value, "evaluation_mae": result.evaluation_mae,
		"evaluation_samples": result.evaluation_samples, "evaluation_revision": result.evaluation_revision,
		"evaluation_cutoff": None if result.evaluation_cutoff is None else result.evaluation_cutoff.isoformat(),
		"availability_policy": result.availability_policy.value,
		"availability_assumption": result.availability_policy.assumption,
		"point_in_time_replay": not result.availability_policy.retrospective,
		"data_snapshot": None if result.data_snapshot is None else result.data_snapshot.isoformat(),
		"data_revision": result.data_revision,
		"excluded_training": [{**asdict(item), "entry_session": item.entry_session.isoformat()} for item in result.excluded_training],
		"excluded_evaluation": [{**asdict(item), "entry_session": item.entry_session.isoformat()} for item in result.excluded_evaluation],
		"epochs": [asdict(item) for item in result.epochs], "device": result.device, "precision": result.precision,
	}
