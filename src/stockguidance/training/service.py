from pathlib import Path

from stockguidance.artifacts.contracts import ArtifactMetadata, TrainingProvenance
from stockguidance.artifacts.exporting import export_artifact
from stockguidance.datasets.training import TrainingDataset
from stockguidance.models.factory import build_model
from stockguidance.training.batches import make_loader
from stockguidance.training.checkpoints import checkpoint_identity, resume_checkpoint, save_checkpoint
from stockguidance.training.configuration import Precision, TrainingConfig
from stockguidance.training.data import dataset_revision, validate_chronology
from stockguidance.training.engine import evaluate, fit_epoch
from stockguidance.training.resources import create_optimizer, resolve_resources
from stockguidance.training.result import EpochMetrics, EvaluationKind, ExcludedSample, TrainingResult


def excluded_samples(dataset: TrainingDataset | None) -> tuple[ExcludedSample, ...]:
	return () if dataset is None else tuple(ExcludedSample(sample.timing.entry_session.session_date, sample.feature_state.value, sample.target_state.value, tuple(sorted({issue.state.value for issue in (*sample.features.issues, *sample.targets.issues)}))) for sample in dataset.excluded)


def train_model(dataset: TrainingDataset, config: TrainingConfig, artifact_path: Path, *, evaluation: TrainingDataset | None = None, checkpoint_path: Path | None = None, resume_from: Path | None = None) -> TrainingResult:
	if not isinstance(config, TrainingConfig) or not isinstance(artifact_path, Path):
		raise ValueError("Training requires typed configuration and an explicit artifact path")
	paths = tuple(path.resolve() for path in (artifact_path, checkpoint_path, resume_from) if path is not None)
	if any(path == paths[0] for path in paths[1:]):
		raise ValueError("Inference artifact and resumable checkpoint paths must be separate")
	semantics = validate_chronology(dataset, evaluation)

	import torch

	resources = resolve_resources(config)
	torch.manual_seed(config.seed)
	generator = torch.Generator().manual_seed(config.seed)
	model = build_model(config.model, semantics.features).to(resources.device)
	optimizer = create_optimizer(model, config, resources)
	scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=config.learning_rate_gamma)
	scaler = torch.amp.GradScaler("cuda", enabled=resources.precision is Precision.FP16)
	revision = dataset_revision(dataset)
	identity = checkpoint_identity(config, revision, device=str(resources.device), precision=resources.precision.value, framework_version=str(torch.__version__))
	start_epoch = 0 if resume_from is None else resume_checkpoint(resume_from, identity, model, optimizer, scheduler, scaler, generator)
	if start_epoch >= config.epochs:
		raise ValueError("Resume requires a larger total epoch count than the checkpoint")
	loader = make_loader(dataset, config, generator, shuffle=config.shuffle)
	metrics: list[EpochMetrics] = []
	for epoch in range(start_epoch + 1, config.epochs + 1):
		loss = fit_epoch(model, loader, optimizer, scaler, resources)
		metrics.append(EpochMetrics(epoch, loss, float(optimizer.param_groups[0]["lr"])))
		scheduler.step()
	evaluation_generator = torch.Generator().manual_seed(config.seed)
	training_mae = evaluate(model, make_loader(dataset, config, evaluation_generator, shuffle=False), resources)
	evaluation_mae = None if evaluation is None else evaluate(model, make_loader(evaluation, config, evaluation_generator, shuffle=False), resources)
	provenance = TrainingProvenance(dataset.training_cutoff, dataset.training_cutoff, dataset.eligible[0].timing.entry_session.session_date, dataset.eligible[-1].timing.entry_session.session_date, dataset.eligible[-1].timing.exit_session.session_date, dataset.eligible[-1].timing.exit_session.closes_at, revision, tuple(sample.cache_key for sample in dataset.eligible), tuple(sample.cache_key for sample in dataset.excluded), config.seed, config.epochs)
	metadata = ArtifactMetadata(semantics, config.model, provenance, str(torch.__version__))
	if checkpoint_path is not None:
		save_checkpoint(checkpoint_path, identity, config.epochs, model, optimizer, scheduler, scaler, generator)
	model.float()
	artifact = export_artifact(artifact_path, model, metadata)
	return TrainingResult(artifact, config.epochs, len(dataset.eligible), excluded_samples(dataset), tuple(metrics), training_mae, EvaluationKind.IN_SAMPLE if evaluation is None else EvaluationKind.CHRONOLOGICAL_HOLDOUT, evaluation_mae, 0 if evaluation is None else len(evaluation.eligible), excluded_samples(evaluation), None if evaluation is None else evaluation.training_cutoff, None if evaluation is None else dataset_revision(evaluation), str(resources.device), resources.precision.value)
