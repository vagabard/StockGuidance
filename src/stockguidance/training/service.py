from dataclasses import asdict
from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactMetadata, ArtifactUniverse, ModelSemantics, TrainingProvenance
from stockguidance.artifacts.exporting import export_artifact
from stockguidance.datasets.training import TrainingDataset
from stockguidance.models.factory import build_model
from stockguidance.preprocessing import PreprocessingSpec, fit_training_preprocessing
from stockguidance.training.batches import make_loader
from stockguidance.training.checkpoints import checkpoint_identity, resume_checkpoint, save_checkpoint
from stockguidance.training.configuration import Precision, TrainingConfig
from stockguidance.training.data import dataset_revision, validate_chronology
from stockguidance.training.engine import evaluate_metrics, fit_epoch_metrics
from stockguidance.training.resources import create_optimizer, resolve_resources
from stockguidance.training.result import EpochMetrics, EvaluationKind, ExcludedSample, TrainingResult
from stockguidance.market_data.identity import content_id


def excluded_samples(dataset: TrainingDataset | None) -> tuple[ExcludedSample, ...]:
	return () if dataset is None else tuple(ExcludedSample(sample.timing.entry_session.session_date, sample.feature_state.value, sample.target_state.value, tuple(sorted({issue.state.value for issue in (*sample.features.issues, *sample.targets.issues)}))) for sample in dataset.excluded)


def semantics_identity(semantics: ModelSemantics) -> str:
	features = semantics.features.to_mapping() if hasattr(semantics.features, "to_mapping") else {
		"ticker_ids": list(semantics.features.ticker_ids), "lookback_sessions": semantics.features.lookback_sessions,
		"signal_mode": semantics.features.signal_mode.value, "interval": semantics.features.interval.value,
		"missing_history": semantics.features.missing_history.value, "feature_order": [item.value for item in semantics.features.feature_order],
		"adjustment": semantics.features.adjustment.value,
	}
	return content_id({"features": features, "target": semantics.target.to_mapping(), "calendar_id": semantics.calendar_id, "calendar_version": semantics.calendar_version, "source": asdict(semantics.source)})


def train_model(dataset: TrainingDataset, config: TrainingConfig, artifact_path: Path, *, evaluation: TrainingDataset | None = None, checkpoint_path: Path | None = None, resume_from: Path | None = None, universe: ArtifactUniverse | None = None) -> TrainingResult:
	if not isinstance(config, TrainingConfig) or not isinstance(artifact_path, Path):
		raise ValueError("Training requires typed configuration and an explicit artifact path")
	paths = tuple(path.resolve() for path in (artifact_path, checkpoint_path, resume_from) if path is not None)
	if any(path == paths[0] for path in paths[1:]):
		raise ValueError("Inference artifact and resumable checkpoint paths must be separate")
	semantics = validate_chronology(dataset, evaluation)
	versioned = any((
		semantics.features.version == "configured-features-v2",
		semantics.target.version != "raw_return-v1",
		config.model.implementation_version == "stockguidance-models-v2",
		config.loss.version != "mae-v1",
		dataset.preprocessing is not None,
		universe is not None,
	))
	if versioned and dataset.preprocessing is None:
		dataset = dataset.with_preprocessing(fit_training_preprocessing(dataset, PreprocessingSpec()))
	if versioned and universe is None:
		universe = ArtifactUniverse(tuple(semantics.features.ticker_ids), tuple(str(item) for item in semantics.features.ticker_ids), "explicit_subset", dataset.revision)
	if universe is not None and universe.ticker_ids != tuple(semantics.features.ticker_ids):
		raise ValueError("Artifact universe must match the exact model ticker order")

	import torch

	resources = resolve_resources(config)
	torch.manual_seed(config.seed)
	generator = torch.Generator().manual_seed(config.seed)
	model = build_model(config.model, semantics.features).to(resources.device)
	optimizer = create_optimizer(model, config, resources)
	scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=config.learning_rate_gamma)
	scaler = torch.amp.GradScaler("cuda", enabled=resources.precision is Precision.FP16)
	revision = dataset_revision(dataset)
	checkpoint_v2 = any((semantics.features.version == "configured-features-v2", semantics.target.version != "raw_return-v1", config.model.implementation_version == "stockguidance-models-v2", config.loss.version != "mae-v1"))
	identity = checkpoint_identity(config, revision, device=str(resources.device), precision=resources.precision.value, framework_version=str(torch.__version__), preprocessing_identity=None if dataset.preprocessing is None else dataset.preprocessing.identity, semantics_identity=semantics_identity(semantics) if checkpoint_v2 else None, universe_identity=None if not checkpoint_v2 or universe is None else content_id(universe.to_mapping()))
	checkpoint_schema = "stockguidance-checkpoint-3" if checkpoint_v2 else "stockguidance-checkpoint-1" if dataset.preprocessing is None else "stockguidance-checkpoint-2"
	if resume_from is None:
		start_epoch = 0
	elif checkpoint_schema == "stockguidance-checkpoint-1":
		start_epoch = resume_checkpoint(resume_from, identity, model, optimizer, scheduler, scaler, generator)
	else:
		start_epoch = resume_checkpoint(resume_from, identity, model, optimizer, scheduler, scaler, generator, schema=checkpoint_schema)
	if start_epoch >= config.epochs:
		raise ValueError("Resume requires a larger total epoch count than the checkpoint")
	loader = make_loader(dataset, config, generator, shuffle=config.shuffle)
	metrics: list[EpochMetrics] = []
	output_transform = None if dataset.preprocessing is None else dataset.preprocessing.output
	for epoch in range(start_epoch + 1, config.epochs + 1):
		epoch_result = fit_epoch_metrics(model, loader, optimizer, scaler, resources, config.loss, output_transform)
		metrics.append(EpochMetrics(epoch, epoch_result.mae, float(optimizer.param_groups[0]["lr"]), epoch_result.objective_loss, epoch_result.rmse, epoch_result.pair_count, epoch_result.ordering_accuracy))
		scheduler.step()
	evaluation_generator = torch.Generator().manual_seed(config.seed)
	training_metrics = evaluate_metrics(model, make_loader(dataset, config, evaluation_generator, shuffle=False), resources, config.loss, output_transform)
	evaluation_dataset = None if evaluation is None else evaluation if dataset.preprocessing is None else evaluation.with_preprocessing(dataset.preprocessing)
	evaluation_metrics = None if evaluation_dataset is None else evaluate_metrics(model, make_loader(evaluation_dataset, config, evaluation_generator, shuffle=False), resources, config.loss, output_transform)
	training_mae = training_metrics.mae
	evaluation_mae = None if evaluation_metrics is None else evaluation_metrics.mae
	provenance = TrainingProvenance(dataset.training_cutoff, dataset.training_cutoff, dataset.eligible[0].timing.entry_session.session_date, dataset.eligible[-1].timing.entry_session.session_date, dataset.eligible[-1].timing.exit_session.session_date, dataset.eligible[-1].timing.exit_session.closes_at, revision, tuple(sample.cache_key for sample in dataset.eligible), tuple(sample.cache_key for sample in dataset.excluded), config.seed, config.epochs, dataset.data_snapshot)
	metadata = ArtifactMetadata(
		semantics, config.model, provenance, str(torch.__version__),
		preprocessing=dataset.preprocessing if versioned else None,
		loss=config.loss if versioned else None,
		universe=universe if versioned else None,
	)
	if checkpoint_path is not None:
		if checkpoint_schema == "stockguidance-checkpoint-1":
			save_checkpoint(checkpoint_path, identity, config.epochs, model, optimizer, scheduler, scaler, generator)
		else:
			save_checkpoint(checkpoint_path, identity, config.epochs, model, optimizer, scheduler, scaler, generator, schema=checkpoint_schema)
	model.float()
	artifact = export_artifact(artifact_path, model, metadata)
	target_kind = getattr(dataset.eligible[0].target_specification, "kind", None)
	raw_return_metrics_available = getattr(target_kind, "value", target_kind) == "raw_return"
	return TrainingResult(
		artifact, config.epochs, len(dataset.eligible), excluded_samples(dataset), tuple(metrics), training_mae,
		EvaluationKind.IN_SAMPLE if evaluation is None else EvaluationKind.CHRONOLOGICAL_HOLDOUT, evaluation_mae,
		0 if evaluation is None else len(evaluation.eligible), excluded_samples(evaluation), None if evaluation is None else evaluation.training_cutoff,
		None if evaluation is None else dataset_revision(evaluation), str(resources.device), resources.precision.value,
		semantics.availability_policy, dataset.data_snapshot, revision, config.loss.objective.value, training_metrics.objective_loss,
		training_metrics.rmse, training_metrics.pair_count, training_metrics.ordering_accuracy,
		None if evaluation_metrics is None else evaluation_metrics.objective_loss,
		None if evaluation_metrics is None else evaluation_metrics.rmse,
		0 if evaluation_metrics is None else evaluation_metrics.pair_count,
		None if evaluation_metrics is None else evaluation_metrics.ordering_accuracy,
		raw_return_metrics_available, training_metrics.mae if raw_return_metrics_available else None,
		training_metrics.rmse if raw_return_metrics_available else None,
		None if evaluation_metrics is None or not raw_return_metrics_available else evaluation_metrics.mae,
		None if evaluation_metrics is None or not raw_return_metrics_available else evaluation_metrics.rmse,
		config.loss.version,
		"fractional_return" if raw_return_metrics_available else "target_score",
		"softplus_margin" if config.loss.objective.value == "pairwise_ranking" else "target_units",
	)
